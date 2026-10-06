"""用户偏好（移植自 Java `UserPreferenceService`）。

唯一键 `(user_id, pref_label)`：正向、负向、硬约束三类信号**共用同一条记录**，
所以写信号时必须"读改写"而不是插入新行，否则同一标签会因 negative 不同而重复。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select

from app.common.vo_json import iso_datetime
from app.db.models import UserPreference
from app.db.session import session_scope


def top_preferences(user_id: int, limit: int = 5) -> list[str]:
    safe_limit = max(1, min(limit, 20))
    with session_scope() as session:
        rows = (
            session.execute(
                select(UserPreference)
                .where(UserPreference.user_id == user_id, UserPreference.negative == 0)
                .order_by(UserPreference.count.desc())
                .limit(safe_limit)
            )
            .scalars()
            .all()
        )
        return [row.pref_label for row in rows]


def record_preferences(user_id: int, preferences: list[str] | None) -> None:
    record_signals(user_id, preferences, [], [], "explicit", 1.0)


def record_signals(
    user_id: int,
    explicit: list[str] | None,
    hard: list[str] | None,
    negative: list[str] | None,
    source: str | None,
    confidence: float = 1.0,
) -> None:
    with session_scope() as session:
        _record_one(session, user_id, explicit, source, confidence, False, False)
        _record_one(session, user_id, hard, source, confidence, False, True)
        _record_one(session, user_id, negative, source, confidence, True, False)


def _record_one(session, user_id: int, labels, source, confidence, is_negative: bool, is_hard: bool) -> None:
    if labels is None:
        return
    for raw_label in labels:
        if not raw_label or not raw_label.strip():
            continue
        label = raw_label.strip()
        existing = session.execute(
            select(UserPreference).where(UserPreference.user_id == user_id, UserPreference.pref_label == label)
        ).scalar_one_or_none()
        if existing is None:
            existing = UserPreference(
                user_id=user_id,
                pref_label=label,
                count=0 if is_negative else 1,
                negative=1 if is_negative else 0,
                hard_constraint=1 if is_hard else 0,
            )
            session.add(existing)
        elif not is_negative:
            existing.count = (existing.count or 0) + 1
        existing.negative = 1 if is_negative else 0
        existing.hard_constraint = 1 if (is_hard or existing.hard_constraint == 1) else 0
        existing.source = (source or "").strip() or "explicit"
        existing.confidence = max(0.0, min(1.0, float(confidence)))
        existing.last_seen_at = datetime.now()


def signals(user_id: int, limit: int = 50) -> list[dict]:
    safe_limit = max(1, min(limit, 50))
    with session_scope() as session:
        rows = (
            session.execute(
                select(UserPreference)
                .where(UserPreference.user_id == user_id)
                .order_by(UserPreference.hard_constraint.desc(), UserPreference.updated_at.desc())
                .limit(safe_limit)
            )
            .scalars()
            .all()
        )
    return [
        {
            "label": row.pref_label,
            "count": row.count,
            "source": row.source,
            "confidence": float(row.confidence) if row.confidence is not None else None,
            "negative": row.negative == 1,
            "hardConstraint": row.hard_constraint == 1,
            "lastSeenAt": iso_datetime(row.last_seen_at),
        }
        for row in rows
    ]
