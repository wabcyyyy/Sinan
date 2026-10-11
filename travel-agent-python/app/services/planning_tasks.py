"""Single-process generation leases, including queued tasks and enrichment."""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass

from sqlalchemy import select

from app.agent import RunLimits, new_task_budget
from app.common.envelope import ApiError, error_code, user_reason
from app.db.models import ItineraryDay, ItineraryMain
from app.db.session import session_scope
from app.services import day_persistence, generation_events, itinerary_query


@dataclass
class PlanningTask:
    limits: RunLimits
    timer: threading.Timer
    enriching: bool = False


_lock = threading.RLock()
_tasks: dict[int, PlanningTask] = {}
logger = logging.getLogger(__name__)


def is_active(itinerary_id: int) -> bool:
    with _lock:
        return itinerary_id in _tasks


def task_limits(itinerary_id: int) -> RunLimits | None:
    with _lock:
        task = _tasks.get(itinerary_id)
        return task.limits if task else None


def acquire(user_id: int, itinerary_id: int, limits: RunLimits | None = None) -> RunLimits:
    with _lock:
        if any(task.limits.user_id == user_id for task in _tasks.values()):
            raise ApiError(429, "你已有一个行程任务正在处理，请等它结束后再试")
        budget = limits if limits is not None else new_task_budget(user_id, itinerary_id)
        timer = threading.Timer(max(0.001, budget.remaining()), _expire, (itinerary_id, budget.task_id))
        timer.daemon = True
        _tasks[itinerary_id] = PlanningTask(budget, timer)
        timer.start()
        return budget


def release(itinerary_id: int, task_id: str | None = None, *, core_only: bool = False) -> None:
    with _lock:
        task = _tasks.get(itinerary_id)
        if task is None or (task_id is not None and task.limits.task_id != task_id):
            return
        if core_only and task.enriching:
            return
        task.timer.cancel()
        _tasks.pop(itinerary_id, None)


def mark_enriching(itinerary_id: int, value: bool) -> None:
    with _lock:
        task = _tasks.get(itinerary_id)
        if task is not None:
            task.enriching = value


def reset() -> None:
    with _lock:
        for task in _tasks.values():
            task.timer.cancel()
        _tasks.clear()


def finish_failure(itinerary_id: int, exc: Exception) -> None:
    """Preserve delivered days when any later stage fails or exhausts its budget."""
    with session_scope() as session:
        main = session.get(ItineraryMain, itinerary_id)
        if main is None or main.status != 1:
            return
        delivered = session.scalar(
            select(ItineraryDay.id).where(
                ItineraryDay.itinerary_id == itinerary_id, ItineraryDay.generation_status == "SUCCEEDED"
            )
        )
    if delivered:
        day_persistence.complete_trip(itinerary_id, False)
        generation_events.complete(
            itinerary_id, "PARTIAL", main.days, day_persistence.unfinished_day_nos(itinerary_id), None
        )
    else:
        day_persistence.fail_trip(itinerary_id, str(exc), code=error_code(exc))
        generation_events.error(itinerary_id, error_code(exc), user_reason(exc), True)
    itinerary_query.evict_detail(int(main.user_id), itinerary_id)


def _expire(itinerary_id: int, task_id: str | None) -> None:
    with _lock:
        task = _tasks.get(itinerary_id)
        if task is None or task.limits.task_id != task_id:
            return
        try:
            finish_failure(itinerary_id, TimeoutError("规划任务超过总时限，请重试"))
        except Exception:
            logger.exception("could not finalize expired task %s", task_id)
        finally:
            release(itinerary_id, task_id)
