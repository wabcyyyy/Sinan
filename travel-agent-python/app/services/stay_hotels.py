"""住宿晚次的落库兜底：沿用已有选择，补齐晚次并消除生成时的擅自换店。"""

import json
from typing import Any

from sqlalchemy import select

from app.agent import spread_hotels, sync_schedule_summary
from app.common.vo_json import parse_time_safe
from app.db.models import ItineraryDay, ItineraryItem
from app.db.session import session_scope
from app.schemas.trip import DailyPlan, TripItem
from app.services import itinerary_query


def fill_day_hotel(session: Any, itinerary_id: int, day_no: int, nights: int, plan: DailyPlan) -> None:
    """重生成清旧项前沿用已有住宿，避免唯一酒店证据随覆盖被软删。"""
    if day_no > nights:
        return
    source = session.execute(
        select(ItineraryItem)
        .join(ItineraryDay, ItineraryDay.id == ItineraryItem.day_id)
        .where(
            ItineraryItem.itinerary_id == itinerary_id,
            ItineraryItem.deleted == 0,
            ItineraryItem.item_type == "hotel",
            ItineraryDay.day_no <= nights,
        )
        .order_by(ItineraryDay.day_no, ItineraryItem.sort_no)
        .limit(1)
    ).scalar_one_or_none()
    if source is None:
        return
    raw = plan.model_dump()
    source_wire = itinerary_query.item_vo(source, {})
    source_wire["factEvidence"] = source_wire.get("factEvidence") or {}
    source_item = TripItem.model_validate(source_wire).model_dump()
    if spread_hotels([{"day_no": 0, "items": [source_item]}, raw], nights):
        normalized = DailyPlan.model_validate(raw)
        for field in DailyPlan.model_fields:
            setattr(plan, field, getattr(normalized, field))


def spread_stay_hotels(itinerary_id: int, nights: int) -> int:
    """全链路终态前摊铺缺失晚次；也覆盖首晚漏排、后来才选出酒店的情形。

    只处理已交付的天；住宿实体、价格、来源、证据一起沿用，重复入住软删。
    """
    if nights <= 0:
        return 0
    with session_scope() as session:
        days = (
            session.execute(
                select(ItineraryDay)
                .where(ItineraryDay.itinerary_id == itinerary_id, ItineraryDay.generation_status == "SUCCEEDED")
                .where(ItineraryDay.day_no <= nights)
                .order_by(ItineraryDay.day_no)
            )
            .scalars()
            .all()
        )
        rows = (
            session.execute(
                select(ItineraryItem)
                .where(ItineraryItem.itinerary_id == itinerary_id, ItineraryItem.deleted == 0)
                .order_by(ItineraryItem.sort_no)
            )
            .scalars()
            .all()
        )
        plans = [
            {
                "day_no": day.day_no,
                "items": [
                    {
                        column.name: getattr(row, column.name)
                        for column in ItineraryItem.__table__.columns
                        if column.name not in {"id", "day_id", "itinerary_id", "created_at", "updated_at", "deleted"}
                    }
                    for row in rows
                    if row.day_id == day.id
                ],
            }
            for day in days
        ]
        changed = spread_hotels(plans, nights)
        if not changed:
            return 0
        for day, plan in zip(days, plans, strict=True):
            for item in plan["items"]:
                for field in ("start_time", "end_time"):
                    item[field] = parse_time_safe(item.get(field))
            hotels = [row for row in rows if row.day_id == day.id and row.item_type == "hotel"]
            hotel = next((item for item in plan["items"] if item["item_type"] == "hotel"), None)
            if hotel is None:
                continue
            if hotels:
                for field, value in hotel.items():
                    setattr(hotels[0], field, value)
                for extra in hotels[1:]:
                    extra.deleted = 1
            else:
                session.add(ItineraryItem(**{**hotel, "itinerary_id": itinerary_id, "day_id": day.id}))
            nonhotels = iter(row for row in rows if row.day_id == day.id and row.item_type != "hotel")
            for sort_no, raw in enumerate(plan["items"]):
                if raw["item_type"] != "hotel":
                    next(nonhotels).sort_no = sort_no
            _sync_day_summary(day, plan)
        return changed


def _sync_day_summary(day: ItineraryDay, plan: dict) -> None:
    """首晚漏排、后续才选出酒店时，终态摊铺也必须同步已落库概述。"""
    items = []
    for raw in plan["items"]:
        wire = itinerary_query.item_vo(ItineraryItem(**raw), {})
        wire["factEvidence"] = wire.get("factEvidence") or {}
        items.append(TripItem.model_validate(wire))
    summary = DailyPlan(day_no=day.day_no, items=items)
    sync_schedule_summary(summary)
    try:
        metadata = json.loads(day.metadata_json or "{}")
    except (ValueError, TypeError):
        metadata = {}
    if not isinstance(metadata, dict):
        metadata = {}
    metadata["theme"] = summary.theme
    day.metadata_json = json.dumps(metadata, ensure_ascii=False)
    day.note = summary.note
