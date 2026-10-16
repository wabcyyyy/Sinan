"""`planning_task` 行的写原语（V16/T1）：状态迁移、租约、账本增量与行重建。

本模块只做"在这行上写一次"：每一次推进状态或账本的写，都是
`(itinerary_id, task_id, lease_owner=本 worker, state∈live, cancel 未请求)` 的条件更新，
rowcount=0 就是已经失权，调用方必须停手。

行上的**读**与翻译（状态字母表、`WORKER_ID`、租约时刻算法、`budget_json` 快照 ↔ 行列、
单行读取与执行态投影）在同目录 `planning_task_rows`；生命周期与进程内句柄在
`planning_tasks`。本模块不碰句柄表，也不写产品态（`itinerary_main` 的投影归
`planning_tasks` 与编排层）。

不变量：

- 账本增量由调用方给出（它知道自己上次落库的计数器），本模块不做第二次读，因此
  "累计消耗"永远等于历次真实增量的和，不会因为接管而重复计数。
- 接管以 `FOR UPDATE` 锁住的当前行为准：租约还活着就是别人在跑，让路而不是抢占。
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime

from sqlalchemy import or_, select, update
from sqlalchemy.exc import IntegrityError

from app.agent import RunLimits
from app.common.config import settings
from app.common.envelope import ApiError
from app.db.models import PlanningTask
from app.db.session import session_scope
from app.services import planning_task_rows as rows

logger = logging.getLogger(__name__)

QUEUED = rows.QUEUED
RUNNING = rows.RUNNING
LIVE_STATES = rows.LIVE_STATES
WORKER_ID = rows.WORKER_ID
TaskWindowExpired = rows.TaskWindowExpired
LeaseHeld = rows.LeaseHeld


def open_attempt(user_id: int, itinerary_id: int) -> tuple[RunLimits, dict]:
    """开一个新的执行尝试（首次提交，或显式补齐/重建），返回运行对象与本次快照。

    准入两条：同行程的活租约直接拒（重复提交/双跑），同用户的其它活任务拒 429
    （"你已有一个行程任务正在处理"）——两条都读这一张表，所以重启后依然成立。
    """
    task_id = uuid.uuid4().hex
    at = rows.now()
    window = settings.itinerary_deadline_seconds
    snapshot = rows.new_attempt_snapshot(user_id, itinerary_id, task_id, at, window)
    with session_scope() as session:
        row = rows.read(session, itinerary_id, for_update=True)
        if row is not None and row.state in LIVE_STATES and rows.lease_alive(row, at):
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
                    lease_expires_at=rows.lease_until(at),
                    heartbeat_at=at,
                    budget_json=json.dumps(rows.budget_payload(snapshot)),
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
            snapshot.update(rows.store_totals(row))
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
                    lease_expires_at=rows.lease_until(at),
                    heartbeat_at=at,
                    cancel_requested_at=None,
                    result_code=None,
                    budget_json=json.dumps(rows.budget_payload(snapshot)),
                )
            )
        rebuilt = rows.read(session, itinerary_id)
        assert rebuilt is not None
        return rows.build_limits(rebuilt, snapshot), snapshot


def take_over(user_id: int, itinerary_id: int) -> tuple[RunLimits, dict]:
    """接管仍在绝对截止内的任务：时间窗与计数器都沿用，只换持有者令牌并推进 attempt。"""
    task_id = uuid.uuid4().hex
    at = rows.now()
    with session_scope() as session:
        row = rows.read(session, itinerary_id, for_update=True)
        if row is None:
            raise TaskWindowExpired(f"行程 {itinerary_id} 没有任务行")
        if row.user_id != user_id:
            raise ApiError(409, "任务归属与行程用户不一致")
        if row.deadline_at <= at:
            raise TaskWindowExpired(f"行程 {itinerary_id} 已超过绝对截止 {row.deadline_at}")
        if row.state in LIVE_STATES and rows.lease_alive(row, at):
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
                lease_expires_at=rows.lease_until(at),
                heartbeat_at=at,
                budget_json=json.dumps(rows.budget_payload(snapshot)),
            )
        )
        if getattr(result, "rowcount", 0) == 0:
            raise LeaseHeld(f"行程 {itinerary_id} 的租约在接管期间被其它 worker 续上")
        rebuilt = rows.read(session, itinerary_id)
        assert rebuilt is not None
        return rows.build_limits(rebuilt, snapshot), snapshot


def apply_budget(itinerary_id: int, task_id: str, snapshot: dict, deltas: dict[str, int]) -> None:
    """预算落库 = 同一次条件更新里续租 + 累计账本加增量。"""
    at = rows.now()
    values: dict = {
        "budget_json": json.dumps(rows.budget_payload(snapshot)),
        "lease_expires_at": rows.lease_until(at),
        "heartbeat_at": at,
    }
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
            raise rows.loss_reason(itinerary_id, task_id)


def renew_lease(itinerary_id: int, task_id: str) -> bool:
    """心跳续租：只动租约两列（没花成本就不必改账本快照）。"""
    at = rows.now()
    statement = (
        update(PlanningTask)
        .where(
            PlanningTask.itinerary_id == itinerary_id,
            PlanningTask.task_id == task_id,
            PlanningTask.lease_owner == WORKER_ID,
            PlanningTask.state.in_(LIVE_STATES),
        )
        .values(lease_expires_at=rows.lease_until(at), heartbeat_at=at)
    )
    with session_scope() as session:
        return bool(getattr(session.execute(statement), "rowcount", 0))


def mark_running(itinerary_id: int, task_id: str) -> None:
    """排队 → 执行：plan_days 真正开始的那一刻（QUEUED 之外不重复迁移）。"""
    at = rows.now()
    with session_scope() as session:
        session.execute(
            update(PlanningTask)
            .where(
                PlanningTask.itinerary_id == itinerary_id,
                PlanningTask.task_id == task_id,
                PlanningTask.lease_owner == WORKER_ID,
                PlanningTask.state == QUEUED,
            )
            .values(state=RUNNING, lease_expires_at=rows.lease_until(at), heartbeat_at=at)
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
            .values(lease_expires_at=rows.now())
        )


def set_terminal(itinerary_id: int, task_id: str, state: str, result_code: str | None) -> bool:
    """持有者落执行终态；失权则返回 False（不覆盖新持有者的结论）。"""
    at = rows.now()
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
            .values(state=state, result_code=result_code, lease_expires_at=rows.now())
        )


def flag_cancel(itinerary_id: int) -> dict | None:
    """置取消旗（仅对 live 行生效），返回本次可见的 attempt/state；不在跑则返回 None。"""
    with session_scope() as session:
        row = rows.read(session, itinerary_id, for_update=True)
        if row is None or row.state not in LIVE_STATES:
            return None
        if row.cancel_requested_at is None:
            session.execute(
                update(PlanningTask)
                .where(PlanningTask.itinerary_id == itinerary_id, PlanningTask.state.in_(LIVE_STATES))
                .values(cancel_requested_at=rows.now())
            )
        return {"attempt": row.attempt, "state": row.state, "taskId": row.task_id}
