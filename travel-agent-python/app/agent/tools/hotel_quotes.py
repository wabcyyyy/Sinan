"""酒店实时价工具（LA2）：SerpApi `google_hotels` 按需取数通道。

前身是 L15 的 Hotellook 观测价合并机器；其取价端点已死（LA1 探针 `dead`，
2026-09-25 实测 404），合并机器随路线 A 整体摘除——主链路酒店价**如实回退
估价**，候选卡挂地图核实深链（`chat_draft/hotel.py` 经 `map_link` 统一口径），
「查实时价」走本工具（按需按钮，L16 接前端）。本模块现职：把 SerpApi 行整形
成消费方可直接渲染的价格行。

与 `flight_quotes.py` 同域同构（`app.agent.tools`）：interactive 车道、共用
SerpApi 月配额计数器（250 次/月按 key 计，两引擎同池——金贵，绝不进主链路）；
缺席一律**日志 + 轨迹事件双可见**（L1「降级可见」），绝不静默空。

事实边界：
- 谷歌酒店价是**查询时点的观测价**：消费方呈现时必须带观测口径，不得当成
  行程自身的价格写回库（服务层 `live_hotel_quotes` 不落库，同航班侧纪律）；
- 无 key / 配额尽 / 上游无价 → `[]`，reason 由服务层按"问过了但没有"与
  "查不了"（400）分开。
"""

from __future__ import annotations

import logging
from typing import Any

from app.agent.data import live_quotes
from app.agent.runtime.trace import record_event, traced

logger = logging.getLogger(__name__)


def _absence(reason: str, **context: object) -> list[dict]:
    """缺席统一出口：日志 + 轨迹事件双可见（与 flight_quotes._quote_absence 同款）。"""
    logger.warning("hotel live quotes absent: %s (%s)", reason, context or "-")
    record_event("tool", "hotel.search_live_quotes", status="degraded", error=reason, metadata=dict(context))
    return []


@traced("tool", "hotel.search_live_quotes")
def search_live_hotel_quotes(
    city: str,
    check_in: str,
    check_out: str,
    *,
    adults: int = 2,
    currency: str = "cny",
    limit: int = 5,
) -> list[dict[str, Any]]:
    """城市 + 入/离窗口 → 酒店实时价行（按每晚价升序），缺席返回 []。

    行 dict 为 wire 形状（camelCase，与航班报价行同约定）：
    name / nightlyPrice / totalPrice / currency / source / rating。
    日期必填——实时查询按精确窗口问，没有窗口就没有可查的口径（如实缺席）。
    """
    place = str(city or "").strip()
    start = str(check_in or "").strip()[:10]
    end = str(check_out or "").strip()[:10]
    if not place:
        return _absence("no_city")
    if not start or not end:
        return _absence("no_date_window", city=place)
    if not live_quotes.live_quotes_enabled():
        return _absence("no_key", city=place)

    rows = live_quotes.fetch_hotel_live_quotes(place, start, end, adults=adults, currency=currency) or []
    priced = [row for row in rows if isinstance(row.get("nightly_price"), (int, float))]
    if not priced:
        return _absence("no_live_rows", city=place, start_date=start, end_date=end)

    priced.sort(key=lambda row: float(row["nightly_price"]))
    return [
        {
            "name": row["name"],
            "nightlyPrice": float(row["nightly_price"]),
            "totalPrice": float(row["total_price"]) if row.get("total_price") is not None else None,
            "currency": row.get("currency"),
            "source": row.get("source"),
            "rating": row.get("rating"),
        }
        for row in priced[: max(int(limit), 1)]
    ]
