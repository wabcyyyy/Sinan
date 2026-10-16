"""没有活持有者时的规划任务终态收敛（V16/T1，恢复扫描专用）。

`planning_tasks.finish_failure` 服务"持有者还活着、自己收尾"；这里服务另一侧：
持有者已经死了（租约过期）、进程重启后没有句柄、或取消旗已落但没人接。两条线共用
同一套**交付语义**——已交付天保留为 PARTIAL 且必须带最终预算，一天没交付才 FAILED——
区别只在资格：这里没有活租约需要 CAS（那个 worker 已经没了）。

`settle_*` 两个函数只收执行态（产品终态已由别人写好），少这一步就会出现
"产品 COMPLETED、执行面还挂着 QUEUED/RUNNING（只是租约过期）"，于是每轮扫描都重进
这条已交付完的行程。
"""

from __future__ import annotations

import logging

from app.db.models import ItineraryMain
from app.db.session import session_scope
from app.services import budget_engine, day_persistence, generation_events, itinerary_query
from app.services import planning_task_rows as rows
from app.services import planning_task_store as store

logger = logging.getLogger(__name__)


def terminalize_expired(itinerary_id: int, *, completed_days: int) -> None:
    """无持有者时的过期收敛（恢复扫描用），语义见 `_terminalize_without_holder`。"""
    _terminalize_without_holder(
        itinerary_id,
        completed_days=completed_days,
        outcome=rows.EXPIRED,
        event_code="TIMEOUT",
        fail_code=None,
        reason="规划任务已超过总时限，请重新生成",
        partial_code="EXPIRED_PARTIAL",
        user_facing=True,
    )


def terminalize_cancelled(itinerary_id: int, *, completed_days: int) -> None:
    """用户已按取消、持有者却当场死掉的收敛：旗子写在持久面，不认它就没人来收敛。

    恢复扫描原先只看租约，这类任务会被每 90s 接管一次、每次都在 `TaskStopped` 上退出，
    CANCELLED 因此是不可达状态（PLAN T1「取消停止后续外呼与写入」+「重启恢复」的交叉洞）。
    """
    _terminalize_without_holder(
        itinerary_id,
        completed_days=completed_days,
        outcome=rows.CANCELLED,
        event_code="CANCELLED",
        fail_code="CANCELLED",
        reason="已取消生成",
        partial_code="CANCELLED_PARTIAL",
        user_facing=False,
    )


def _terminalize_without_holder(
    itinerary_id: int,
    *,
    completed_days: int,
    outcome: str,
    event_code: str,
    fail_code: str | None,
    reason: str,
    partial_code: str,
    user_facing: bool,
) -> None:
    """无持有者时的终态收敛：产品终态 + 最终预算 + 执行态一次写完（唯一实现）。

    与 `planning_tasks.finish_failure` 同一套交付语义——已交付天保留为 PARTIAL 且必须带
    最终预算，一天没交付才 FAILED。区别只在资格：这里没有活租约需要 CAS（那个 worker
    已经没了）。
    """
    with session_scope() as session:
        main = session.get(ItineraryMain, itinerary_id)
        if main is None:
            return
        days = main.days
        user_id = int(main.user_id)
    if completed_days:
        budget_engine.recalculate(itinerary_id)
        day_persistence.complete_trip(itinerary_id, False)
        generation_events.complete(
            itinerary_id, "PARTIAL", days, day_persistence.unfinished_day_nos(itinerary_id), None
        )
        store.force_terminal(itinerary_id, outcome, partial_code)
    else:
        day_persistence.fail_trip(itinerary_id, reason, user_facing=user_facing, code=fail_code)
        generation_events.error(itinerary_id, event_code, reason, True)
        store.force_terminal(itinerary_id, outcome, outcome)
    itinerary_query.evict_detail(user_id, itinerary_id)


def settle_success(itinerary_id: int, task_id: str) -> None:
    """恢复路径把整趟补完之后落执行终态（本地句柄此时已随 `release` 交还）。

    少这一步就会出现"产品 COMPLETED、执行面还挂着 QUEUED/RUNNING（只是租约过期）"，
    于是每轮扫描都重进这条已交付完的行程——终态与可见条目必须一起收敛。
    """
    if not store.set_terminal(itinerary_id, task_id, rows.FINISHED, None):
        logger.warning("planning task %s FINISHED not written: lease no longer held", itinerary_id)


def settle_without_holder(itinerary_id: int, outcome: str, code: str | None) -> None:
    """没有活持有者时的执行终态（恢复扫描自己判定"已交付完"）：只收 live 行，不做 CAS。"""
    store.force_terminal(itinerary_id, outcome, code)
