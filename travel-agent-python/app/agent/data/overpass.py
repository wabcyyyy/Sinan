"""Overpass：按 OSM 标签检索（Nominatim 做不了标签过滤）。

与 Nominatim 点名解析的分工：地理编码答"这个名字指哪儿"，答不了"城里哪些店
带饮食标签"（实测 bracket 过滤语法基本无效）。饮食证据的检索面由这里承担：
`diet:vegetarian` / `diet:vegan` 的 `yes|only` 实体 → 与 `places.geocode_place_rows`
同形的证据行（dietary_options / category_tags / place_id / source_url），
消费方零分叉。行归一复用 `places._osm_facts`（同一套 OSM 标签口径）。

数据边界：OSM 标签是志愿者贡献，快餐连锁也可能带 diet 标签——本模块只负责
"真实取回了标签"，不判定菜单适用性；`restaurant` 行排前只是展示顺序先验。

依赖：common（external_client/http_client/config）、places（行归一）；无上层依赖。
"""

from __future__ import annotations

from typing import Any

from app.agent.data.places import _osm_facts
from app.common.config import settings
from app.common.external_client import ExternalClient, fetch_json
from app.common.http_client import api_client, contact_user_agent

_BASE = "https://overpass-api.de/api/interpreter"
_UA = contact_user_agent()
# 半径检索窗口（城市尺度）：与 OTM 半径池同一量级（12km 半径 ≈ ±0.13° 纬度）。
_SPAN_DEG = 0.13

# Overpass 偶发 504：免费公共实例，负缓存要短、重试 1 次（同 OTM radius 纪律）。
_client: ExternalClient = ExternalClient(
    name="overpass",
    ttl_seconds=6 * 3600,
    negative_ttl_seconds=600,
    timeout_seconds=max(settings.places_timeout_seconds, 25.0),
    min_interval_seconds=1.1,
    retry_attempts=1,
)


def search_diet_tagged_places(city: str, diet: str, limit: int = 8) -> list[dict[str, Any]]:
    """按 OSM diet 标签检索城市周边餐饮；未启用/失败/无中心点返回空。"""
    if not settings.overpass_enabled or diet not in ("vegetarian", "vegan"):
        return []
    payload = _fetch(city, diet, limit)
    if not isinstance(payload, dict) or not isinstance(payload.get("elements"), list):
        return []
    rows = [row for element in payload["elements"][: max(int(limit), 1)] if (row := _normalize(element))]
    # 正餐店优先的确定性排序：大范围 bbox 会捞进带 diet 标签的快餐连锁（OSM
    # 标签本身仍原样透传、不增不减），restaurant 行排前只是展示顺序先验。
    rows.sort(key=lambda row: 0 if row.get("category_tags", {}).get("amenity") == "restaurant" else 1)
    return rows


def _fetch(city: str, diet: str, limit: int) -> Any:
    # 延迟导入：city_center 模块反向依赖 places，places→本模块（经 impl）后
    # 顶层 import 会在补初始化序上多一个环。
    from app.agent.data.city_center import city_center

    center = city_center(city)
    if center is None:
        return None
    bbox = (
        center["latitude"] - _SPAN_DEG,
        center["longitude"] - _SPAN_DEG * 1.6,
        center["latitude"] + _SPAN_DEG,
        center["longitude"] + _SPAN_DEG * 1.6,
    )
    query = (
        f"[out:json][timeout:25];"
        f'nwr["diet:{diet}"~"^(yes|only)$"]["name"]'
        f"({bbox[0]:.4f},{bbox[1]:.4f},{bbox[2]:.4f},{bbox[3]:.4f});"
        f"out center {max(int(limit), 1)};"
    )
    return _client.call(
        f"diet:{diet}:{city}",
        lambda: fetch_json(_client, api_client(), _BASE, params={"data": query}, headers={"User-Agent": _UA}),
    )


def _normalize(element: Any) -> dict[str, Any] | None:
    """Overpass element → 证据行；无名/无坐标的行返回 None（进不了票与名称门槛）。"""
    if not isinstance(element, dict):
        return None
    raw_tags = element.get("tags")
    tags: dict[str, Any] = raw_tags if isinstance(raw_tags, dict) else {}
    name = str(tags.get("name") or "").strip()
    if not name:
        return None
    osm_type = str(element.get("type") or "")
    osm_id = str(element.get("id") or "")
    raw_center = element.get("center")
    point: dict[str, Any] = raw_center if isinstance(raw_center, dict) else element
    try:
        lat, lng = float(point["lat"]), float(point["lon"])
    except (TypeError, ValueError, KeyError):
        return None
    main_key = "amenity" if tags.get("amenity") else ("shop" if tags.get("shop") else "")
    pseudo = {
        "osm_type": osm_type,
        "osm_id": osm_id if osm_id.isdigit() else None,
        "extratags": tags,
        "category": main_key or "amenity",
        "type": str(tags.get(main_key) or "restaurant") if main_key else "restaurant",
    }
    return {
        "name": name,
        "id": f"osm:{osm_type}:{osm_id}" if osm_id else name,
        "display_name": "、".join(
            part for part in (name, str(tags.get("addr:street") or ""), str(tags.get("addr:city") or "")) if part
        ),
        "latitude": lat,
        "longitude": lng,
        "aliases": [alias for alias in (str(tags.get("alt_name") or ""), str(tags.get("name:en") or "")) if alias],
        "localized_name": None,
        "source": "overpass",
        **_osm_facts(pseudo),
    }
