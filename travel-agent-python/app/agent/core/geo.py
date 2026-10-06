"""地理计算工具：球面距离与最近邻路线排序（无外部依赖的纯函数）。

职责：
- haversine_meters：计算两点间的球面距离（米）；
- nearest_neighbor_order：把一组 POI 按地理邻近串成一条合理的游览顺序。

实现要点：
- 纯计算、无状态，被 generators.fallback_generate 用来排布每日景点路线，
  让相邻景点之间移动距离尽量短。
"""

import math
from collections.abc import Iterable


def haversine_meters(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def nearest_neighbor_order(pois: Iterable[dict]) -> list[dict]:
    items = [p for p in pois if p.get("latitude") is not None and p.get("longitude") is not None]
    if not items:
        return []
    ordered: list[dict] = [items[0]]
    remaining = list(items[1:])
    while remaining:
        cur = ordered[-1]
        idx = min(
            range(len(remaining)),
            key=lambda i: haversine_meters(
                float(cur["latitude"]),
                float(cur["longitude"]),
                float(remaining[i]["latitude"]),
                float(remaining[i]["longitude"]),
            ),
        )
        ordered.append(remaining.pop(idx))
    return ordered
