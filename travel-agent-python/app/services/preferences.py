"""用户偏好（移植自 Java `UserPreferenceService`）。

唯一键 `(user_id, pref_label)`：正向、负向、硬约束三类信号**共用同一条记录**，
所以写信号时必须"读改写"而不是插入新行，否则同一标签会因 negative 不同而重复。

显式长期记忆（个人 Agent V1，规格 §3.1）：同一张表里 source="explicit_memory" 的行
是「长期记住」入口管理的集合；历史累计标签（其他 source）只有被用户显式保存后才
进入该集合。普通建壳不再自动写入长期记忆（原 record_preferences 随规格 §3.1.3 移除）。
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import select

from app.common.envelope import ApiError
from app.common.vo_json import iso_datetime
from app.db.models import UserPreference
from app.db.session import session_scope

#: 长期记忆入口的唯一 source 值（规格 §3.1.2）
MEMORY_SOURCE = "explicit_memory"
#: 每项 trim 后 1–32 字符；正负合计最多 20 项（规格 §3.1.2）
_LABEL_MAX_CHARS = 32
_LABEL_MAX_TOTAL = 20


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
        existing.confidence = Decimal(str(max(0.0, min(1.0, float(confidence)))))
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


# ---- 显式长期记忆（规格 §3.1）：只管 source="explicit_memory" 的行 ----


def memory_labels(user_id: int) -> dict:
    """GET /preferences/memory：只读长期记忆入口的正负标签。

    历史累计标签（其他 source）**不冒充**长期记忆：只有被用户显式保存
    （save_memory 落为 MEMORY_SOURCE）后才出现在这里。
    """
    with session_scope() as session:
        rows = (
            session.execute(
                select(UserPreference).where(UserPreference.user_id == user_id, UserPreference.source == MEMORY_SOURCE)
            )
            .scalars()
            .all()
        )
    return _memory_view(rows)


def save_memory(user_id: int, positive: list[str] | None, negative: list[str] | None) -> dict:
    """PUT /preferences/memory：原子替换用户显式记忆集合（规格 §3.1.4-§3.1.6）。

    - 同名保存直接更新现有行的 source/negative，不插重复行（唯一键语义）；
    - 集合中被移除的 explicit_memory 行删除；**不**删除/改写其他来源标签；
    - 空集合 = 遗忘此入口管理的全部记忆；
    - 只动行上的 source/negative，不追溯修改已创建的行程（hard_constraint 等历史
      语义保持原样，也不把负向标签自动升级为硬约束）。
    """
    pos = _clean_memory_labels(positive)
    neg = _clean_memory_labels(negative)
    overlap = sorted(set(pos) & set(neg))
    if overlap:
        raise ApiError(400, f"同一标签不能同时正向和负向：{'、'.join(overlap)}")
    if len(pos) + len(neg) > _LABEL_MAX_TOTAL:
        raise ApiError(400, f"长期记忆正负合计最多 {_LABEL_MAX_TOTAL} 项")
    with session_scope() as session:
        rows = (
            session.execute(
                select(UserPreference).where(UserPreference.user_id == user_id, UserPreference.source == MEMORY_SOURCE)
            )
            .scalars()
            .all()
        )
        by_label = {row.pref_label: row for row in rows}
        keep = set(pos) | set(neg)
        for row in rows:
            if row.pref_label not in keep:
                session.delete(row)
        for label, is_negative in [*[(item, False) for item in pos], *[(item, True) for item in neg]]:
            row = by_label.get(label)
            if row is None:
                # 唯一键 (user_id, pref_label)：同名历史行（其他 source）不在此——
                # 显式保存同名历史标签时它不在 explicit_memory 集合里，会走到插入分支
                # 并撞唯一键；所以先按唯一键全量查一次，命中即就地转正（更新 source）
                row = session.execute(
                    select(UserPreference).where(UserPreference.user_id == user_id, UserPreference.pref_label == label)
                ).scalar_one_or_none()
                if row is None:
                    row = UserPreference(user_id=user_id, pref_label=label, count=1)
                    session.add(row)
            row.source = MEMORY_SOURCE
            row.negative = 1 if is_negative else 0
            row.last_seen_at = datetime.now()
    return memory_labels(user_id)


def _memory_view(rows) -> dict:
    positive = sorted(row.pref_label for row in rows if row.negative != 1)
    negative = sorted(row.pref_label for row in rows if row.negative == 1)
    return {"positive": positive, "negative": negative}


def _clean_memory_labels(labels: list[str] | None) -> list[str]:
    """trim、去空、去重保序；超长 400（规格 §3.1.2：每项 trim 后 1–32 字符）。"""
    cleaned: dict[str, None] = {}
    for raw in labels or []:
        label = (raw or "").strip()
        if not label:
            continue
        if len(label) > _LABEL_MAX_CHARS:
            raise ApiError(400, f"偏好标签最长 {_LABEL_MAX_CHARS} 个字符：{label}")
        cleaned.setdefault(label, None)
    return list(cleaned)
