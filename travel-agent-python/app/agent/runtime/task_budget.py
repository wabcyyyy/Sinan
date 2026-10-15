"""Durable itinerary budgets share the checkpoint SQLite database."""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from contextlib import closing
from dataclasses import fields
from pathlib import Path

from app.agent.runtime.run_limits import RunLimits
from app.common.config import settings


def _save(itinerary_id: int, snapshot: dict) -> None:
    Path(settings.checkpoint_db_path).parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(settings.checkpoint_db_path, timeout=5)) as connection, connection:
        connection.execute(
            "CREATE TABLE IF NOT EXISTS task_budgets (itinerary_id INTEGER PRIMARY KEY, payload TEXT NOT NULL)"
        )
        connection.execute(
            "INSERT INTO task_budgets VALUES (?, ?) ON CONFLICT(itinerary_id) DO UPDATE SET payload=excluded.payload",
            (itinerary_id, json.dumps(snapshot)),
        )


def new_task_budget(user_id: int, itinerary_id: int) -> RunLimits:
    limits = RunLimits(
        task_id=uuid.uuid4().hex,
        user_id=user_id,
        itinerary_id=itinerary_id,
        deadline_seconds=settings.itinerary_deadline_seconds,
    )
    limits.persist = lambda snapshot: _save(itinerary_id, snapshot)
    limits.persist(limits.snapshot())
    return limits


def load_task_budget(itinerary_id: int) -> RunLimits | None:
    if not Path(settings.checkpoint_db_path).exists():
        return None
    with closing(sqlite3.connect(settings.checkpoint_db_path, timeout=5)) as connection:
        exists = connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='task_budgets'"
        ).fetchone()
        if exists is None:
            return None
        row = connection.execute("SELECT payload FROM task_budgets WHERE itinerary_id=?", (itinerary_id,)).fetchone()
    if row is None:
        return None
    try:
        data = json.loads(row[0])
    except (ValueError, TypeError):
        return None
    required = {
        "task_id",
        "user_id",
        "itinerary_id",
        "submitted_at",
        "deadline_seconds",
        "llm_calls",
        "prompt_tokens",
        "completion_tokens",
        "retrievals",
        "research_calls",
    }
    if not isinstance(data, dict) or not required.issubset(data) or not data["task_id"]:
        return None
    if data["itinerary_id"] != itinerary_id:
        return None
    allowed = {item.name for item in fields(RunLimits) if not item.name.startswith("_")} - {"persist", "started_at"}
    try:
        limits = RunLimits(**{key: value for key, value in data.items() if key in allowed})
        if limits.deadline_seconds <= 0 or limits.submitted_at > time.time():
            return None
    except (TypeError, ValueError):
        return None
    limits.started_at = time.monotonic() - max(0, time.time() - limits.submitted_at)
    limits.persist = lambda snapshot: _save(itinerary_id, snapshot)
    return limits
