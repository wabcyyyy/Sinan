"""就近分店判定：同一个店名，取离当天行程最近的那家（2026-10-12 P1）。

`existence.resolve_poi` 按 (城市, 名字) 判定并进程级记忆化，连锁店名因此总是落到
OSM 排序最前的那家分店。本模块补一个**带锚点**的查法，供落地阶段把远处分店换成
附近同名分店；判定类型、名称门槛、目的地闸、额度与签票全部复用 `existence`，
这里不另立第二套口径。

依赖：existence（结论类型与判定原语）、data.places（限框点名解析）、core.poi_identity（距离）；
无上层依赖。
"""

from __future__ import annotations

import logging
import math
from typing import Any

from app.agent.core.poi_identity import haversine_m
from app.agent.data import places
from app.agent.grounding import existence

logger = logging.getLogger(__name__)


def resolve_poi_near(
    name: str, city: str, anchors: list[tuple[float, float]], max_distance_m: float
) -> existence.ResolveResult:
    """同名、且离 `anchors`（当天其它已落地点位）最近的那个实体；超出 `max_distance_m` 不收。

    `resolve_poi` 只按 (城市, 名字) 查，连锁店名回的是 OSM 排序最前的分店（实测
    2026-10-12：「くら寿司」落到距新宿酒店 19 km 的町田店，并借走它的 diet:vegan
    标签）。这里只问 Nominatim（唯一能按名直查的免费源），限定在锚点外扩
    `max_distance_m` 的框内，取离最近锚点最近的同名行；名称门槛与目的地闸照旧走
    `pick_row`。不进 `existence._memo`：结论取决于锚点，不是 (城市, 名字) 的稳定属性——
    places 层已按含框的查询键缓存。命中照常签票（票按 external_id 优先查，借不到别家）。
    """
    target = str(name or "").strip()
    provider = existence.provider_registry()["nominatim"]
    if not target or not anchors or not provider.available():
        return existence.ResolveResult.unknown("near_lookup_unavailable")
    if not existence.budget_allows():
        return existence.ResolveResult.unknown("budget_exhausted")
    existence.consume_budget()
    lat_pad = max_distance_m / 111_000
    lng_pad = max_distance_m / (111_000 * max(math.cos(math.radians(anchors[0][0])), 0.1))
    viewbox = (
        min(lat for lat, _ in anchors) - lat_pad,
        min(lng for _, lng in anchors) - lng_pad,
        max(lat for lat, _ in anchors) + lat_pad,
        max(lng for _, lng in anchors) + lng_pad,
    )
    try:
        rows = places.geocode_place_rows(target, city, namedetails=True, viewbox=viewbox)
    except Exception as exc:  # 与 _run_providers 同纪律：provider 故障按未判定
        logger.warning("existence near lookup failed: %s", exc)
        return existence.ResolveResult.unknown("near_lookup_error")

    def _detour(row: dict[str, Any]) -> float:
        # geocode_place_rows 只产出带浮点坐标的行（缺坐标的应答行在解析时已跳过）
        return min(haversine_m(row["latitude"], row["longitude"], a_lat, a_lng) for a_lat, a_lng in anchors)

    near_rows = sorted((row for row in rows or [] if _detour(row) <= max_distance_m), key=_detour)
    if not near_rows:
        return existence.ResolveResult.unknown("no_branch_near_day")
    result = existence.pick_row(
        near_rows, target, city, provider=provider.name, spellings_of=existence.nominatim_spellings
    )
    if result.grounded:
        result = existence.issue_resolve_ticket(result, target, city)
    return result
