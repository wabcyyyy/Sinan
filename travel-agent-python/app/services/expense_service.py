"""Owner-only expense CRUD; associations and writes share one transaction.

Amounts remain Decimal until wire serialization. Totals are summed in Python to
avoid SQLite floating-point SUM, always grouped by currency and category.
Expense data is served separately from itinerary detail/share/version snapshots.
"""

from __future__ import annotations

from collections import defaultdict
from decimal import Decimal
from typing import Any

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.common.envelope import ApiError
from app.common.vo_json import iso_date, iso_datetime
from app.db.models import Expense, ItineraryDay, ItineraryItem
from app.db.session import session_scope
from app.schemas.expense import ExpenseCreate, ExpenseUpdate
from app.services import itinerary_query

_COLUMN_BY_FIELD = {
    "category": "category",
    "amount": "amount",
    "currency": "currency",
    "dayNo": "day_no",
    "itemId": "item_id",
    "spentAt": "spent_at",
    "paymentMethod": "payment_method",
    "note": "note",
}


def create(user_id: int, itinerary_id: int, body: ExpenseCreate) -> dict[str, Any]:
    # Revalidate dumped data so model_construct/mutated direct callers cannot
    # bypass the schema's monetary constraints. Never round invalid input.
    try:
        body = ExpenseCreate.model_validate(body.model_dump())
    except ValidationError as exc:
        raise ApiError(400, "记账参数不合法") from exc
    with session_scope() as session:
        itinerary_query.require_writable_main(session, user_id, itinerary_id)
        _validate_associations(session, itinerary_id, body.dayNo, body.itemId)
        row = Expense(
            itinerary_id=itinerary_id,
            user_id=user_id,
            **{column: getattr(body, field) for field, column in _COLUMN_BY_FIELD.items()},
        )
        session.add(row)
        session.flush()
        vo = _vo(row)
    itinerary_query.evict_detail(user_id, itinerary_id)
    return vo


def update(user_id: int, expense_id: int, body: ExpenseUpdate) -> dict[str, Any]:
    try:
        body = ExpenseUpdate.model_validate(body.model_dump(exclude_unset=True))
    except ValidationError as exc:
        raise ApiError(400, "记账参数不合法") from exc
    with session_scope() as session:
        row = _require_expense(session, user_id, expense_id)
        supplied = body.model_fields_set
        day_no = body.dayNo if "dayNo" in supplied else row.day_no
        item_id = body.itemId if "itemId" in supplied else row.item_id
        _validate_associations(session, row.itinerary_id, day_no, item_id)
        for field, column in _COLUMN_BY_FIELD.items():
            if field in supplied:
                setattr(row, column, getattr(body, field))
        session.flush()
        vo = _vo(row)
        itinerary_id = row.itinerary_id
    itinerary_query.evict_detail(user_id, itinerary_id)
    return vo


def delete(user_id: int, expense_id: int) -> None:
    with session_scope() as session:
        row = _require_expense(session, user_id, expense_id)
        row.deleted = 1
        itinerary_id = row.itinerary_id
    itinerary_query.evict_detail(user_id, itinerary_id)


def list_expenses(user_id: int, itinerary_id: int) -> dict[str, Any]:
    with session_scope() as session:
        itinerary_query.require_main(session, user_id, itinerary_id)
        rows = session.scalars(
            select(Expense).where(Expense.itinerary_id == itinerary_id).order_by(Expense.created_at, Expense.id)
        ).all()
        sums: dict[tuple[str, str], Decimal] = defaultdict(lambda: Decimal("0.00"))
        for row in rows:
            sums[(row.currency, row.category)] += row.amount
        totals = [
            {"category": category, "currency": currency, "amount": f"{total:.2f}"}
            for (currency, category), total in sorted(sums.items())
        ]
        return {"expenses": [_vo(row) for row in rows], "totals": totals}


def _validate_associations(session: Session, itinerary_id: int, day_no: int | None, item_id: int | None) -> None:
    if day_no is not None:
        day = session.scalar(
            select(ItineraryDay).where(ItineraryDay.itinerary_id == itinerary_id, ItineraryDay.day_no == day_no)
        )
        if day is None:
            raise ApiError(400, "关联日期不属于该行程")
    if item_id is None:
        return
    item_day_no = session.scalar(
        select(ItineraryDay.day_no)
        .join(ItineraryItem, ItineraryItem.day_id == ItineraryDay.id)
        .where(
            ItineraryItem.id == item_id,
            ItineraryItem.itinerary_id == itinerary_id,
            ItineraryDay.itinerary_id == itinerary_id,
        )
    )
    if item_day_no is None:
        raise ApiError(400, "关联行程项不属于该行程")
    if day_no is not None and item_day_no != day_no:
        raise ApiError(400, "关联行程项与日期不一致")


def _require_expense(session: Session, user_id: int, expense_id: int) -> Expense:
    """改/删账目（写路径，SPEC C2.3）：行程闸门 owner/editor；账目行 owner 全权、
    editor 只能动自己记的账（row.user_id 即记账人）。"""
    row = session.scalar(select(Expense).where(Expense.id == expense_id))
    if row is None:
        raise ApiError(404, "账目不存在")
    main = itinerary_query.require_writable_main(session, user_id, row.itinerary_id)
    if row.user_id != user_id and main.user_id != user_id:
        raise ApiError(404, "账目不存在")
    return row


def _vo(row: Expense) -> dict[str, Any]:
    return {
        "id": row.id,
        "itineraryId": row.itinerary_id,
        "userId": row.user_id,
        "category": row.category,
        "amount": f"{row.amount:.2f}",
        "currency": row.currency,
        "dayNo": row.day_no,
        "itemId": row.item_id,
        "spentAt": iso_date(row.spent_at),
        "paymentMethod": row.payment_method,
        "note": row.note,
        "createdAt": iso_datetime(row.created_at),
    }
