"""规划任务的进程内运行句柄（V16/T1）：本地句柄表 + 绝对截止定时器 + 心跳续租。

`planning_tasks` 管一次尝试的生命周期，本模块只回答两件事：

1. **本 worker 手上有没有这份句柄**（`task_limits` / `is_held_local` / `holds_task`）。
   句柄表**不是**所有权依据——所有权永远是 `planning_task` 那一行（判据在
   `planning_task_rows.holds_lease`），这里只用于取回正在跑的 `RunLimits` 与定时器。
2. **两根定时器**：绝对截止到了就触发终态收敛（`on_expire` 由生命周期层传入，于是
   本模块不需要反向 import `planning_tasks`），心跳按 `PLANNING_HEARTBEAT_SECONDS`
   续租，续不上就地作废句柄。

失权（`lost`）登记在这里：心跳续不上、或探针判定已失权时，句柄立刻从表里摘掉，
后续每一次预算写入与落库探针都会拒。
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass, field

from app.agent import RunLimits
from app.common.config import settings
from app.services import planning_task_rows as rows
from app.services import planning_task_store as store

logger = logging.getLogger(__name__)


@dataclass
class Handle:
    """本 worker 手上的一份执行句柄（本地定时器 + 账本增量缓存，不承载所有权）。"""

    limits: RunLimits
    deadline_timer: threading.Timer | None = None
    heartbeat_timer: threading.Timer | None = None
    enriching: bool = False
    lost: bool = False
    # 这份 attempt 是不是"对已结算交付责任的后台重拉"（= 一次补齐，V17/T2）。
    # 用户侧补齐在 `itinerary_generation.regenerate_day` 自己登记结果，两者不双计。
    repair: bool = False
    outcome: str | None = None
    result_code: str | None = None
    last_counters: dict[str, int] = field(default_factory=dict)
    probe_at: float = 0.0


LOCK = threading.RLock()
HELD: dict[int, Handle] = {}


def task_limits(itinerary_id: int) -> RunLimits | None:
    """本 worker 正在跑的这份预算（排队与编排线程内部传递用）。"""
    with LOCK:
        held = HELD.get(itinerary_id)
        return held.limits if held else None


def is_held_local(itinerary_id: int) -> bool:
    """本 worker 手上是否有这份任务——只服务"谁该收尾"，不是所有权判据。"""
    with LOCK:
        return itinerary_id in HELD


def holds_task(itinerary_id: int) -> bool:
    """当前 worker 是否仍有资格为这趟行程写终态（本地句柄 + 持久租约都成立）。

    终态写入前问一次：超时定时器或恢复接管可能已经把结论写掉，此时旧 worker 的
    `_finish` 会把接管者判的 PARTIAL 覆盖成 COMPLETED——那正是"失权后写回"的产品后果。
    """
    with LOCK:
        held = HELD.get(itinerary_id)
        if held is None or held.lost:
            return False
        task_id = held.limits.task_id
    return rows.holds_lease(itinerary_id, task_id)


# -------------------------------------------------------------------- 定时器 ----


def arm(delay: float, target: Callable, args: tuple) -> threading.Timer:
    timer = threading.Timer(delay, target, args)
    timer.daemon = True
    timer.start()
    return timer


def cancel_timers(held: Handle) -> None:
    for timer in (held.deadline_timer, held.heartbeat_timer):
        if timer is not None:
            timer.cancel()


def start_timers(held: Handle, on_expire: Callable[[int, str], None]) -> None:
    """挂上两根定时器：绝对截止（`on_expire`，由生命周期层实现）与心跳续租。"""
    limits = held.limits
    assert limits.itinerary_id is not None and limits.task_id is not None
    itinerary_id, task_id = limits.itinerary_id, limits.task_id
    remaining = max(0.001, limits.remaining())
    held.deadline_timer = arm(remaining, on_expire, (itinerary_id, task_id))
    held.heartbeat_timer = arm(min(settings.planning_heartbeat_seconds, remaining), heartbeat, (itinerary_id, task_id))


def heartbeat(itinerary_id: int, task_id: str) -> None:
    """续租；续不上就说明已被接管或已终态，句柄就地作废（不再续，探针立刻拒）。"""
    if not store.renew_lease(itinerary_id, task_id):
        logger.warning("lease renewal lost for itinerary %s (task %s)", itinerary_id, task_id)
        with LOCK:
            held = HELD.get(itinerary_id)
            if held is not None and held.limits.task_id == task_id:
                held.lost = True
                cancel_timers(held)
                HELD.pop(itinerary_id, None)
        return
    with LOCK:
        held = HELD.get(itinerary_id)
        if held is None or held.lost or held.limits.task_id != task_id:
            return
        held.heartbeat_timer = arm(settings.planning_heartbeat_seconds, heartbeat, (itinerary_id, task_id))
