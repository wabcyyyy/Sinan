"""外部地点数据层：OpenTripMap 为主、Nominatim 兜底（只取数据，不下结论）。

POI 权威库退役后的坐标/分类/图片来源（TREK 式"不存语料，事实交给外部活数据"）：

- **OpenTripMap**（OSM+Wikidata 加工的旅游切片）：城市定位（geoname）、景点半径
  检索（坐标/分类/热度）、xid 详情（结构化地址/官网/图片/维基简介）。免费 key，
  仅 en/ru 两种语言——返回名称为英文，中文名由生成链路的 LLM 对齐；
- **Nominatim**（OSM 官方地理编码）：任意名称→坐标，OTM 失败或缺 key 时兜底；
  使用政策要求 1 rps 与可联系的 User-Agent。`geocode_place_rows` 区分 None
  （没问到）与 [] （问到且没有），供存在性判定取用。

事实边界：两家数据源都**没有票价/营业时间**——这些字段由 LLM 估价并按
`estimated` 如实标注，行程页以深链（见 `map_link`）引导用户出发前自行核实。

依赖：common（external_client/http_client/config）；无 agent 内部依赖、无 DB。
地图深链与 GCJ-02 换算在 `app/agent/data/map_link.py`。
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from typing import Any
from urllib.parse import quote

from app.agent.core.poi_identity import matches_any
from app.common.config import settings
from app.common.external_client import BACKGROUND, INTERACTIVE, ExternalClient, fetch_json, redact_secrets
from app.common.http_client import api_client, contact_user_agent

logger = logging.getLogger(__name__)

_OTM_BASE = "https://api.opentripmap.com/0.1/en"
_NOMINATIM_BASE = "https://nominatim.openstreetmap.org/search"
# 与 wikipedia 图片通道一致的 UA 策略（Nominatim 政策要求可联系的标识；OTM 同式
# 标识，L1 补充 1：三客户端此前不带 UA，对免费源不留联系方式不礼貌）。
# 联系方式走 UA_CONTACT 部署参数（R3-F7 拍板 2026-10-05）：默认占位地址，真实值进 .env 不入仓。
# BESEC-2：UA 收拢到 http_client.contact_user_agent 单一工厂，此处不再自拼字符串。
_UA = contact_user_agent()
# enrich 详情并入的并行度（L1：串行 12 次 detail 在供应商变慢时最坏 12×超时 96s；
# 并行后最坏 ≈ 间隔排队 11s + 一次超时）。真正的速率闸门是 OTM 车道节流，
# 线程再多也只是排队，故小常量即可、不进 Settings（反冗余）。
_ENRICH_WORKERS = 4


def otm_enabled() -> bool:
    return bool(str(settings.otm_api_key or "").strip())


# ---- 缓存/节流通道（G-3.2） -------------------------------------------------
# 城市坐标与地点详情基本不变：成功缓存 24h；半径检索 6h（新开景点能较快出现）。
# 节流与重试纪律（L1）：OTM 免费档明确低于 5 rps——geo/radius 收紧到 0.35s
# （≈2.9 rps）；detail 走 background 车道，显式固化 1.0s 不再依赖车道默认。
# detail 是 enrich 的高频道，开 1 次重试。
# radius 也开 1 次重试（审查 P2-8 评估结论，原为 0）：免费源、无月配额计数器，
# 且重试会重新预约车道槽（不跌破 2.9 rps 的自我约束）；代价是失败时每次生成多
# ≤2 次免费调用，换来"一次瞬时 429/5xx 不再让整个域的证据池归零"——池空直接
# 表现为 draft_only，是用户可见的最强降级。geo 保持 0：每趟只查 1 次、成功缓存
# 24h，重试窗口内第二次调用与第一次几乎同因失败，收益不抵调用。
# 三个 client 共用 `rate_group="otm"`（R3-F2）：per-instance 节流下 geo+radius+
# detail 三车道叠加实测 ≈6.8 rps 破 5 rps 自设约束——共享后 geo+radius 同一条
# 0.35s 时间线（叠加仍 ≈2.9 rps），detail background 1.0s，总叠加 ≈3.9 rps ≤5。
_otm_geo_client: ExternalClient = ExternalClient(
    name="otm_geoname",
    ttl_seconds=24 * 3600,
    negative_ttl_seconds=300,
    timeout_seconds=settings.places_timeout_seconds,
    min_interval_seconds=0.35,
    rate_group="otm",
)
_otm_radius_client: ExternalClient = ExternalClient(
    name="otm_radius",
    ttl_seconds=6 * 3600,
    negative_ttl_seconds=600,
    timeout_seconds=settings.places_timeout_seconds,
    min_interval_seconds=0.35,
    retry_attempts=1,
    rate_group="otm",
)
_otm_detail_client: ExternalClient = ExternalClient(
    name="otm_detail",
    ttl_seconds=24 * 3600,
    negative_ttl_seconds=1800,
    timeout_seconds=settings.places_timeout_seconds,
    min_interval_seconds=1.0,
    retry_attempts=1,
    rate_group="otm",
)
# Nominatim 政策：公共实例 1 rps —— 显式放宽车道间隔到 1.1s；瞬时失败重试 1 次
# （重试同样过 1.1s 槽，不会跌破政策速率）。
_nominatim_client: ExternalClient = ExternalClient(
    name="nominatim",
    ttl_seconds=24 * 3600,
    negative_ttl_seconds=600,
    timeout_seconds=settings.places_timeout_seconds,
    min_interval_seconds=1.1,
    retry_attempts=1,
)


def _otm_get(path: str, params: dict[str, Any], client: ExternalClient, *, lane: str = INTERACTIVE) -> Any:
    key = str(settings.otm_api_key or "").strip()
    if not key:
        return None
    # 密钥绝不进缓存键：它不是缓存身份的一部分，而失败日志会把缓存键原样打出来
    # ——把 apikey 拼进键里等于把密钥写进生产日志（2026-09-18 量测时实测到）。
    cache_key = f"{path}:{sorted(params.items(), key=lambda kv: kv[0])}"

    def _load() -> Any:
        payload = fetch_json(
            client,
            api_client(),
            f"{_OTM_BASE}/{path}",
            params={**params, "apikey": key},
            headers={"User-Agent": _UA},
        )
        # error 解包必须在缓存之前（R3-F5）：OTM 偶发「HTTP 200 + {"error": …}」，
        # 旧实现把 error dict 当成功正缓存 6-24h 且熔断计数永不推进；就地抛出让它
        # 走既有语义——None 负缓存（短 TTL）+ `_record_outcome(True)` 计入熔断。
        if isinstance(payload, dict) and payload.get("error"):
            raise RuntimeError(f"otm[{path}]: {redact_secrets(str(payload['error']))[:120]}")
        return payload

    return client.call(cache_key, _load, lane=lane)


# ---- OpenTripMap：城市定位 / 半径检索 / 详情 ---------------------------------


def resolve_city_center(city_en: str) -> dict[str, Any] | None:
    """城市名（建议英文）→ 中心坐标与国家；OTM 不可用返回 None。"""
    name = str(city_en or "").strip()
    if not name:
        return None
    row = _otm_get("places/geoname", {"name": name[:64]}, _otm_geo_client)
    if not isinstance(row, dict) or row.get("lat") is None or row.get("lon") is None:
        return None
    return {
        "name": str(row.get("name") or ""),
        "country": str(row.get("country") or ""),
        "country_code": str(row.get("country") or ""),
        "latitude": float(row["lat"]),
        "longitude": float(row["lon"]),
    }


def _normalize_place(
    row: dict[str, Any], *, city: str | None = None, category: str = "attraction"
) -> dict[str, Any] | None:
    """OTM 列表行 → 统一地点条目；空名/缺坐标的行丢弃（name:"" 的 OSM 节点不少见）。"""
    point = row.get("point") or {}
    name = str(row.get("name") or "").strip()
    lat_raw, lon_raw = point.get("lat"), point.get("lon")
    if lat_raw is None or lon_raw is None:
        return None
    try:
        lat, lon = float(lat_raw), float(lon_raw)
    except (TypeError, ValueError):
        return None
    if not name or (lat == 0.0 and lon == 0.0):
        return None
    rate = row.get("rate")
    if isinstance(rate, str) and rate.isdigit():
        rate = int(rate)
    return {
        "xid": str(row.get("xid") or ""),
        "name": name,
        "category": category,
        "latitude": lat,
        "longitude": lon,
        "dist_m": round(float(row["dist"])) if row.get("dist") is not None else None,
        "popularity": int(rate) if isinstance(rate, (int, float)) else None,
        "kinds": str(row.get("kinds") or ""),
        "source": "opentripmap",
        "city": city,
    }


def search_places_near(
    latitude: float,
    longitude: float,
    *,
    radius_m: int | None = None,
    limit: int | None = None,
    city: str | None = None,
    kinds: str | None = None,
    category: str = "attraction",
) -> list[dict[str, Any]]:
    """以坐标为圆心的地点检索（热度降序）；kinds 传 OTM 分类（如 foods）可收窄。

    OTM 不可用返回空，由上层降级为联网搜索/纯 LLM。
    """
    if not otm_enabled():
        return []
    params: dict[str, Any] = {
        "radius": int(radius_m or settings.otm_radius_m),
        "lat": latitude,
        "lon": longitude,
        "format": "json",
        "limit": int(limit or settings.otm_limit),
        "sort": "-rate",
    }
    if kinds:
        params["kinds"] = kinds
    payload = _otm_get("places/radius", params, _otm_radius_client)
    if not isinstance(payload, list):
        return []
    out = [p for row in payload if isinstance(row, dict) if (p := _normalize_place(row, city=city, category=category))]
    out.sort(key=lambda p: (-(p["popularity"] or 0), p["dist_m"] if p["dist_m"] is not None else 1 << 30))
    return out


def place_detail(xid: str) -> dict[str, Any] | None:
    """xid → 地址/官网/图片/维基简介；用于给头部候选补详情（后台车道）。"""
    row = _otm_get("places/xid/" + quote(str(xid or "")), {}, _otm_detail_client, lane=BACKGROUND)
    if not isinstance(row, dict):
        return None
    address = row.get("address") or {}
    address_text = " ".join(
        str(address.get(k) or "").strip()
        for k in ("road", "house", "city", "state")
        if str(address.get(k) or "").strip()
    )
    extract = (row.get("wikipedia_extracts") or {}).get("text")
    preview = (row.get("preview") or {}).get("source")
    rate = row.get("rate")
    return {
        "xid": str(row.get("xid") or xid),
        "name": str(row.get("name") or ""),
        "address": address_text or None,
        "url": str(row.get("url") or "") or None,
        "image": preview or (str(row.get("image") or "") or None),
        "intro": str(extract or "").strip() or None,
        "wikipedia": str(row.get("wikipedia") or "") or None,
        "wikidata": str(row.get("wikidata") or "") or None,
        "popularity": int(rate) if isinstance(rate, (int, float)) else None,
        "source": "opentripmap",
    }


def enrich_with_details(places: list[dict[str, Any]], *, top: int = 12) -> list[dict[str, Any]]:
    """给热度最高的前 top 个景点并入详情字段（官网/图片/简介）；后台车道、失败原地保留。

    L1 并行化：详情请求是互不依赖的只读 GET，4 路 fan-out 让「供应商变慢/挂」
    的最坏等待从 串行 12×超时 降到 排队间隔 + 单次超时；真实速率仍由 detail
    车道节流钳制（多开线程只是排队）。线程池是内联 fan-out（同 supervisor 与
    _search_pois 先例），不是 INV-8 语境里脱离请求生命周期的后台任务。
    `pool.map` 保序合并：输出顺序与串行版逐字节一致（eval/快照零 diff 的前提）。
    """
    targets = places[:top]
    # 无 key 时 place_detail 恒为 None：直接跳过（离线/eval 路径零线程池开销）。
    if not targets or not otm_enabled():
        return places
    with ThreadPoolExecutor(max_workers=min(_ENRICH_WORKERS, len(targets)), thread_name_prefix="poi-enrich") as pool:
        details = list(pool.map(lambda place: place_detail(str(place.get("xid") or "")), targets))
    for place, detail in zip(targets, details, strict=True):
        if not detail:
            continue
        for key in ("address", "url", "image", "intro", "wikipedia", "wikidata"):
            if detail.get(key) and not place.get(key):
                place[key] = detail[key]
    return places


# ---- Nominatim 兜底 ---------------------------------------------------------


def geocode_place_rows(name: str, city: str | None = None, *, namedetails: bool = False) -> list[dict[str, Any]] | None:
    """点名→候选要素列表；**None 与 [] 是两种结论**，不可合并。

    - `None`：未启用 / 请求失败 / 非 200 / 解析失败——"没问到"，不能当成不存在；
    - `[]`：服务成功应答且明确没有这个要素——provider 的否定结论。

    存在性判定（`app/agent/grounding/existence.py`）依赖这个区分：把请求失败当成
    "景点不存在"去删用户的点位，是本模块最不能犯的错。
    `namedetails=True` 时带回 OSM 的全部名称标签（name:ja / name:zh-Hans /
    alt_name…），跨脚本与简繁写法只有靠它才能对上（2026-09-18 量测实测）。
    """
    if not settings.nominatim_enabled:  # 离线护栏：eval/CI 不能因为"点名解析"打公网
        return None
    query = " ".join(part for part in (str(name or "").strip(), str(city or "").strip()) if part)
    if not query:
        return None
    params: dict[str, Any] = {"q": query[:120], "format": "jsonv2", "limit": 5 if namedetails else 1}
    if namedetails:
        params["namedetails"] = "1"
    payload = _nominatim_client.call(
        f"q:{query[:120]}:{int(namedetails)}",
        lambda: fetch_json(
            _nominatim_client,
            api_client(),
            _NOMINATIM_BASE,
            params=params,
            headers={"User-Agent": _UA, "Accept-Language": "zh-CN,zh,en"},
        ),
    )
    if not isinstance(payload, list):
        return None
    rows: list[dict[str, Any]] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        try:
            lat, lng = float(item["lat"]), float(item["lon"])
        except (TypeError, ValueError, KeyError):
            continue
        raw_details: Any = item.get("namedetails")
        details: dict[str, Any] = raw_details if isinstance(raw_details, dict) else {}
        rows.append(
            {
                "name": str(item.get("name") or ""),
                "display_name": str(item.get("display_name") or ""),
                "latitude": lat,
                "longitude": lng,
                # 别名集合：任何一条名称标签都算同一个实体的写法
                "aliases": sorted({str(v).strip() for v in details.values() if str(v or "").strip()}),
                "localized_name": str(
                    details.get("name:zh-Hans") or details.get("name:zh-CN") or details.get("name:zh") or ""
                ).strip()
                or None,
                "country_code": str(item.get("country_code") or "").upper() or None,
                "category": str(item.get("category") or ""),
                "type": str(item.get("type") or ""),
                "source": "nominatim",
            }
        )
    return rows


def geocode_place(name: str, city: str | None = None) -> dict[str, Any] | None:
    """名称（可带城市）→ 单个坐标；OTM 是分类检索、兜不了"点名解析"，由 Nominatim 承担。"""
    rows = geocode_place_rows(name, city, namedetails=True)
    return next(
        (row for row in rows or [] if matches_any(name, [row.get("name", ""), *(row.get("aliases") or [])])), None
    )
