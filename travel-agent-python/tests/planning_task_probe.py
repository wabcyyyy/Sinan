"""测试侧的"持有者进程已死"模拟（V16/T1 持久租约）。

持久执行面上线后，"进程重启"不再等于"清空进程内活跃表"：租约写在 `planning_task` 行里，
只清本地句柄的话，行内 `lease_expires_at` 仍然新鲜，恢复扫描与补齐端点都会正确地认为
"还有人在跑"。要模拟真死，两条输入都得动：本地句柄清空 + 租约过期。

生产代码不提供"把租约弄过期"的入口（那是时间的活），所以这个动作放在测试侧。
调用方必须已经初始化业务库（用例自带的 SQLite 夹具即可）。
"""

from datetime import datetime, timedelta

from sqlalchemy import update

from app.db import session as db_session
from app.db.models import PlanningTask
from app.services import planning_tasks


def expire_lease(*itinerary_ids: int) -> None:
    """只把租约拨到已过期，本地句柄不动。

    为什么单独要这一步：`takeover` 现在拒绝抢占仍在续租的租约（`LeaseHeld`），与恢复
    扫描的口径一致——判据是先 `is_active() == False`。用例要合法地演"被第二个 worker
    接管"，就得先让租约过期，而不是绕开这条不变式。
    """
    if not itinerary_ids:
        return
    dead = datetime.now() - timedelta(seconds=1)
    with db_session.session_scope() as session:
        session.execute(
            update(PlanningTask)
            .where(PlanningTask.itinerary_id.in_(list(itinerary_ids)))
            .values(lease_expires_at=dead, heartbeat_at=dead)
        )


def simulate_worker_exit(*itinerary_ids: int) -> None:
    """清掉本进程的句柄，并把给定行程的租约拨到已过期（= 那个 worker 不会再回来）。"""
    planning_tasks.reset()
    expire_lease(*itinerary_ids)
