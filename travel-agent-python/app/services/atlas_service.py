"""Atlas 行程图鉴聚合（SPEC v2.3 §6.7，S3）。

六步口径（实现按此，不做前端猜测）：
1. 只查 **archived=0** 的本人行程（归档即不入图鉴；本期无 includeArchived 参数）；
2. 逐城取该城全部行程点位的**质心**（仅计非空且非 0/0 的点）→ `coordSource="items"`；
3. 质心不可得 → 回落 `city_geo.lat/lng` → `"geo_fallback"`；
4. 两者皆无 → 进 `unknownCities`（reason="no_coordinates"），**不进 pins、不影响其他城市**；
5. 归国只认 `city_geo`；未命中 → `unknownCities`（reason="dict_miss"）+ `coverage.dictMiss`，
   **禁止默认按 CN 处理**（否则海外行程被悄悄并入中国）；有坐标但未归国的城市仍出钉，
   `country/countryCode` 置 null（两条覆盖度轴分开呈现）；
6. `scope`：visited = endDate < today 且生成完成；planned = 其余（含生成未完成）。
   **纯日期启发式 + 生成状态，不是 GPS 核验**（README 如实标注）。

粒度事实（如实标注）：一行程 = 一个 `city`，无多城市结构 → 跨城行程只归一个钉；
本接口不得暗示「逐城足迹」。
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import date
from decimal import Decimal
from typing import Any, cast

from sqlalchemy import select

from app.common.envelope import ApiError
from app.common.vo_json import iso_date
from app.db.models import CityGeo, ItineraryItem, ItineraryMain
from app.db.session import session_scope

logger = logging.getLogger(__name__)

ALLOWED_SCOPES = ("all", "planned", "visited")


def build_atlas(user_id: int, scope: str | None = None) -> dict[str, Any]:
    normalized = (scope or "all").strip() or "all"
    if normalized not in ALLOWED_SCOPES:
        raise ApiError(400, f"无效的 scope 过滤：{normalized}")

    today = date.today()
    with session_scope() as session:
        mains = (
            session.execute(
                select(ItineraryMain)
                .where(ItineraryMain.user_id == user_id, ItineraryMain.archived.is_(False))
                .order_by(ItineraryMain.id.desc())
            )
            .scalars()
            .all()
        )
        trip_scopes = {main.id: _trip_scope(main, today) for main in mains}
        if normalized != "all":
            mains = [main for main in mains if trip_scopes[main.id] == normalized]

        items: Sequence[ItineraryItem] = []
        if mains:
            items = (
                session.execute(
                    select(ItineraryItem).where(ItineraryItem.itinerary_id.in_([main.id for main in mains]))
                )
                .scalars()
                .all()
            )
        city_rows = {row.city_name: row for row in session.execute(select(CityGeo)).scalars().all()}

    items_by_trip: dict[int, list[ItineraryItem]] = {}
    for item in items:
        items_by_trip.setdefault(item.itinerary_id, []).append(item)
    items_without_coord = sum(1 for item in items if not _valid_coord(item.latitude, item.longitude))

    cities: dict[str, dict[str, Any]] = {}
    for main in mains:
        entry = cities.setdefault(main.city, {"trips": [], "coords": []})
        entry["trips"].append(main)
        for item in items_by_trip.get(main.id, []):
            lat, lon = item.latitude, item.longitude
            if _valid_coord(lat, lon):
                # _valid_coord 已排除 None；cast appease float() 的 ConvertibleToFloat stub
                entry["coords"].append((float(cast(Decimal, lat)), float(cast(Decimal, lon))))

    pins: list[dict[str, Any]] = []
    unknown_cities: list[dict[str, str]] = []
    dict_miss = 0
    for city, entry in cities.items():
        geo = city_rows.get(city)
        if geo is None:
            # 字典未命中必须计数，无论最终有没有坐标（覆盖度的独立一轴）
            dict_miss += 1

        lat = lng = None
        coord_source = None
        if entry["coords"]:
            count = len(entry["coords"])
            lat = round(sum(coord[0] for coord in entry["coords"]) / count, 6)
            lng = round(sum(coord[1] for coord in entry["coords"]) / count, 6)
            coord_source = "items"
        elif geo is not None and geo.lat is not None and geo.lng is not None:
            lat, lng = round(float(geo.lat), 6), round(float(geo.lng), 6)
            coord_source = "geo_fallback"

        if lat is None:
            unknown_cities.append({"city": city, "reason": "no_coordinates"})
            continue
        if geo is None:
            unknown_cities.append({"city": city, "reason": "dict_miss"})

        pins.append(
            {
                "city": city,
                "country": None if geo is None else geo.country,
                "countryCode": None if geo is None else geo.country_code,
                "lat": lat,
                "lng": lng,
                "coordSource": coord_source,
                "tripCount": len(entry["trips"]),
                "trips": [_trip_row(main, trip_scopes[main.id]) for main in entry["trips"]],
            }
        )

    pins.sort(key=lambda pin: (-pin["tripCount"], pin["city"]))
    unknown_cities.sort(key=lambda entry: entry["city"])
    highlight_country_codes = sorted({pin["countryCode"] for pin in pins if pin["countryCode"]})
    return {
        "scope": normalized,
        "stats": {
            "cityCount": len(pins),
            "countryCount": len(highlight_country_codes),
            "tripCount": len(mains),
            "plannedTripCount": sum(1 for m in mains if trip_scopes[m.id] == "planned"),
            "visitedTripCount": sum(1 for m in mains if trip_scopes[m.id] == "visited"),
        },
        "coverage": {
            "tripsTotal": len(mains),
            "pinsRendered": len(pins),
            "itemsWithoutCoord": items_without_coord,
            "dictMiss": dict_miss,
        },
        "highlightCountryCodes": highlight_country_codes,
        "unknownCities": unknown_cities,
        "pins": pins,
    }


def _trip_scope(main: ItineraryMain, today: date) -> str:
    """visited = 结束日在今天之前且生成完成；其余（含生成未完成）归 planned。"""
    if main.end_date is not None and main.end_date < today and main.gen_state == "COMPLETED":
        return "visited"
    return "planned"


def _trip_row(main: ItineraryMain, scope: str) -> dict[str, Any]:
    return {
        "id": main.id,
        "title": main.title,
        "startDate": iso_date(main.start_date),
        "endDate": iso_date(main.end_date),
        "status": main.status,
        "scope": scope,
        "coverUrl": main.cover_url,
        "city": main.city,
    }


def _valid_coord(lat: Decimal | None, lng: Decimal | None) -> bool:
    if lat is None or lng is None:
        return False
    return not (float(lat) == 0.0 and float(lng) == 0.0)
