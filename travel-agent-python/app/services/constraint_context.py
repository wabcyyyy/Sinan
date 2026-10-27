"""以持久任务令牌保护 CP 阶段快照，接管沿用原绝对截止及预算。"""

from sqlalchemy import select

from app.agent import (
    TaskOwnershipLost,
    check_task_write,
    current_limits,
    load_constraint_context,
    save_constraint_context,
)
from app.db.models import PlanningTask
from app.db.session import session_scope


def load_context(user_id, itinerary_id, fingerprint):
    limits = current_limits()
    if limits is None or limits.task_id is None or limits.deadline_at is None:
        return None
    check_task_write()
    return load_constraint_context(user_id, itinerary_id, fingerprint, limits.deadline_at)


def save_context(user_id, itinerary_id, fingerprint, context):
    limits = current_limits()
    if limits is None or limits.task_id is None or limits.deadline_at is None:
        return
    check_task_write()
    with session_scope() as session:
        row = session.scalar(select(PlanningTask).where(PlanningTask.itinerary_id == itinerary_id).with_for_update())
        if (
            row is None
            or row.user_id != user_id
            or row.task_id != limits.task_id
            or row.state not in {"QUEUED", "RUNNING"}
            or row.cancel_requested_at
        ):
            raise TaskOwnershipLost("阶段快照的执行权已失效")
        # 接管必须取得同行锁；旧 worker 不可在新 owner 写入后覆盖快照。
        save_constraint_context(user_id, itinerary_id, fingerprint, limits.deadline_at, context)
