"""只从可见的公开投影复制；不回读作者的私人设置或绑定关系。"""

import json
from datetime import time
from typing import Any

from app.common.envelope import ApiError
from app.db.models import ItineraryDay, ItineraryItem, ItineraryMain
from app.db.session import session_scope

FORK_TITLE_PREFIX = "模板:"


def _parse_time(value: Any) -> time | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return time.fromisoformat(value.strip())
    except ValueError:
        return None


def copy_summary(user_id: int, summary: dict[str, Any]) -> dict[str, Any]:
    """复制公开快照为个人路线，日期和费用留给新所有者设置。"""
    days = summary.get("dayList") or []
    if not days or not all(day.get("items") for day in days):
        raise ApiError(400, "这份公开行程尚无完整路线，暂时不能复制")
    with session_scope() as session:
        copy = ItineraryMain(
            user_id=user_id,
            title=f"{FORK_TITLE_PREFIX}{summary['title']}"[:128],
            city=summary["city"],
            days=len(days),
            persons=summary.get("persons") or 1,
            stay_nights=max(len(days) - 1, 0),
            status=2,
            gen_state="COMPLETED",
            cover_url=summary.get("coverUrl"),
            plan_note="由公开规划复制，尚未按你的出行日期核验。未继承作者的私人日期、预算与预订；住宿请自行选择。",
        )
        session.add(copy)
        session.flush()
        for day_no, day in enumerate(days, 1):
            new_day = ItineraryDay(
                itinerary_id=copy.id,
                day_no=day_no,
                generation_status="SUCCEEDED",
                metadata_json=json.dumps({"theme": day.get("theme")}, ensure_ascii=False),
            )
            session.add(new_day)
            session.flush()
            for sort_no, item in enumerate(day.get("items") or []):
                session.add(
                    ItineraryItem(
                        day_id=new_day.id,
                        itinerary_id=copy.id,
                        item_type=item.get("itemType") or "attraction",
                        poi_name=str(item["poiName"])[:128],
                        address=item.get("address"),
                        latitude=item.get("latitude"),
                        longitude=item.get("longitude"),
                        start_time=_parse_time(item.get("startTime")),
                        end_time=_parse_time(item.get("endTime")),
                        remark=item.get("tip"),
                        sort_no=sort_no,
                        source="template",
                        value_kind="estimated",
                        verification_status="unverified",
                        freshness_status="unknown",
                        review_requirement="before_departure",
                    )
                )
        session.flush()
        return {"itineraryId": copy.id, "title": copy.title}
