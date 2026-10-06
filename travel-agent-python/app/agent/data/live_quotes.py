"""按需实时报价：SerpApi `google_flights` + `google_hotels`（interactive 车道 + 月配额）。

L12 地基卡：只取数、不下结论、不进生成主链路（「查实时价」端点在 L14 接线）。
SerpApi 免费档 250 次/月、付费 $25/千次——**按需补充源**，价格金贵：

- `retry_attempts=0`：每次外呼都真金白银，失败不自动重试（用户手动重查）；
- interactive 车道（用户在等）+ 进程内短 TTL 缓存（15min，重复点击不再烧配额）；
- 月配额计数器是**进程内**的：重启清零。这是单机诚实口径——跨进程持久化反而
  会在多 worker 下高估消耗；真实上限以 SerpApi 后台为准，本计数器防的是"脚本
  一夜烧穿免费档"。**配额按 key 计，两个引擎共用同一只计数器**（LA2：酒店
  通道 google_hotels 加入后不另设机制）。

文档核对结论（2026-09-25，serpapi.com/google-flights-api，入 commit message）：
- `GET https://serpapi.com/search.json`，参数 engine=google_flights、api_key、
  departure_id / arrival_id（IATA 或 kgmid）、outbound_date / return_date
  （YYYY-MM-DD）、type（1=往返默认/2=单程）、currency、adults、travel_class；
- 响应含 search_metadata / search_parameters / best_flights / other_flights /
  price_insights；每个 itinerary = {flights:[{departure_airport{name,id,time},
  arrival_airport, duration, airplane, airline, flight_number, travel_class,
  legroom, extensions}], layovers[], total_duration, price, type,
  booking_token}。**无 deep_link 字段**——booking_token 是 SerpApi 的续查出口，
  用户核实深链由消费方另建（L14/L16）；
- 429 / error 字段 / 配额尽一律 None，绝不冒充成功。
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

from app.common.config import settings
from app.common.external_client import INTERACTIVE, ExternalClient, fetch_json
from app.common.http_client import api_client, contact_user_agent

logger = logging.getLogger(__name__)

_SEARCH_URL = "https://serpapi.com/search.json"
# BESEC-2：UA 收拢单一工厂——此前硬编码占位地址，.env 的 UA_CONTACT 对这里不生效
_USER_AGENT = contact_user_agent()
_QUOTA_TTL_SECONDS = 15 * 60

_serpapi_client: ExternalClient = ExternalClient(
    name="serpapi_flights",
    ttl_seconds=_QUOTA_TTL_SECONDS,
    negative_ttl_seconds=120,
)

# ---- 进程内月配额（单机诚实口径：重启清零） ---------------------------------

_spend_lock = threading.Lock()
_spend: dict[str, Any] = {"month": "", "used": 0}


def current_month() -> str:
    """月份桶键；独立函数便于 fake clock 单测（monkeypatch 本模块属性）。"""
    return time.strftime("%Y-%m")


def _spend_one() -> bool:
    """扣减 1 次配额；月份翻转即清零。配额尽返回 False。"""
    quota = max(0, int(settings.serpapi_monthly_quota))
    with _spend_lock:
        month = current_month()
        if _spend["month"] != month:
            _spend["month"] = month
            _spend["used"] = 0
        if _spend["used"] >= quota:
            return False
        _spend["used"] += 1
        return True


def quota_exhausted() -> bool:
    """快速判空（不扣减）：配额尽时调用方直接拿 None，不睡车道、不开请求。"""
    quota = max(0, int(settings.serpapi_monthly_quota))
    with _spend_lock:
        if _spend["month"] != current_month():
            return False
        return _spend["used"] >= quota


def quota_used() -> int:
    with _spend_lock:
        if _spend["month"] != current_month():
            return 0
        return int(_spend["used"])


def reset_quota_for_tests() -> None:
    with _spend_lock:
        _spend["month"] = ""
        _spend["used"] = 0


def live_quotes_enabled() -> bool:
    return bool(str(settings.serpapi_key or "").strip())


# ---- 取数 -------------------------------------------------------------------


def fetch_live_quotes(
    departure_iata: str,
    arrival_iata: str,
    outbound_date: str,
    return_date: str,
    *,
    currency: str = "cny",
    adults: int = 1,
) -> list[dict[str, Any]] | None:
    """往返实时报价 itinerary 行列表 | None；配额在**真实外呼前一刻**扣减。

    - `None`：未配 key / 配额尽 / 请求失败 / 429 / 应答形状不对；
    - 配额检查不拦缓存：命中进程内缓存的复查不花配额也不被拒（它没有外呼）；
    - 行 dict 归并 best_flights 与 other_flights 两桶（`source_bucket` 标记来源，
      L14 呈现时优先 best），flights 段列表原样透传；
    - 已扣配额不退款：失败的服务调用同样消耗真实配额，记账口径从实。
    """
    api_key = str(settings.serpapi_key or "").strip()
    departure = str(departure_iata or "").strip().upper()
    arrival = str(arrival_iata or "").strip().upper()
    outbound = str(outbound_date or "").strip()
    back = str(return_date or "").strip()
    if not api_key or not departure or not arrival or not outbound or not back:
        return None
    # 密钥绝不进缓存键；api_key 在 query 里，异常脱敏由 external_client 统一兜
    cache_key = f"{departure}:{arrival}:{outbound}:{back}:{currency}:{int(adults)}"
    payload = _serpapi_client.call(
        cache_key,
        lambda: _fetch_with_quota(api_key, departure, arrival, outbound, back, currency, adults),
        lane=INTERACTIVE,
    )
    if not isinstance(payload, dict):
        return None
    if payload.get("error"):
        logger.warning("serpapi[%s>%s] error: %s", departure, arrival, str(payload["error"])[:120])
        return None
    itineraries: list[dict[str, Any]] = []
    for bucket in ("best_flights", "other_flights"):
        entries = payload.get(bucket)
        if not isinstance(entries, list):
            continue
        for item in entries:
            if not isinstance(item, dict):
                continue
            flights = item.get("flights")
            if item.get("price") is None or not isinstance(flights, list) or not flights:
                continue
            itineraries.append(
                {
                    "source_bucket": bucket,
                    "price": item["price"],
                    "type": item.get("type"),
                    "total_duration": item.get("total_duration"),
                    "booking_token": item.get("booking_token"),
                    "flights": [segment for segment in flights if isinstance(segment, dict)],
                }
            )
    return itineraries


def _fetch_with_quota(
    api_key: str,
    departure: str,
    arrival: str,
    outbound: str,
    back: str,
    currency: str,
    adults: int,
) -> Any:
    """loader：缓存未命中且拿到车道槽后才执行——配额在此刻扣减。"""
    if not _spend_one():
        logger.info("serpapi quota exhausted at fetch time (%d/%d)", quota_used(), settings.serpapi_monthly_quota)
        return None
    return fetch_json(
        _serpapi_client,
        api_client(),
        _SEARCH_URL,
        params={
            "engine": "google_flights",
            "departure_id": departure,
            "arrival_id": arrival,
            "outbound_date": outbound,
            "return_date": back,
            "type": 1,
            "currency": currency,
            "adults": int(adults),
            "api_key": api_key,
        },
        headers={"User-Agent": _USER_AGENT},
    )


def fetch_hotel_live_quotes(
    location: str,
    check_in: str,
    check_out: str,
    *,
    adults: int = 2,
    currency: str = "cny",
) -> list[dict[str, Any]] | None:
    """按城市 + 入/离窗口查酒店实时价（SerpApi `google_hotels`）→ property 行列表 | None。

    与 google_flights 共用 key、月配额计数器与 interactive 车道（LA2：按需通道，
    250 次/月是硬顶，绝不进主链路）。语义同 `fetch_live_quotes`：

    - `None`：未配 key / 配额尽 / 请求失败 / 429 / 应答形状不对——"没问到"；
    - 配额在真实外呼前一刻扣减，缓存命中不花配额；已扣不退（真实消耗记账从实）；
    - 行 dict：name / nightly_price / total_price / currency / source / rating；
      缺名或缺每晚价的行丢弃（没有名字无法核对，没有每晚价没有价格事实）。
    """
    api_key = str(settings.serpapi_key or "").strip()
    place = str(location or "").strip()
    check_in = str(check_in or "").strip()
    check_out = str(check_out or "").strip()
    if not api_key or not place or not check_in or not check_out:
        return None
    # 密钥绝不进缓存键；api_key 在 query 里，异常脱敏由 external_client 统一兜
    cache_key = f"hotel:{place}:{check_in}:{check_out}:{currency}:{int(adults)}"
    payload = _serpapi_client.call(
        cache_key,
        lambda: _fetch_hotels_with_quota(api_key, place, check_in, check_out, adults, currency),
        lane=INTERACTIVE,
    )
    if not isinstance(payload, dict):
        return None
    if payload.get("error"):
        logger.warning("serpapi google_hotels[%s] error: %s", place, str(payload["error"])[:120])
        return None
    properties = payload.get("properties")
    if not isinstance(properties, list):
        logger.warning("serpapi google_hotels[%s] envelope without properties", place)
        return None
    rows: list[dict[str, Any]] = []
    for raw in properties:
        if not isinstance(raw, dict):
            continue
        name = str(raw.get("name") or "").strip()
        nightly = _extracted_price((raw.get("rate_per_night") or {}).get("extracted_lowest"))
        if not name or nightly is None:
            continue
        rows.append(
            {
                "name": name,
                "nightly_price": nightly,
                "total_price": _extracted_price((raw.get("total_rate") or {}).get("extracted_lowest")),
                "currency": currency,
                "source": str(raw.get("source") or "").strip() or None,
                "rating": _as_positive_float(raw.get("overall_rating")),
            }
        )
    return rows


def _fetch_hotels_with_quota(
    api_key: str,
    place: str,
    check_in: str,
    check_out: str,
    adults: int,
    currency: str,
) -> Any:
    """loader：缓存未命中且拿到车道槽后才执行——配额在此刻扣减。"""
    if not _spend_one():
        logger.info("serpapi quota exhausted at fetch time (%d/%d)", quota_used(), settings.serpapi_monthly_quota)
        return None
    return fetch_json(
        _serpapi_client,
        api_client(),
        _SEARCH_URL,
        params={
            "engine": "google_hotels",
            "q": place,
            "check_in_date": check_in,
            "check_out_date": check_out,
            "adults": int(adults),
            "currency": currency,
            "api_key": api_key,
        },
        headers={"User-Agent": _USER_AGENT},
    )


def _extracted_price(value: object) -> float | None:
    """SerpApi `extracted_lowest/higher` → 正数价；缺失/非数值/非正一律 None。"""
    return _as_positive_float(value)


def _as_positive_float(value: object) -> float | None:
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None
