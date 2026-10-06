"""城市名 → 中心坐标：city_geo 字典 → Nominatim 兜底（数据面唯一入口）。

为什么独立成模块：这条链只做"取坐标"（读城市字典、必要时问 Nominatim），不下结论。
tools 的 OTM 半径池与 weather 的天气预报都要城市中心，两侧因此都向下依赖本模块——
域化改造前它是 tools 的私有 helper，weather 只能反向 `import tools`（data → tools 倒挂）。
"""

from __future__ import annotations

from app.agent.data import city_reference, places


def city_center(city: str) -> dict | None:
    """城市名 → 中心坐标：city_geo 字典（坐标为空时未填）→ Nominatim 兜底。"""
    geo = city_reference.get_city_geo(city)
    try:
        if geo and geo.get("lat") is not None and geo.get("lng") is not None:
            return {"latitude": float(geo["lat"]), "longitude": float(geo["lng"])}
    except (TypeError, ValueError):
        pass
    hit = places.geocode_place(str(city or "").strip())
    if hit:
        return {"latitude": hit["latitude"], "longitude": hit["longitude"]}
    return None
