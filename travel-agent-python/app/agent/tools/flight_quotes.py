"""航班报价工具（L14）：聚合价（Aviasales）与实时价（SerpApi）两条取数通道。

为什么单独成模块而不是塞进 `impl.py`：这两个函数连整形助手一起 ~210 行，塞进去
会把 `impl.py` 顶过 400 行大文件线（规模棘轮 INV-1 只减不增）。关注点本身也独立
——"trip 级非点位证据"的取数与整形，与 POI 池检索（impl.py 的主题）无关。

与 `impl.py` 同域（`app.agent.tools`），注册表 handler 用 `from app.agent.tools
import flight_quotes` 后**调用期**读模块属性（`flight_quotes.search_flight_quotes`）
——晚绑定语义与 `tools.xxx` 完全一致（`mock.patch.object(flight_quotes, ...)` 生效）。

事实边界（与 PLAN-真实数据面 §1 的诚实叙事一致）：
- 聚合缓存价不是实时价——证据票如实带观测时点，`expires` 收紧到 24h，叙事是
  「出发前核实」；
- 缺席（无出发地 / 城市未映射 / 无 key / 上游无价）一律**日志 + 轨迹事件双可见**，
  绝不静默返回空：否则"没有航班"与"我们没查"在运维面上无法区分；
- 只查不下单：出口是深链（核实），不是下单页。
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import quote

from app.agent.data import city_iata, flight_prices, live_quotes
from app.agent.runtime.trace import record_event, traced

logger = logging.getLogger(__name__)

#: 聚合价的时效上限（SPEC L13/L14：expires 收紧 ≤24h）。
QUOTE_TTL_SECONDS = 24 * 3600
_TZ_CST = timezone(timedelta(hours=8))
_GOOGLE_FLIGHTS = "https://www.google.com/travel/flights?q="


def _quote_now() -> datetime:
    return datetime.now(_TZ_CST)


def _quote_absence(reason: str, **context: object) -> list[dict]:
    """航段缺席的统一出口：**日志 + 轨迹事件双可见**（L1「降级可见」口径）。"""
    logger.warning("flight quotes absent: %s (%s)", reason, context or "-")
    record_event("tool", "flight.search_quotes", status="degraded", error=reason, metadata=dict(context))
    return []


def _as_price(value: object) -> float | None:
    try:
        price = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return price if price > 0 else None


def _as_int(value: object) -> int | None:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _google_flights_link(origin: str, dest: str, outbound: str, back: str | None) -> str:
    """核实用搜索深链（不是报价链接）。

    SerpApi 的 google_flights 响应**没有 deep_link 字段**（官方文档核对结论），
    它的出口是 booking_token——那是 SerpApi 自己的续查令牌，不能给用户。所以
    这里构造一条 Google Flights **搜索**链接：它是一次查询而不是一份报价，
    与地图深链同一叙事（引导用户出发前自行核实）。构造 URL 不等于编造事实。
    """
    query = f"Flights from {origin} to {dest} on {outbound}"
    if back:
        query += f" returning {back}"
    return _GOOGLE_FLIGHTS + quote(query)


def _segment_airport(segment: dict, key: str) -> str:
    airport = segment.get(key)
    return str(airport.get("id") or "").strip().upper() if isinstance(airport, dict) else ""


def _shape_aggregate_row(
    row: dict, *, origin: str, dest: str, origin_city: str, city: str, now: datetime
) -> dict | None:
    """Aviasales 聚合价行 → FlightQuote 字段口径；无有效价返回 None。"""
    price = _as_price(row.get("price"))
    if price is None:
        return None
    link = str(row.get("deep_link") or "") or None
    return {
        "origin_iata": str(row.get("origin") or origin),
        "destination_iata": str(row.get("destination") or dest),
        "origin_city": origin_city,
        "destination_city": city,
        "depart_date": str(row.get("departure_at") or "")[:10] or None,
        "return_date": str(row.get("return_at") or "")[:10] or None,
        "airline": str(row.get("airline") or "") or None,
        "flight_number": str(row.get("flight_number") or "") or None,
        "transfers": _as_int(row.get("transfers")),
        "price": price,
        "currency": str(row.get("currency") or "") or None,
        "provider": "aviasales",
        "source_url": link,
        "retrieved_at": now.isoformat(timespec="seconds"),
        "expires_at": (now + timedelta(seconds=QUOTE_TTL_SECONDS)).isoformat(timespec="seconds"),
        "deep_link": link,
    }


def _shape_live_row(
    row: dict, *, origin: str, dest: str, origin_city: str, city: str, start: str, end: str, now: datetime
) -> dict | None:
    """SerpApi itinerary → FlightQuote 字段口径；无有效价返回 None。"""
    price = _as_price(row.get("price"))
    if price is None:
        return None
    segments = [s for s in (row.get("flights") or []) if isinstance(s, dict)]
    # 往返 itinerary 的 flights 把两个方向串在一个数组里。切分靠"首段的到达机场
    # 就是本次目的地机场"：之后凡是**从该机场起飞**的段都属于返程。不能拿
    # origin 去比——查询用的是城市码（PAR/LON），段里是机场码（CDG/LHR），
    # 两者不同层级，比不中就会把返程段算进去（中转数直接算错）。
    dest_airport = _segment_airport(segments[0], "arrival_airport") if segments else ""
    outbound = (
        [s for s in segments if _segment_airport(s, "departure_airport") != dest_airport] if dest_airport else segments
    )
    first = (outbound or segments or [{}])[0]
    link = _google_flights_link(origin, dest, start, end)
    return {
        "origin_iata": origin,
        "destination_iata": dest,
        "origin_city": origin_city,
        "destination_city": city,
        "depart_date": start,
        "return_date": end,
        # 往返 itinerary 的 flights 含两个方向；航司/航班号取去程首段
        "airline": str(first.get("airline") or "") or None,
        "flight_number": str(first.get("flight_number") or "") or None,
        "transfers": max(len(outbound) - 1, 0) if outbound else None,
        "price": price,
        "currency": "cny",
        "provider": "google_flights",
        "source_url": link,
        "retrieved_at": now.isoformat(timespec="seconds"),
        "expires_at": (now + timedelta(seconds=QUOTE_TTL_SECONDS)).isoformat(timespec="seconds"),
        "deep_link": link,
    }


def _select_aggregate_rows(origin: str, dest: str, start: str, end: str, limit: int) -> list[dict] | None:
    """按日期口径取聚合价行并做窗口过滤；`None` = 上游没答，`[]` = 答了但没有。"""
    if not start:
        return flight_prices.fetch_price_dates(origin, dest, _quote_now().strftime("%Y-%m"), limit=max(limit * 4, 12))
    rows = flight_prices.fetch_price_dates(origin, dest, start, return_at=end or None, limit=max(limit * 4, 12))
    # 精确窗口：上游偶尔给出邻近日期的行，与本行程不符的一律丢弃
    wanted = [row for row in rows or [] if str(row.get("departure_at") or "")[:10] == start]
    if end:
        wanted = [row for row in wanted if str(row.get("return_at") or "")[:10] == end]
    return wanted


@traced("tool", "flight.search_quotes")
def search_flight_quotes(
    origin_city: str,
    city: str,
    start_date: str | None = None,
    end_date: str | None = None,
    limit: int = 3,
) -> list[dict]:
    """出发地 + 目的地 → 往返聚合价行（已整形为 FlightQuote 字段口径）。

    缺席（返回 []）的每一种情形都经 `_quote_absence` 留下可见原因：
    - 没给出发地 → 本卡不猜出发地（"有 origin 才查航班"是契约）；
    - 出发地/目的地未命中 IATA 表 → **不换近似城市去查**（映射错机场是事实错误）；
    - 未配 Travelpayouts token → data 层零外呼（硬纪律）；
    - 上游无该线路报价 → 两家源对中国大陆国内线覆盖弱，如实缺席。

    日期口径：给了 start_date 就按**精确窗口**查往返（departure_at=出发日、
    return_at=返程日），只留窗口精确匹配的行；没给日期则查当月聚合价
    （行上的日期是上游返回的真实日期，不编造）。
    """
    if not str(origin_city or "").strip():
        return _quote_absence("no_origin", city=city)
    origin = city_iata.resolve_iata(origin_city)
    dest = city_iata.resolve_iata(city)
    if not origin or not dest:
        return _quote_absence("city_not_mapped", origin_city=origin_city, city=city, origin_iata=origin, dest_iata=dest)
    if not flight_prices.flight_prices_enabled():
        return _quote_absence("no_token", origin=origin, dest=dest)

    start = str(start_date or "").strip()[:10]
    end = str(end_date or "").strip()[:10]
    wanted = _select_aggregate_rows(origin, dest, start, end, limit) or []
    if not wanted:
        return _quote_absence("no_quote_rows", origin=origin, dest=dest, start_date=start or None, end_date=end or None)

    now = _quote_now()
    origin_text, city_text = str(origin_city).strip(), str(city).strip()
    quotes: list[dict] = []
    for row in sorted(wanted, key=lambda r: _as_price(r.get("price")) or float("inf"))[:limit]:
        shaped = _shape_aggregate_row(row, origin=origin, dest=dest, origin_city=origin_text, city=city_text, now=now)
        if shaped:
            quotes.append(shaped)
    if not quotes:
        return _quote_absence("rows_without_price", origin=origin, dest=dest)
    return quotes


@traced("tool", "flight.search_live_quotes")
def search_live_flight_quotes(
    origin_city: str,
    city: str,
    start_date: str | None = None,
    end_date: str | None = None,
    limit: int = 3,
) -> list[dict]:
    """按需实时价（SerpApi google_flights，interactive 车道）→ FlightQuote 口径行。

    与聚合价同一 wire 形状，只有 provider/deep_link 不同：消费方（详情 VO、
    前端）不需要知道数据来自哪一家。日期必填——实时查询按精确日期问，没有
    日期就没有可查的窗口（如实缺席）。
    """
    origin = city_iata.resolve_iata(origin_city)
    dest = city_iata.resolve_iata(city)
    start = str(start_date or "").strip()[:10]
    end = str(end_date or "").strip()[:10]
    if not origin or not dest:
        return _quote_absence("city_not_mapped", origin_city=origin_city, city=city)
    if not start or not end:
        return _quote_absence("no_date_window", origin=origin, dest=dest, start_date=start or None)
    if not live_quotes.live_quotes_enabled():
        return _quote_absence("no_key", origin=origin, dest=dest)

    rows = live_quotes.fetch_live_quotes(origin, dest, start, end) or []
    if not rows:
        return _quote_absence("no_live_rows", origin=origin, dest=dest, start_date=start, end_date=end)

    now = _quote_now()
    origin_text, city_text = str(origin_city).strip(), str(city).strip()
    quotes: list[dict] = []
    for row in rows[:limit]:
        shaped = _shape_live_row(
            row, origin=origin, dest=dest, origin_city=origin_text, city=city_text, start=start, end=end, now=now
        )
        if shaped:
            quotes.append(shaped)
    if not quotes:
        return _quote_absence("rows_without_price", origin=origin, dest=dest)
    return quotes


def shape_quote_for_wire(row: dict[str, Any]) -> dict[str, Any] | None:
    """内部形状 → 线级 FlightQuote 别名形状；形状不合法返回 None（跨模块 API）。

    由 services 层的详情 VO 消费（`itinerary_query._loads_flight_quotes`）——
    该处不能深路径 import agent 子模块，走 `app.agent` 门面导出本函数。
    """
    from app.schemas.trip import FlightQuote

    try:
        return FlightQuote.model_validate(row).model_dump(by_alias=True)
    except Exception:
        return None
