"""Durable API handoff and shared SSE; isolated SQLite, no real model calls."""

import asyncio
import json
from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine, func, select, update
from sqlalchemy.orm import sessionmaker

from app.common import cache_store, event_hub, event_publisher, generation_event_log
from app.common.config import settings
from app.common.envelope import ApiError
from app.db import session as db
from app.db.models import Base, ItineraryMain, PlanningTask, SysUser
from app.schemas.business.itinerary import GenerateTripRequest
from app.services import generation_recovery, itinerary_events, itinerary_generation, planning_queue, planning_tasks
from tests.planning_task_probe import simulate_worker_exit


@pytest.fixture
def queue_db(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'dispatch.db'}")
    Base.metadata.create_all(engine)
    db.init_engine(engine, sessionmaker(bind=engine, expire_on_commit=False))
    monkeypatch.setattr(itinerary_generation, "destination_problem", lambda *_: None)
    cache_store.reset_for_tests()
    event_publisher.reset_event_publisher()
    with db.session_scope() as session:
        session.add(SysUser(id=1, username="queue-test", password="example-only-no-login"))
    yield
    planning_tasks.reset()
    event_publisher.reset_event_publisher()
    cache_store.reset_for_tests()
    db.init_engine(None)
    engine.dispose()


def _state(trip: int) -> dict:
    state = planning_tasks.state_of(trip)
    assert state is not None
    return state


def _replay(trip: int, cursor: int):
    result = generation_event_log.read_after(trip, cursor)
    assert result is not None
    return result


def _create() -> int:
    result = itinerary_generation.generate(1, GenerateTripRequest(city="杭州", days=2, regionHint="西湖附近"))
    return int(result["id"])


def test_accept_persists_unowned_task_and_complete_command(queue_db):
    trip = _create()
    planning_tasks.reset()  # API 本地状态全部丢弃，交付责任仍在 DB。
    state = _state(trip)
    assert state and state["state"] == "QUEUED" and state["workerId"] == ""
    assert not planning_tasks.is_active(trip)
    with pytest.raises(ApiError) as duplicate:
        planning_tasks.acquire(1, trip)
    assert duplicate.value.status == 409
    assert planning_queue.command_for(trip).region_hint == "西湖附近"
    limits = planning_tasks.claim_queued()
    assert limits and limits.itinerary_id == trip
    assert planning_tasks.claim_queued() is None
    assert _state(trip)["attempt"] == 1


def test_enqueue_failure_rolls_back_shell_and_all_days(queue_db, monkeypatch):
    enqueue = planning_queue.enqueue

    def fail(*args):
        enqueue(*args)
        raise ApiError(429, "injected dispatch rejection")

    monkeypatch.setattr(planning_queue, "enqueue", fail)
    with pytest.raises(ApiError):
        _create()
    with db.session_scope() as session:
        assert session.scalar(select(func.count()).select_from(ItineraryMain)) == 0
        assert session.scalar(select(func.count()).select_from(PlanningTask)) == 0


def test_fresh_trip_recovers_on_expired_lease_without_idle_delay(queue_db, monkeypatch):
    trip = _create()
    first = planning_tasks.claim_queued()
    assert first
    first.reserve_llm(10, 20)
    planning_tasks.mark_running(trip)
    deadline = _state(trip)["deadlineAt"]
    simulate_worker_exit(trip)
    submitted = []
    monkeypatch.setattr(generation_recovery, "resume_day", lambda *_: None)
    monkeypatch.setattr(itinerary_generation.generation_pool, "submit", lambda *args: submitted.append(args))
    generation_recovery.recover()
    current = _state(trip)
    assert submitted and current["attempt"] == 2
    assert current["deadlineAt"] == deadline and current["consumed"]["consumed_llm_calls"] == 1


def test_expired_queue_is_terminalized_without_model_call(queue_db, monkeypatch):
    trip = _create()
    with db.session_scope() as session:
        session.execute(
            update(PlanningTask)
            .where(PlanningTask.itinerary_id == trip)
            .values(deadline_at=datetime.now() - timedelta(seconds=1))
        )
    monkeypatch.setattr(itinerary_generation.generation_pool, "submit", lambda *_: pytest.fail("expired task ran"))
    assert generation_recovery.recover() == 1
    assert _state(trip)["state"] == "EXPIRED"


def test_shared_replay_survives_process_local_reset_and_closes_once(queue_db):
    trip = _create()
    event_publisher.publish_event(trip, "day_start", {"dayNo": 1})
    event_publisher.publish_event(trip, "complete", {"state": "COMPLETED"})
    event_hub.reset_for_tests()  # 第二个 API 实例没有第一实例的任何本地日志。

    async def receive():
        sub, _ = event_hub.subscribe(trip, "{}")
        return [frame async for frame in itinerary_events.event_frames(trip, sub, last_event_id=0)]

    frames = asyncio.run(receive())
    assert len(frames) == 2
    assert [json.loads(frame.split("data:", 1)[1])["type"] for frame in frames] == ["day_start", "complete"]
    assert frames[0].startswith("id:1\n") and frames[1].startswith("id:2\n")
    assert event_hub.subscriber_count(trip) == 0


def test_shared_replay_reports_retention_gap_and_keeps_monotonic_cursor(queue_db, monkeypatch):
    trip = _create()
    monkeypatch.setattr(generation_event_log, "REPLAY_BUFFER", 2)
    for number in range(4):
        event_publisher.publish_event(trip, "day_start", {"dayNo": number})
    frames, complete, latest = _replay(trip, 0)
    assert not complete and latest == 4 and [frame.event_id for frame in frames] == [3, 4]
    assert _replay(trip, 3)[1]
    assert not _replay(trip, 99)[1]
    with db.session_scope() as session:
        from app.db.models import GenerationEvent

        session.execute(update(GenerationEvent).values(created_at=datetime.now() - timedelta(hours=3)))
    assert generation_event_log.cleanup() == 2
    event_publisher.publish_event(trip, "day_done", {"dayNo": 1})
    assert _replay(trip, 4)[2] == 5


def test_queue_capacity_rejection_leaves_no_shell(queue_db, monkeypatch):
    first = _create()
    monkeypatch.setattr(settings, "planning_queue_capacity", 1)
    with pytest.raises(ApiError) as refused:
        itinerary_generation.generate(1, GenerateTripRequest(city="杭州", days=1), "retry-key")
    assert refused.value.status == 429
    with db.session_scope() as session:
        assert session.scalars(select(ItineraryMain.id)).all() == [first]


def test_new_attempt_does_not_replay_previous_terminal(queue_db):
    trip = _create()
    event_publisher.publish_event(trip, "complete", {"state": "PARTIAL"})
    with db.session_scope() as session:
        session.execute(update(PlanningTask).where(PlanningTask.itinerary_id == trip).values(state="FINISHED"))
    planning_tasks.acquire(1, trip)
    event_publisher.publish_event(trip, "day_start", {"dayNo": 2})
    frames, complete, _ = _replay(trip, 0)
    assert not complete
    assert [json.loads(frame)["type"] for frame in frames] == ["day_start"]


def test_private_events_share_wire_sequence_without_replay_gaps(queue_db):
    trip = _create()
    event_publisher.publish_event(trip, "day_start", {"dayNo": 1})
    private = event_publisher.publish_local(trip, "chat_token", {"delta": "test"})
    event_publisher.publish_event(trip, "day_done", {"dayNo": 1})
    frames, complete, latest = _replay(trip, 0)
    assert complete and latest == 2
    assert [json.loads(frame)["seq"] for frame in frames] == [1, 3]
    assert json.loads(private)["seq"] == 2


def test_explicit_new_attempt_rebuilds_current_requirements(queue_db):
    trip = _create()
    with db.session_scope() as session:
        session.execute(update(PlanningTask).where(PlanningTask.itinerary_id == trip).values(state="FINISHED"))
        session.execute(
            update(ItineraryMain).where(ItineraryMain.id == trip).values(requirements="保留新确认的亲子需求")
        )
    planning_tasks.acquire(1, trip)
    assert planning_queue.command_for(trip).requirements == "保留新确认的亲子需求"


def test_worker_health_requires_recent_successful_poll(queue_db, tmp_path, monkeypatch):
    import os
    import time

    from app.common import worker_health

    path = tmp_path / "worker.heartbeat"
    monkeypatch.setattr(settings, "planning_health_path", str(path))
    assert not worker_health.healthy()
    worker_health.record()
    assert worker_health.healthy()
    stale = time.time() - max(30, settings.itinerary_deadline_seconds + settings.planning_live_seconds) - 1
    os.utime(path, (stale, stale))
    assert not worker_health.healthy()
    worker_health.clear()
    assert not path.exists()


def test_event_transport_does_not_change_itinerary_modified_time(queue_db):
    trip = _create()
    previous = datetime(2026, 1, 1)
    with db.session_scope() as session:
        session.execute(update(ItineraryMain).where(ItineraryMain.id == trip).values(updated_at=previous))
    event_publisher.publish_event(trip, "day_start", {"dayNo": 1})
    event_publisher.publish_local(trip, "chat_token", {"delta": "test"})
    with db.session_scope() as session:
        assert session.scalar(select(ItineraryMain.updated_at).where(ItineraryMain.id == trip)) == previous
