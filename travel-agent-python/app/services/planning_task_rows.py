"""`planning_task` 行的读侧原语与不变量字典（V16/T1 的"行 ↔ 快照"翻译层）。

与 `planning_task_store` 的分工：本模块只做**不推进状态**的事——身份常量与状态字母表、
绝对截止/租约的时刻算法、`budget_json` 快照与行列之间的双向翻译、单行读取与执行态投影。
状态与账本的**写**（条件更新、CAS、接管、终态）都在 `planning_task_store`。
进程内句柄表两边都不碰：谁在跑 = (task_id, lease_owner, state, lease_expires_at) 这一行。

为什么值得单独一份：`planning_tasks`（生命周期）与 `planning_task_store`（写原语）都要读
这些判据，任何一份留错地方就会变成"两个模块各自理解同一张表"。时刻取应用侧时钟
（与 `generation_recovery` 的僵尸判定同源，部署保持应用与库同时区）。
"""

from __future__ import annotations

import logging
import os
import uuid
from dataclasses import fields
from datetime import datetime, timedelta

from sqlalchemy import select

from app.agent import RunLimits, TaskCancelled, TaskOwnershipLost
from app.common.config import settings
from app.db.models import PlanningTask
from app.db.session import session_scope

logger = logging.getLogger(__name__)

# 进程身份：pid + 随机段。重启后必然变化，旧 worker 的条件更新因此恒不命中。
WORKER_ID = f"{os.getpid()}-{uuid.uuid4().hex[:8]}"

QUEUED = "QUEUED"
RUNNING = "RUNNING"
FINISHED = "FINISHED"
FAILED = "FAILED"
CANCELLED = "CANCELLED"
EXPIRED = "EXPIRED"
LIVE_STATES = (QUEUED, RUNNING)

#: 当前 attempt 快照里的计数器 → 本责任累计账本列（含 Token 结算释放的预留）
LIFETIME_COLUMNS = {
    "llm_calls": "consumed_llm_calls",
    "prompt_tokens": "consumed_prompt_tokens",
    "completion_tokens": "consumed_completion_tokens",
    "retrievals": "consumed_retrievals",
    "research_calls": "consumed_research_calls",
    "existence_checks": "consumed_existence_checks",
    "replans": "consumed_replans",
}


class TaskWindowExpired(Exception):
    """接管请求落在绝对截止之后：调用方应当终态化，而不是重开窗口。"""


class LeaseHeld(Exception):
    """接管落在一个仍然有效的租约上：任务还在别人手里跑，调用方必须让路而不是抢占。"""


def now() -> datetime:
    return datetime.now()


def lease_until(at: datetime) -> datetime:
    return at + timedelta(seconds=settings.planning_live_seconds)


def store_totals(row: PlanningTask) -> dict[str, int]:
    """行上的累计账本 → 快照计数器键（重开 attempt 时作为新快照的基线）。"""
    return {key: int(getattr(row, column) or 0) for key, column in LIFETIME_COLUMNS.items()}


def counters_of(snapshot: dict) -> dict[str, int]:
    return {key: int(snapshot.get(key) or 0) for key in LIFETIME_COLUMNS}


def budget_payload(snapshot: dict) -> dict:
    """进 `budget_json` 的那部分：剥掉一次性的成本事件。

    `cost_events`（V17/T2）描述"这一次写入要落哪些账"，不是"当前状态"。留在快照里会被
    接管后的新 worker 当成自己的待办重放——预留行按主键幂等，不至于重复扣钱，但会把
    上一个 worker 的事件算到这次头上，白多一层排查面。事件的实际落库在
    `planning_tasks._persist` 里先于本写入完成。
    """
    return {key: value for key, value in snapshot.items() if key != "cost_events"}


def read(session, itinerary_id: int, *, for_update: bool = False) -> PlanningTask | None:
    statement = select(PlanningTask).where(PlanningTask.itinerary_id == itinerary_id)
    if for_update:
        statement = statement.with_for_update()
    return session.execute(statement).scalar_one_or_none()


def is_cancel_pending(row: PlanningTask) -> bool:
    """live 行上已落下取消旗：持有者已死时这条责任只能由恢复扫描收敛，不能再被接管。"""
    return row.cancel_requested_at is not None and row.state in LIVE_STATES


def lease_alive(row: PlanningTask, at: datetime) -> bool:
    """租约仍在有效期内 = 有 worker 正在跑（准入与接管都以此判断）。"""
    return bool(row.lease_expires_at and row.lease_expires_at > at)


def state_of(itinerary_id: int) -> dict | None:
    """只读执行态（端点、恢复扫描与测试断言用；不做任何写）。"""
    with session_scope() as session:
        row = read(session, itinerary_id)
        if row is None:
            return None
        return {
            "itineraryId": row.itinerary_id,
            "taskId": row.task_id,
            "workerId": row.lease_owner,
            "attempt": row.attempt,
            "state": row.state,
            "submitAt": row.submit_at,
            "deadlineAt": row.deadline_at,
            "leaseExpiresAt": row.lease_expires_at,
            "heartbeatAt": row.heartbeat_at,
            "cancelRequestedAt": row.cancel_requested_at,
            "resultCode": row.result_code,
            "consumed": {column: int(getattr(row, column) or 0) for column in LIFETIME_COLUMNS.values()},
        }


def owns_trip_terminal(itinerary_id: int, task_id: str | None) -> bool:
    """行程终态写入的资格：**只判所有权，不判取消**。

    取消、过期、终检没跑完这三种收敛本身必须能写产品终态——用 `check_task_write()`
    守卫会被"取消旗已落下"一起拒掉，PARTIAL/FAILED 就永远落不了地。这里只问一件事：
    这趟是不是有一个**活租约不在我手上**。是 → 结论归当前持有者，本次写作废。

    `task_id` 为 None 表示调用方根本不在任务上下文里（恢复扫描的无持有者收敛）→ 放行；
    给了 `task_id` 就必须与行内令牌一致，同进程被接管的旧 worker 也因此照样被拒。
    """
    with session_scope() as session:
        row = read(session, itinerary_id)
    if row is None or row.state not in LIVE_STATES:
        return True
    if row.lease_owner != WORKER_ID:
        return False
    return task_id is None or row.task_id == task_id


def is_active(itinerary_id: int) -> bool:
    """所有权判据：状态 live 且租约未过期才算有人在跑（重启后同样问得出答案）。"""
    with session_scope() as session:
        row = read(session, itinerary_id)
    if row is None or row.state not in LIVE_STATES:
        return False
    return lease_alive(row, now())


def holds_lease(itinerary_id: int, task_id: str | None) -> bool:
    """指定身份是否仍是这行的持有者（终态写入前的资格判定）。"""
    if task_id is None:
        return False
    with session_scope() as session:
        row = read(session, itinerary_id)
    if row is None or row.state not in LIVE_STATES:
        return False
    return row.task_id == task_id and row.lease_owner == WORKER_ID


def loss_reason(itinerary_id: int, task_id: str | None) -> Exception:
    """条件更新落空之后才读一次行，把"为什么失权"说准（取消 vs 被接管/已终态）。"""
    with session_scope() as session:
        row = read(session, itinerary_id)
    if row is None:
        return TaskOwnershipLost(f"行程 {itinerary_id} 的任务行已不存在")
    if row.cancel_requested_at is not None and row.state in LIVE_STATES:
        return TaskCancelled(f"行程 {itinerary_id} 已被用户取消")
    if row.task_id != task_id:
        return TaskOwnershipLost(f"行程 {itinerary_id} 的任务已被接管（attempt {row.attempt}）")
    return TaskOwnershipLost(f"行程 {itinerary_id} 的任务已处于终态 {row.state}")


def build_limits(row: PlanningTask, snapshot: dict) -> RunLimits:
    """由行 + 快照重建 RunLimits（快照形状由 RunLimits 自己定义，不在此复制字段清单）。

    `task_id` 取自快照而不是行内旧值：接管已把新令牌写进行里，重建出的这份运行对象
    必须用它这一次的身份去做后续的条件更新。
    """
    allowed = {item.name for item in fields(RunLimits)} - {"persist", "started_at", "guard"}
    values = {key: value for key, value in snapshot.items() if key in allowed}
    values.update(
        {
            "user_id": row.user_id,
            "itinerary_id": row.itinerary_id,
            "deadline_at": row.deadline_at.timestamp(),
        }
    )
    limits = RunLimits(**values)
    limits.submitted_at = row.submit_at.timestamp()
    return limits


def new_attempt_snapshot(user_id: int, itinerary_id: int, task_id: str, at: datetime, window: float) -> dict:
    """新 attempt 的零值快照：重开的是时间窗，账本另由 `consumed_*` 列累计。"""
    return {
        "task_id": task_id,
        "user_id": user_id,
        "itinerary_id": itinerary_id,
        "submitted_at": at.timestamp(),
        "deadline_at": (at + timedelta(seconds=window)).timestamp(),
        "usage_unknown": 0,
        "terminal_resets": 0,
        "elapsed_ms": 0,
        "deadline_seconds": window,
        **{key: 0 for key in LIFETIME_COLUMNS},
        "max_llm_calls": settings.max_llm_calls,
        "max_retrievals": settings.max_retrievals,
        "max_research_calls": settings.research_call_limit,
        "max_existence_checks": settings.existence_resolve_limit,
        "max_tokens": settings.max_token_budget,
        "max_replans": settings.max_replans,
        "no_progress_count": 0,
    }
