"""城市名 → 中心坐标：city_geo 字典 → OTM / Nominatim 同名候选兜底（数据面唯一入口）。

为什么独立成模块：取城市坐标时只采纳同名候选；名称规则定义在 core.poi_identity，外部源失败如实未确认。
tools 的 OTM 半径池与 weather 的天气预报都要城市中心，两侧因此都向下依赖本模块——
域化改造前它是 tools 的私有 helper，weather 只能反向 `import tools`（data → tools 倒挂）。
"""

from __future__ import annotations

from app.agent.core.poi_identity import matches_city_name
from app.agent.data import city_reference, places


def city_center(city: str) -> dict | None:
    """城市名 → 中心坐标：city_geo 字典（坐标为空时未填）→ OTM / Nominatim 同名候选兜底。"""
    geo = city_reference.get_city_geo(city)
    try:
        if geo and geo.get("lat") is not None and geo.get("lng") is not None:
            return {"latitude": float(geo["lat"]), "longitude": float(geo["lng"])}
    except (TypeError, ValueError):
        pass
    name = str(city or "").strip()
    hit = places.resolve_city_center(name)
    if not hit or not matches_city_name(name, hit):
        rows = places.geocode_place_rows(name, namedetails=True)
        hit = next((row for row in rows or [] if matches_city_name(name, row)), None)
    if hit:
        return {"latitude": hit["latitude"], "longitude": hit["longitude"]}
    return None


def destination_problem(city: str) -> str | None:
    """成功搜索却没有同名目的地时提示修正；数据源不可用不能当成城市不存在。"""
    if city_reference.get_city_geo(city):
        return None
    hit = places.resolve_city_center(city)
    if hit and matches_city_name(city, hit):
        return None
    rows = places.geocode_place_rows(city, namedetails=True)
    if rows is None or any(matches_city_name(city, row) for row in rows):
        return None
    return f"无法将目的地「{city}」对应到同名城市，请补充国家/地区或更正城市名称"
