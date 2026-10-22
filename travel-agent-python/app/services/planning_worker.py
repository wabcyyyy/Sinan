"""规划执行进程的调度；API 只写投递意图，产品执行仍复用 plan_days。"""

import logging
import time

from app.common.config import settings
from app.services import itinerary_generation, planning_queue, planning_task_runs, planning_tasks

logger = logging.getLogger(__name__)


def run_once(recover_at: float) -> float:
    """空槽先给恢复任务机会；满载时也定期收敛过期/取消责任。"""
    from app.services import generation_recovery

    if available() or time.monotonic() >= recover_at:
        generation_recovery.recover()
        recover_at = time.monotonic() + generation_recovery.SCAN_INTERVAL_SECONDS
    poll_once()
    return recover_at


def available() -> bool:
    with planning_task_runs.LOCK:
        return len(planning_task_runs.HELD) < settings.planning_worker_concurrency


def poll_once() -> int:
    """只领取本进程能立即处理的任务；其余排队责任留在数据库中。"""
    claimed = 0
    while available():
        limits = planning_tasks.claim_queued()
        if limits is None:
            break
        itinerary_id = limits.itinerary_id
        assert itinerary_id is not None and limits.user_id is not None
        try:
            command = planning_queue.command_for(itinerary_id)
            itinerary_generation.submit_planning(limits.user_id, itinerary_id, command, limits=limits)
            claimed += 1
        except Exception as exc:
            planning_tasks.finish_failure(itinerary_id, exc)
            planning_tasks.release(itinerary_id, limits.task_id)
            logger.exception("planning dispatch failed: itinerary=%s", itinerary_id)
    return claimed
