"""地图核实深链：把"这个事实是否还成立"交给地图 App 的活数据（D1-D10 统一口径）。

从 `places.py` 拆出（G4 之后 places 只管"取外部数据"，本模块只管"把点指给人看"）：
纯函数拼 URL，无 key 无网络。国内高德、海外谷歌；坐标先过有效性谓词并按
GCJ-02 换算，无/无效坐标一律退化成关键词搜索链接——**不给假位置**。

搜索/路线深链目前是后端单端实现：React 前端（2026-10-04 起）不再拼语义深链，
只按 pin 出高德 marker / 谷歌查询两条固定「核实」链接（经 shared/map-link 白名单）。
共享 fixture tests/golden/deeplink_cases.json 仍双向钉住两侧口径
（tests/test_deeplink_parity.py +
travel-frontend-react/src/react-app/itinerary/deeplink-parity.test.ts），
机制性差异（前端无有效性谓词/无路线深链）见 tests/golden/deeplink_parity.md 差异表。

依赖：app.agent.data.city_reference（city_geo 城市字典的国内判定唯一入口）。
"""

import math
from typing import Any
from urllib.parse import quote

from app.agent.data import city_reference

_AMAP_SRC = "travel-assistant"


def _parse_coords(latitude: Any, longitude: Any) -> tuple[float, float] | None:
    """坐标有效性谓词（原与前端 geo.hasValidCoordinates 同语义；前端 React 端
    已无此谓词，差异登记在 deeplink_parity.md 差异表 R2）：
    有限值 + 经纬度范围 + 非 0/0 哨兵；有效则返回 (lat, lon)，无效返回 None。
    判定与打点共用同一谓词，杜绝"同一坐标两处口径"。
    """
    try:
        lat, lon = float(latitude), float(longitude)
    except (TypeError, ValueError):
        return None
    if not (math.isfinite(lat) and math.isfinite(lon)):
        return None
    if abs(lat) > 90 or abs(lon) > 180 or (lat == 0 and lon == 0):
        return None
    return lat, lon


def _in_china(latitude: float, longitude: float) -> bool:
    return 18.0 <= latitude <= 54.0 and 73.0 <= longitude <= 135.0


def _dict_domestic(city: str) -> bool | None:
    """city_geo.is_domestic 判定，走 `city_reference.get_city_geo` 这个城市字典唯一入口。

    未收录 / 城市为空 / 库不可用一律返回 None，由调用方决定默认——统一默认海外：
    谷歌链接对国内点只是体验次优，高德链接对海外点则是错误国家，两种错误不对称。

    此前这里自己开 `session_scope` 裸查 `city_geo`，是 agent 层唯一一处直连业务库的
    读路径，也是同一张表的第三种读法（`city_reference` 走 db_pool、services 走 ORM），
    三份各自的失败语义（None / False / 吞异常）互不一致（单一真源）。
    """
    name = str(city or "").strip()[:32]
    if not name:
        return None
    row = city_reference.get_city_geo(name)
    if row is None:
        return None
    return bool(row.get("is_domestic"))


def _is_domestic(latitude: Any, longitude: Any, city: str) -> bool:
    """坐标优先（国界框，坐标先过有效性谓词）；无/无效坐标查 city_geo 字典。"""
    coords = _parse_coords(latitude, longitude)
    if coords is not None:
        return _in_china(*coords)
    return bool(_dict_domestic(city))


def to_gcj02(latitude: float, longitude: float) -> tuple[float, float]:
    """WGS-84 → GCJ-02（高德 URI 按 GCJ-02 解释坐标，直传 WGS-84 会偏数百米）。

    与前端 src/utils/coordinates.ts 的 toGcj02 同一公式（两侧测试各钉已知值，
    改一侧必须同步另一侧）；中国境外原样返回。
    """
    if longitude < 72.004 or longitude > 137.8347 or latitude < 0.8293 or latitude > 55.8271:
        return latitude, longitude
    x = longitude - 105
    y = latitude - 35
    pi = math.pi

    def _wave(v: float, a: float, b: float) -> float:
        return (a * math.sin(v * pi) + b * math.sin(v / 3 * pi)) * 2 / 3

    common = (20 * math.sin(6 * x * pi) + 20 * math.sin(2 * x * pi)) * 2 / 3
    lat_offset = -100 + 2 * x + 3 * y + 0.2 * y * y + 0.1 * x * y + 0.2 * math.sqrt(abs(x))
    lat_offset += common + _wave(y, 20, 40) + (160 * math.sin(y / 12 * pi) + 320 * math.sin(y * pi / 30)) * 2 / 3
    lon_offset = 300 + x + 2 * y + 0.1 * x * x + 0.1 * x * y + 0.1 * math.sqrt(abs(x))
    lon_offset += common + _wave(x, 20, 40) + (150 * math.sin(x / 12 * pi) + 300 * math.sin(x / 30 * pi)) * 2 / 3
    rad = latitude / 180 * pi
    magic = 1 - 0.00669342162296594323 * math.sin(rad) ** 2
    sqrt_magic = math.sqrt(magic)
    lat_offset = lat_offset * 180 / ((6378245 * (1 - 0.00669342162296594323)) / (magic * sqrt_magic) * pi)
    lon_offset = lon_offset * 180 / (6378245 / sqrt_magic * math.cos(rad) * pi)
    return latitude + lat_offset, longitude + lon_offset


def map_search_url(name: str, city: str, *, latitude: float | None = None, longitude: float | None = None) -> str:
    """单点"地图核实"链接：国内高德、海外谷歌。

    有有效坐标：国内走高德 marker（坐标先换算 GCJ-02），海外走谷歌坐标定位；
    无/无效坐标一律关键词搜索（名称 + 城市，空格分隔）。
    """
    label = str(name or "").strip()
    city_text = str(city or "").strip()
    keyword = f"{label} {city_text}".strip()
    coords = _parse_coords(latitude, longitude)
    if _is_domestic(latitude, longitude, city_text):
        if coords is not None:
            g_lat, g_lon = to_gcj02(*coords)
            return (
                f"https://uri.amap.com/marker?position={g_lon},{g_lat}&name={quote(label)}&src={_AMAP_SRC}&callnative=0"
            )
        return f"https://uri.amap.com/search?keyword={quote(keyword)}&src={_AMAP_SRC}&callnative=0"
    query = f"{coords[0]},{coords[1]}" if coords is not None else keyword
    return f"https://www.google.com/maps/search/?api=1&query={quote(query)}"


def map_directions_url(stops: list[dict[str, Any]]) -> str | None:
    """全天路线链接：取有效坐标的停靠点串 waypoint；少于 2 点返回 None。

    国内走高德 navigation（from/to/via 先换算 GCJ-02；官方 URI 途经点上限
    1 个，超出返回 None、由界面提示分段核实——与前端同口径）；海外走谷歌 dir
    （origin/destination 用坐标，waypoints 暂不设上限——D6 已知差异，前端限 3）。
    """
    points: list[tuple[float, float]] = []
    for stop in stops:
        coords = _parse_coords(stop.get("latitude"), stop.get("longitude"))
        if coords is not None:
            points.append(coords)
    if len(points) < 2:
        return None

    def _coord(lat: float, lon: float) -> str:
        g_lat, g_lon = to_gcj02(lat, lon)
        return f"{g_lon},{g_lat}"

    if _in_china(points[0][0], points[0][1]):
        middles = points[1:-1]
        if len(middles) > 1:
            return None
        url = (
            f"https://uri.amap.com/navigation?from={_coord(*points[0])}&to={_coord(*points[-1])}"
            f"&mode=car&policy=1&src={_AMAP_SRC}&coordinate=gaode&callnative=0"
        )
        return f"{url}&via={_coord(*middles[0])}" if middles else url
    origin = quote(f"{points[0][1]},{points[0][0]}")
    destination = quote(f"{points[-1][1]},{points[-1][0]}")
    waypoints = quote("|".join(f"{lon},{lat}" for lat, lon in points[1:-1]))
    url = f"https://www.google.com/maps/dir/?api=1&origin={origin}&destination={destination}"
    return f"{url}&waypoints={waypoints}" if waypoints else url
