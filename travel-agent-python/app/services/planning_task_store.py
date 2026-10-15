"""`planning_task` 行的持久原语（V16/T1）：所有权、租约、绝对截止与累计账本。

本模块只做"这一行上的事"：条件更新、租约续期、账本增量、状态迁移与行重建。
**不碰进程内句柄表**，也不写产品态（`itinerary_main` 的投影归 `planning_tasks` 与编排层）：
谁在跑 = (task_id, lease_owner, state, lease_expires_at)；花了多少 = consumed_* + budget_json。

不变量：

- 每一次推进状态或账本的写，都是 `(itinerary_id, task_id, lease_owner=本 worker,
  state∈live, cancel 未请求)` 的条件更新；rowcount=0 就是已经失权，调用方必须停手。
- 账本增量由调用方给出（它知道自己上次落库的计数器），本模块不做第二次读，因此
  "累计消耗"永远等于历次真实增量的和，不会因为接管而重复计数。
- 时刻取应用侧时钟（与 `generation_recovery` 的僵尸判定同源，部署保持应用与库同时区）。
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from dataclasses import fields
from datetime import datetime, timedelta

from sqlalchemy import or_, select, update
from sqlalchemy.exc import IntegrityError

from app.agent import RunLimits, TaskCancelled, TaskOwnershipLost
from app.common.config import settings
from app.common.envelope import ApiError
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

#: 当前 attempt 快照里的计数器 → 本责任累计账本列（只增，负增量不入账）
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


def open_attempt(user_id: int, itinerary_id: int) -> tuple[RunLimits, dict]:
    """开一个新的执行尝试（首次提交，或显式补齐/重建），返回运行对象与本次快照。

    准入两条：同行程的活租约直接拒（重复提交/双跑），同用户的其它活任务拒 429
    （"你已有一个行程任务正在处理"）——两条都读这一张表，所以重启后依然成立。
    """
    task_id = uuid.uuid4().hex
    at = now()
    window = settings.itinerary_deadline_seconds
    snapshot = new_attempt_snapshot(user_id, itinerary_id, task_id, at, window)
    with session_scope() as session:
        row = read(session, itinerary_id, for_update=True)
        if row is not None and row.state in LIVE_STATES and lease_alive(row, at):
            raise ApiError(409, "该行程已有生成任务在运行，请等它结束")
        busy = session.scalar(
            select(PlanningTask.itinerary_id).where(
                PlanningTask.user_id == user_id,
                PlanningTask.itinerary_id != itinerary_id,
                PlanningTask.state.in_(LIVE_STATES),
                PlanningTask.lease_expires_at > at,
            )
        )
        if busy is not None:
            raise ApiError(429, "你已有一个行程任务正在处理，请等它结束后再试")
        if row is None:
            session.add(
                PlanningTask(
                    itinerary_id=itinerary_id,
                    user_id=user_id,
                    task_id=task_id,
                    lease_owner=WORKER_ID,
                    attempt=1,
                    state=QUEUED,
                    submit_at=at,
                    deadline_at=datetime.fromtimestamp(snapshot["deadline_at"]),
                    lease_expires_at=lease_until(at),
                    heartbeat_at=at,
                    budget_json=json.dumps(snapshot),
                )
            )
            try:
                session.flush()
            except IntegrityError:
                # 主键就是 itinerary_id：并发首次提交时两条都可能读到"没有行"。
                # 让先落库的那条赢，另一条按同一口径回 409，而不是抛 500。
                raise ApiError(409, "该行程已有生成任务在运行，请等它结束") from None
        else:
            # 累计账本从行上的 consumed_* 接着记：显式补齐/重建重开的是时间窗，不是额度。
            # 上限（max_*）不动，所以一个已经烧完本轮调用与 token 的责任重来一次也拿不到新配额；
            # 运营放行的额外修复预算由 T2 显式控制，不在这里悄悄放宽。
            snapshot.update(store_totals(row))
            session.execute(
                update(PlanningTask)
                .where(PlanningTask.itinerary_id == itinerary_id)
                .values(
                    user_id=user_id,
                    task_id=task_id,
                    lease_owner=WORKER_ID,
                    attempt=row.attempt + 1,
                    state=QUEUED,
                    deadline_at=datetime.fromtimestamp(snapshot["deadline_at"]),
                    lease_expires_at=lease_until(at),
                    heartbeat_at=at,
                    cancel_requested_at=None,
                    result_code=None,
                    budget_json=json.dumps(snapshot),
                )
            )
        rebuilt = read(session, itinerary_id)
        assert rebuilt is not None
        return build_limits(rebuilt, snapshot), snapshot


def take_over(user_id: int, itinerary_id: int) -> tuple[RunLimits, dict]:
    """接管仍在绝对截止内的任务：时间窗与计数器都沿用，只换持有者令牌并推进 attempt。"""
    task_id = uuid.uuid4().hex
    at = now()
    with session_scope() as session:
        row = read(session, itinerary_id, for_update=True)
        if row is None:
            raise TaskWindowExpired(f"行程 {itinerary_id} 没有任务行")
        if row.user_id != user_id:
            raise ApiError(409, "任务归属与行程用户不一致")
        if row.deadline_at <= at:
            raise TaskWindowExpired(f"行程 {itinerary_id} 已超过绝对截止 {row.deadline_at}")
        if row.state in LIVE_STATES and lease_alive(row, at):
            # 调用方的 is_active() 读的是另一个事务，中间可能有 worker 刚续租。
            # 这里以 FOR UPDATE 锁住的当前行为准：租约还活着就是别人在跑，让路而不是抢占。
            raise LeaseHeld(f"行程 {itinerary_id} 的租约仍由 {row.lease_owner} 持有")
        snapshot = json.loads(row.budget_json) if row.budget_json else {}
        if not isinstance(snapshot, dict):
            snapshot = {}
        snapshot.update(
            {
                "task_id": task_id,
                "user_id": row.user_id,
                "itinerary_id": itinerary_id,
                "deadline_at": row.deadline_at.timestamp(),
            }
        )
        result = session.execute(
            update(PlanningTask)
            .where(
                PlanningTask.itinerary_id == itinerary_id,
                or_(PlanningTask.lease_expires_at <= at, PlanningTask.state.notin_(LIVE_STATES)),
            )
            .values(
                task_id=task_id,
                lease_owner=WORKER_ID,
                attempt=row.attempt + 1,
                lease_expires_at=lease_until(at),
                heartbeat_at=at,
                budget_json=json.dumps(snapshot),
            )
        )
        if getattr(result, "rowcount", 0) == 0:
            raise LeaseHeld(f"行程 {itinerary_id} 的租约在接管期间被其它 worker 续上")
        rebuilt = read(session, itinerary_id)
        assert rebuilt is not None
        return build_limits(rebuilt, snapshot), snapshot


def apply_budget(itinerary_id: int, task_id: str, snapshot: dict, deltas: dict[str, int]) -> None:
    """预算落库 = 同一次条件更新里续租 + 累计账本加增量。"""
    at = now()
    values: dict = {"budget_json": json.dumps(snapshot), "lease_expires_at": lease_until(at), "heartbeat_at": at}
    for column, delta in deltas.items():
        values[column] = getattr(PlanningTask, column) + delta
    statement = (
        update(PlanningTask)
        .where(
            PlanningTask.itinerary_id == itinerary_id,
            PlanningTask.task_id == task_id,
            PlanningTask.lease_owner == WORKER_ID,
            PlanningTask.state.in_(LIVE_STATES),
            PlanningTask.cancel_requested_at.is_(None),
        )
        .values(**values)
    )
    with session_scope() as session:
        if getattr(session.execute(statement), "rowcount", 0) == 0:
            raise loss_reason(itinerary_id, task_id)


def renew_lease(itinerary_id: int, task_id: str) -> bool:
    """心跳续租：只动租约两列（没花成本就不必改账本快照）。"""
    at = now()
    statement = (
        update(PlanningTask)
        .where(
            PlanningTask.itinerary_id == itinerary_id,
            PlanningTask.task_id == task_id,
            PlanningTask.lease_owner == WORKER_ID,
            PlanningTask.state.in_(LIVE_STATES),
        )
        .values(lease_expires_at=lease_until(at), heartbeat_at=at)
    )
    with session_scope() as session:
        return bool(getattr(session.execute(statement), "rowcount", 0))


def mark_running(itinerary_id: int, task_id: str) -> None:
    """排队 → 执行：plan_days 真正开始的那一刻（QUEUED 之外不重复迁移）。"""
    at = now()
    with session_scope() as session:
        session.execute(
            update(PlanningTask)
            .where(
                PlanningTask.itinerary_id == itinerary_id,
                PlanningTask.task_id == task_id,
                PlanningTask.lease_owner == WORKER_ID,
                PlanningTask.state == QUEUED,
            )
            .values(state=RUNNING, lease_expires_at=lease_until(at), heartbeat_at=at)
        )


def clear_lease(itinerary_id: int, task_id: str) -> None:
    """交还租约但不落结论（提交被拒/进程收尾）：让恢复扫描按持久面判定接管或终态化。"""
    with session_scope() as session:
        session.execute(
            update(PlanningTask)
            .where(
                PlanningTask.itinerary_id == itinerary_id,
                PlanningTask.task_id == task_id,
                PlanningTask.lease_owner == WORKER_ID,
                PlanningTask.state.in_(LIVE_STATES),
            )
            .values(lease_expires_at=now())
        )


def set_terminal(itinerary_id: int, task_id: str, state: str, result_code: str | None) -> bool:
    """持有者落执行终态；失权则返回 False（不覆盖新持有者的结论）。"""
    at = now()
    with session_scope() as session:
        rowcount = getattr(
            session.execute(
                update(PlanningTask)
                .where(
                    PlanningTask.itinerary_id == itinerary_id,
                    PlanningTask.task_id == task_id,
                    PlanningTask.lease_owner == WORKER_ID,
                    PlanningTask.state.in_(LIVE_STATES),
                )
                .values(state=state, result_code=result_code, lease_expires_at=at, heartbeat_at=at)
            ),
            "rowcount",
            0,
        )
    return bool(rowcount)


def force_terminal(itinerary_id: int, state: str, result_code: str | None) -> None:
    """无持有者时的终态（恢复扫描判定过期）：没有活租约需要 CAS。"""
    with session_scope() as session:
        session.execute(
            update(PlanningTask)
            .where(PlanningTask.itinerary_id == itinerary_id, PlanningTask.state.in_(LIVE_STATES))
            .values(state=state, result_code=result_code, lease_expires_at=now())
        )


def flag_cancel(itinerary_id: int) -> dict | None:
    """置取消旗（仅对 live 行生效），返回本次可见的 attempt/state；不在跑则返回 None。"""
    with session_scope() as session:
        row = read(session, itinerary_id, for_update=True)
        if row is None or row.state not in LIVE_STATES:
            return None
        if row.cancel_requested_at is None:
            session.execute(
                update(PlanningTask)
                .where(PlanningTask.itinerary_id == itinerary_id, PlanningTask.state.in_(LIVE_STATES))
                .values(cancel_requested_at=now())
            )
        return {"attempt": row.attempt, "state": row.state, "taskId": row.task_id}
