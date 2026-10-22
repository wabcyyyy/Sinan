"""MySQL 中的规划投递意图；与行程建壳共用事务，不在 API 内申请执行租约。"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import TYPE_CHECKING

from pydantic import TypeAdapter
from sqlalchemy import select

from app.common.config import settings
from app.common.envelope import ApiError
from app.db.models import PlanningQueueGuard, PlanningTask
from app.db.session import session_scope
from app.services import planning_task_rows as rows
from app.services import planning_task_store as store

if TYPE_CHECKING:
    from app.services.itinerary_generation import GenerateCommand


@contextmanager
def admission() -> Iterator[None]:
    """容量检查与任务插入串行提交；重试幂等回放由调用方先处理。"""
    with session_scope() as session:
        guard = session.scalar(select(PlanningQueueGuard).where(PlanningQueueGuard.id == 1).with_for_update())
        if guard is None:
            # create_all 的隔离测试库无种子；生产迁移已经插入唯一 guard。
            session.add(PlanningQueueGuard(id=1))
            session.flush()
        # 使用 current read，避免外层事务已建立的 RR 快照漏算等待 guard 期间的新任务。
        live = session.scalars(
            select(PlanningTask.itinerary_id).where(PlanningTask.state.in_(rows.LIVE_STATES)).with_for_update()
        ).all()
        if len(live) >= settings.planning_queue_capacity:
            raise ApiError(429, "行程生成任务已满，系统繁忙，请稍后再试")
        yield


def enqueue(user_id: int, itinerary_id: int, command: GenerateCommand) -> None:
    from app.services.itinerary_generation import GenerateCommand

    payload = TypeAdapter(GenerateCommand).dump_json(command).decode()
    store.open_attempt(user_id, itinerary_id, request_json=payload, queued=True)


def command_for(itinerary_id: int) -> GenerateCommand:
    from app.db.models import ItineraryMain
    from app.services.generation_recovery import rebuild_request
    from app.services.itinerary_generation import GenerateCommand

    with session_scope() as session:
        row = session.get(PlanningTask, itinerary_id)
        if row is not None and row.request_json:
            return TypeAdapter(GenerateCommand).validate_json(row.request_json)
        main = session.get(ItineraryMain, itinerary_id)
        if main is None:
            raise ApiError(404, "行程不存在")
        return rebuild_request(main)
