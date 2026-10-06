"""条目对/错反馈（SPEC C3.5 反馈回路·记录链路）：upsert 一人一条 / 撤销 / 查本人。

权限口径（SPEC §3 + Q1/Q5/Q6 推荐列）：
- 写（upsert）：读面皆可写——owner/editor/viewer 一律走 `require_main`。旅伴最
  清楚哪天排得离谱，写权限=读权限；防滥用靠登录态，不做频控（私域小流量）；
- 撤销/回显：仅本人数据——行级过滤 user_id，动不了别人的反馈；
- 撤销=硬删行（Q5，不留墓碑）；行程/条目软删后反馈行保留（Q6，eval 需要历史，
  不可见靠投影隔离——本表不进 share/模板/MCP 任何投影）。

反馈与行程详情各自独立读取（同 expense），不动 itinerary:detail 缓存。
"""

from __future__ import annotations

from typing import Any

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.common.envelope import ApiError
from app.common.vo_json import iso_datetime
from app.db.models import ItemFeedback, ItineraryItem
from app.db.session import session_scope
from app.schemas.feedback import FeedbackCreate
from app.services import itinerary_query


def upsert(user_id: int, itinerary_id: int, body: FeedbackCreate) -> dict[str, Any]:
    """一人一条：UNIQUE(item_id, user_id) 上重复提交覆盖旧值（点错场景可改）。"""
    # 复检 dumped 数据，model_construct 绕过校验的直调同样拦下（expense 同款防线）
    try:
        body = FeedbackCreate.model_validate(body.model_dump())
    except ValidationError as exc:
        raise ApiError(400, "反馈参数不合法") from exc
    with session_scope() as session:
        itinerary_query.require_main(session, user_id, itinerary_id)
        _require_item_in_trip(session, itinerary_id, body.itemId)
        row = _find(session, user_id, body.itemId)
        if row is not None:
            _apply(row, body)
            return _vo(row)
        created = ItemFeedback(
            itinerary_id=itinerary_id,
            item_id=body.itemId,
            user_id=user_id,
            value=_db_value(body.value),
            reason=body.reason,
            note=body.note,
        )
        session.add(created)
        try:
            session.flush()
        except IntegrityError as exc:
            # 并发首提撞唯一键：回滚本事务后同会话重查，落成覆盖（upsert 语义不因并发破坏）
            session.rollback()
            row = _find(session, user_id, body.itemId)
            if row is None:
                raise ApiError(409, "反馈提交冲突，请重试") from exc
            _apply(row, body)
            return _vo(row)
        return _vo(created)


def revoke(user_id: int, itinerary_id: int, item_id: int) -> None:
    """撤销自己的反馈：硬删行（Q5）；没有本人反馈行即 404（也动不了别人的）。"""
    with session_scope() as session:
        itinerary_query.require_main(session, user_id, itinerary_id)
        row = _find(session, user_id, item_id, itinerary_id=itinerary_id)
        if row is None:
            raise ApiError(404, "反馈不存在")
        session.delete(row)


def list_mine(user_id: int, itinerary_id: int) -> dict[str, Any]:
    """自己的反馈列表（前端回显勾选态）；展示匿名，不含他人数据。"""
    with session_scope() as session:
        itinerary_query.require_main(session, user_id, itinerary_id)
        rows = session.scalars(
            select(ItemFeedback)
            .where(ItemFeedback.itinerary_id == itinerary_id, ItemFeedback.user_id == user_id)
            .order_by(ItemFeedback.id)
        ).all()
        return {"feedbacks": [_vo(row) for row in rows]}


def _find(session: Session, user_id: int, item_id: int, itinerary_id: int | None = None) -> ItemFeedback | None:
    stmt = select(ItemFeedback).where(ItemFeedback.item_id == item_id, ItemFeedback.user_id == user_id)
    if itinerary_id is not None:
        stmt = stmt.where(ItemFeedback.itinerary_id == itinerary_id)
    return session.scalar(stmt)


def _apply(row: ItemFeedback, body: FeedbackCreate) -> None:
    row.value = _db_value(body.value)
    row.reason = body.reason
    row.note = body.note


def _require_item_in_trip(session: Session, itinerary_id: int, item_id: int) -> None:
    """反馈目标必须是本行程的（未软删）条目；跨行程 item_id 拒绝（expense 同口径 400）。"""
    found = session.scalar(
        select(ItineraryItem.id).where(ItineraryItem.id == item_id, ItineraryItem.itinerary_id == itinerary_id)
    )
    if found is None:
        raise ApiError(400, "行程项不属于该行程")


def _db_value(value: str) -> int:
    return 1 if value == "right" else 0


def _vo(row: ItemFeedback) -> dict[str, Any]:
    return {
        "itemId": row.item_id,
        "value": "right" if row.value else "wrong",
        "reason": row.reason,
        "note": row.note,
        "createdAt": iso_datetime(row.created_at),
        "updatedAt": iso_datetime(row.updated_at),
    }
