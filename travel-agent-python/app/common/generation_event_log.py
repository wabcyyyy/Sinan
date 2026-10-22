"""MySQL generation event replay, bounded per itinerary and retained for two hours.

The itinerary row serializes sequence allocation and event commit. Readers never advance
past an uncommitted earlier event, even when several processes publish concurrently.
"""

import json
from datetime import datetime, timedelta

from sqlalchemy import delete, select, update

from app.common.event_hub import REPLAY_BUFFER, Envelope
from app.db.models import GenerationEvent, ItineraryMain, PlanningTask
from app.db.session import session_scope


def append(itinerary_id: int, envelope: str) -> Envelope | None:
    with session_scope() as session:
        main = session.scalar(select(ItineraryMain).where(ItineraryMain.id == itinerary_id).with_for_update())
        if main is None:
            return None
        event_id = int(main.generation_event_seq or 0) + 1
        wire_seq = int(main.event_wire_seq or 0) + 1
        session.execute(
            update(ItineraryMain)
            .where(ItineraryMain.id == itinerary_id)
            .values(generation_event_seq=event_id, event_wire_seq=wire_seq, updated_at=ItineraryMain.updated_at)
        )
        payload = json.loads(envelope)
        payload["seq"] = wire_seq
        frame = Envelope(json.dumps(payload, ensure_ascii=False))
        frame.event_id = event_id
        session.add(
            GenerationEvent(
                itinerary_id=itinerary_id, event_id=event_id, envelope=str(frame), created_at=datetime.now()
            )
        )
        session.execute(
            delete(GenerationEvent).where(
                GenerationEvent.itinerary_id == itinerary_id, GenerationEvent.event_id <= event_id - REPLAY_BUFFER
            )
        )
    return frame


def next_seq(itinerary_id: int) -> int | None:
    """私有会话与终态快照共用 wire seq，但不占用生成回放游标。"""
    with session_scope() as session:
        main = session.scalar(select(ItineraryMain).where(ItineraryMain.id == itinerary_id).with_for_update())
        if main is None:
            return None
        value = int(main.event_wire_seq or 0) + 1
        session.execute(
            update(ItineraryMain)
            .where(ItineraryMain.id == itinerary_id)
            .values(event_wire_seq=value, updated_at=ItineraryMain.updated_at)
        )
        return value


def read_after(itinerary_id: int, cursor: int | None) -> tuple[list[Envelope], bool, int] | None:
    with session_scope() as session:
        main = session.get(ItineraryMain, itinerary_id)
        if main is None:
            return None
        latest = int(main.generation_event_seq or 0)
        if cursor is None:
            return [], True, latest
        start = (
            session.scalar(select(PlanningTask.event_start_seq).where(PlanningTask.itinerary_id == itinerary_id)) or 0
        )
        floor = max(cursor, start)
        records = session.scalars(
            select(GenerationEvent)
            .where(GenerationEvent.itinerary_id == itinerary_id, GenerationEvent.event_id > floor)
            .order_by(GenerationEvent.event_id)
        ).all()
        frames = []
        expected = floor + 1
        complete = start <= cursor <= latest
        for record in records:
            complete = complete and record.event_id == expected
            expected = record.event_id + 1
            frame = Envelope(record.envelope)
            frame.event_id = record.event_id
            frames.append(frame)
        return frames, complete and expected == latest + 1, latest


def cleanup() -> int:
    with session_scope() as session:
        result = session.execute(
            delete(GenerationEvent).where(GenerationEvent.created_at < datetime.now() - timedelta(hours=2))
        )
        return int(getattr(result, "rowcount", 0))
