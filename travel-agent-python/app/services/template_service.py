"""模板发布与 fork（SPEC C2.4）：脱敏投影物化 + 快照复制。

- `publish`（owner）：把脱敏投影物化进 `template_summary` + 打发布时间；源行程
  后续编辑**不影响**已发布模板。
- 投影口径：不含 expense/成员/userId/备注/planNote/逐项花费；花费只以「单均档位
  文案」出现（档位映射在本模块内，预算明细金额不外泄）。
- `fork`：**只读已发布快照**（template_summary），不回读源行程原始行——发布物
  即可见物，item 级备注等私有字段不可能借 fork 绕过脱敏。fork 出的新行程与
  普通行程无差别（可 chat_edit/重排），不含 expense/成员/分享。
"""

from __future__ import annotations

import json
from datetime import datetime
from datetime import time as dt_time
from decimal import Decimal
from typing import Any

from sqlalchemy import select

from app.common.envelope import ApiError
from app.common.vo_json import iso_datetime
from app.db.models import BudgetDetail, ItineraryDay, ItineraryItem, ItineraryMain
from app.db.session import session_scope
from app.services import itinerary_query

FORK_TITLE_PREFIX = "模板:"

#: 花费档位映射（C2.4：档位文案写在投影器内）。key = BudgetDetail.category；
#: 值 = [(单均上限, 文案)]，升序匹配，超出取最后一档。单均 = 类目预算 / item_count。
_TIER_RULES: dict[str, list[tuple[Decimal, str]]] = {
    "门票": [
        (Decimal("0"), "免费参观"),
        (Decimal("50"), "门票 ¥1-50/处"),
        (Decimal("150"), "门票 ¥50-150/处"),
        (Decimal("1e9"), "门票 ¥150+/处"),
    ],
    "餐饮": [
        (Decimal("40"), "餐饮 ¥40 内/餐"),
        (Decimal("100"), "餐饮 ¥40-100/餐"),
        (Decimal("1e9"), "餐饮 ¥100+/餐"),
    ],
    "交通": [
        (Decimal("30"), "交通 ¥30 内/天"),
        (Decimal("80"), "交通 ¥30-80/天"),
        (Decimal("1e9"), "交通 ¥80+/天"),
    ],
    "酒店": [
        (Decimal("300"), "酒店 ¥300 内/晚"),
        (Decimal("800"), "酒店 ¥300-800/晚"),
        (Decimal("1e9"), "酒店 ¥800+/晚"),
    ],
}


def _tier_text(category: str, total: Decimal, unit_count: int) -> dict[str, str] | None:
    rules = _TIER_RULES.get(category)
    if rules is None or total <= 0:
        return None
    unit = total / Decimal(max(unit_count, 1))
    for ceiling, text in rules:
        if unit <= ceiling:
            return {"category": category, "amountRangeText": text}
    return None


def _loads_object(raw: str | None) -> dict[str, Any]:
    if not raw or not raw.strip():
        return {}
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _build_summary(
    main: ItineraryMain,
    days: list[ItineraryDay],
    items: list[ItineraryItem],
    budgets: list[BudgetDetail],
) -> dict[str, Any]:
    items_by_day: dict[int, list[ItineraryItem]] = {}
    for item in items:
        items_by_day.setdefault(item.day_id, []).append(item)

    day_list: list[dict[str, Any]] = []
    for day in sorted(days, key=lambda d: d.day_no):
        day_items = [
            {
                "poiName": item.poi_name,
                "itemType": item.item_type,
                "startTime": item.start_time.isoformat() if item.start_time else None,
                "endTime": item.end_time.isoformat() if item.end_time else None,
                "latitude": float(item.latitude) if item.latitude is not None else None,
                "longitude": float(item.longitude) if item.longitude is not None else None,
            }
            for item in sorted(items_by_day.get(day.id, []), key=lambda i: i.sort_no)
        ]
        day_list.append(
            {
                "dayNo": day.day_no,
                "theme": _loads_object(day.metadata_json).get("theme"),
                "items": day_items,
            }
        )

    cost_tiers = [
        tier
        for budget in budgets
        if (tier := _tier_text(budget.category, budget.amount, budget.item_count)) is not None
    ]
    intro = f"{main.city} {main.days} 日 · {main.persons} 人行程模板"
    if main.trip_theme:
        intro = f"{main.city} {main.days} 日 · {main.trip_theme}"
    return {
        "title": main.title,
        "city": main.city,
        "days": main.days,
        "persons": main.persons,
        "intro": intro,
        "costTiers": cost_tiers,
        "dayList": day_list,
    }


def _require_published(session, template_id: int) -> ItineraryMain:
    main = session.get(ItineraryMain, template_id)
    if main is None or main.template_published_at is None or not main.template_summary:
        raise ApiError(404, "模板不存在")
    return main


def publish(user_id: int, itinerary_id: int) -> dict[str, Any]:
    with session_scope() as session:
        main = itinerary_query.require_owned_main(session, user_id, itinerary_id)
        days = session.scalars(select(ItineraryDay).where(ItineraryDay.itinerary_id == itinerary_id)).all()
        items = session.scalars(select(ItineraryItem).where(ItineraryItem.itinerary_id == itinerary_id)).all()
        budgets = session.scalars(select(BudgetDetail).where(BudgetDetail.itinerary_id == itinerary_id)).all()
        summary = _build_summary(main, list(days), list(items), list(budgets))
        main.template_summary = summary
        main.template_published_at = datetime.now()
        session.flush()
        published_at = main.template_published_at
    itinerary_query.evict_detail(user_id, itinerary_id)
    return {"itineraryId": itinerary_id, "publishedAt": iso_datetime(published_at), "summary": summary}


def template_view(user_id: int, itinerary_id: int) -> dict[str, Any]:
    """owner 视角：已发布→物化快照；未发布→即时构建的预览（不落库）。"""
    with session_scope() as session:
        main = itinerary_query.require_owned_main(session, user_id, itinerary_id)
        if main.template_published_at is not None and main.template_summary:
            published_at = main.template_published_at
            summary: dict[str, Any] = main.template_summary
        else:
            days = session.scalars(select(ItineraryDay).where(ItineraryDay.itinerary_id == itinerary_id)).all()
            items = session.scalars(select(ItineraryItem).where(ItineraryItem.itinerary_id == itinerary_id)).all()
            budgets = session.scalars(select(BudgetDetail).where(BudgetDetail.itinerary_id == itinerary_id)).all()
            published_at = None
            summary = _build_summary(main, list(days), list(items), list(budgets))
    return {"itineraryId": itinerary_id, "publishedAt": iso_datetime(published_at), "summary": summary}


def unpublish(user_id: int, itinerary_id: int) -> None:
    with session_scope() as session:
        main = itinerary_query.require_owned_main(session, user_id, itinerary_id)
        if main.template_published_at is None:
            raise ApiError(404, "模板不存在")
        main.template_published_at = None
        main.template_summary = None
    itinerary_query.evict_detail(user_id, itinerary_id)


def list_templates() -> list[dict[str, Any]]:
    with session_scope() as session:
        rows = session.scalars(
            select(ItineraryMain)
            .where(ItineraryMain.template_published_at.is_not(None), ItineraryMain.template_summary.is_not(None))
            .order_by(ItineraryMain.template_published_at.desc())
        ).all()
        return [_card(row) for row in rows]


def get_template(template_id: int) -> dict[str, Any]:
    with session_scope() as session:
        main = _require_published(session, template_id)
        return {**_card(main), "summary": main.template_summary}


def _card(main: ItineraryMain) -> dict[str, Any]:
    summary = main.template_summary or {}
    return {
        "id": main.id,
        "title": summary.get("title", main.title),
        "city": summary.get("city", main.city),
        "days": summary.get("days", main.days),
        "coverUrl": main.cover_url,
        "costTiers": summary.get("costTiers", []),
        "publishedAt": iso_datetime(main.template_published_at),
    }


def _parse_time(value: Any) -> dt_time | None:
    """快照里的 "HH:MM"/"HH:MM:SS" → time；坏值置 None 而不是让 fork 整体失败。"""
    if not isinstance(value, str) or not value.strip():
        return None
    parts = value.strip().split(":")
    try:
        hour, minute = int(parts[0]), int(parts[1])
        second = int(parts[2]) if len(parts) > 2 else 0
        return dt_time(hour, minute, second)
    except (ValueError, IndexError):
        return None


def fork(user_id: int, template_id: int) -> dict[str, Any]:
    """从已发布快照复制出新行程；源模板零改动，新行程与普通行程无差别。"""
    with session_scope() as session:
        source = _require_published(session, template_id)
        summary: dict[str, Any] = source.template_summary or {}
        title = f"{FORK_TITLE_PREFIX}{summary.get('title', source.title)}"

        copy = ItineraryMain(
            user_id=user_id,
            title=title[:128],
            city=summary.get("city", source.city),
            start_date=source.start_date,
            end_date=source.end_date,
            days=source.days,
            persons=source.persons,
            budget=source.budget,
            hotel_tier=source.hotel_tier,
            stay_nights=source.stay_nights,
            status=2,
            trip_theme=source.trip_theme,
        )
        session.add(copy)
        session.flush()

        for day_summary in summary.get("dayList", []):
            day_no = int(day_summary.get("dayNo") or 0)
            if day_no < 1:
                continue
            new_day = ItineraryDay(
                itinerary_id=copy.id,
                day_no=day_no,
                generation_status="SUCCEEDED",
                metadata_json=json.dumps({"theme": day_summary["theme"]}, ensure_ascii=False)
                if day_summary.get("theme")
                else None,
            )
            session.add(new_day)
            session.flush()
            for sort_no, item_summary in enumerate(day_summary.get("items", [])):
                poi_name = str(item_summary.get("poiName") or "").strip()
                if not poi_name:
                    continue
                session.add(
                    ItineraryItem(
                        day_id=new_day.id,
                        itinerary_id=copy.id,
                        item_type=str(item_summary.get("itemType") or "attraction"),
                        poi_name=poi_name[:128],
                        latitude=item_summary.get("latitude"),
                        longitude=item_summary.get("longitude"),
                        start_time=_parse_time(item_summary.get("startTime")),
                        end_time=_parse_time(item_summary.get("endTime")),
                        sort_no=sort_no,
                        source="template",
                        value_kind="generated",
                    )
                )
        session.flush()
        return {"itineraryId": copy.id, "title": copy.title}
