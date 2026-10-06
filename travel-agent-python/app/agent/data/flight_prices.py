"""机票聚合价数据层：Aviasales v3 `prices_for_dates`（Travelpayouts Data API）。

L12 地基卡：只取数、不下结论、不进 agent 链路（接线在 L14）。这是聚合缓存价
而非实时报价，时效口径由消费方（grounding/FactEvidence）收紧——本层只如实带
回行数据并标注货币与深链。

官方文档核对结论（2026-09-25，入 commit message）：
- `GET https://api.travelpayouts.com/aviasales/v3/prices_for_dates`，参数
  origin / destination（IATA）、departure_at（YYYY-MM 或日期）、currency、
  sorting=price、one_way、limit；官方限速 600 req/min——background 车道
  1.0s 间隔远低于限制；
- 响应 `{success, currency, data:[{origin, destination, origin_airport,
  destination_airport, price, airline, flight_number, departure_at, return_at,
  transfers, return_transfers, duration, duration_to, duration_back, link}]}`；
  `link` 是 Aviasales 站内相对路径，本层拼成绝对 `deep_link`；
- 鉴权官方双通道：`X-Access-Token` 头**或** `token` query 参数。本层走头通道
  ——token 既不进 URL（httpx 异常消息带不出密钥）也不进缓存键（密钥不是缓存
  身份，失败日志会原样打出键，places.py 同款纪律）。

None 与 [] 是两种结论（places.geocode_place_rows 同款语义，不可合并）：
- `None`：未配 token / 请求失败 / 应答形状不对——"没问到"，消费方按缺席降级；
- `[]`：问到了且该线路该月确无报价行。
"""

from __future__ import annotations

import logging
from typing import Any

from app.common.config import settings
from app.common.external_client import BACKGROUND, ExternalClient, fetch_json
from app.common.http_client import api_client, contact_user_agent

logger = logging.getLogger(__name__)

_PRICES_FOR_DATES = "https://api.travelpayouts.com/aviasales/v3/prices_for_dates"
_AVIASALES_BASE = "https://www.aviasales.com"
# BESEC-2：UA 收拢单一工厂——此前硬编码占位地址，.env 的 UA_CONTACT 对这里不生效
_USER_AGENT = contact_user_agent()

_flight_client: ExternalClient = ExternalClient(
    name="aviasales_prices",
    ttl_seconds=6 * 3600,
    negative_ttl_seconds=600,
    min_interval_seconds=1.0,
    retry_attempts=1,
)

#: 行内必须可用的最小键集（缺失即丢行，容错口径见 fetch_price_dates）
_ROW_MIN_KEYS = ("origin", "destination", "price", "departure_at")


def flight_prices_enabled() -> bool:
    return bool(str(settings.travelpayouts_token or "").strip())


def fetch_price_dates(
    origin_iata: str,
    dest_iata: str,
    depart_month: str,
    *,
    currency: str = "cny",
    limit: int = 30,
    return_at: str | None = None,
) -> list[dict[str, Any]] | None:
    """出发/目的地 IATA + 出发期（YYYY-MM 或 YYYY-MM-DD）→ 按价排序的聚合价行列表 | None。

    行 dict 透传上游全部字段（未知字段原样保留，不猜测不裁剪），仅补两列：
    `currency`（来自应答信封）与 `deep_link`（上游相对 link → 绝对地址）。
    缺 price/origin/destination/departure_at 任一项的行丢弃——单行残缺是上游
    脏数据，不值得让整次查询降 None。

    `return_at` 给了就是**往返**查询（不带 `one_way`，官方默认往返），返回行的
    `return_at` 是返程日期、`price` 是往返总价；不给则单程（L12 既有口径）。
    """
    token = str(settings.travelpayouts_token or "").strip()
    origin = str(origin_iata or "").strip().upper()
    dest = str(dest_iata or "").strip().upper()
    month = str(depart_month or "").strip()
    back = str(return_at or "").strip()
    if not token or not origin or not dest or not month:
        return None
    params: dict[str, Any] = {
        "origin": origin,
        "destination": dest,
        "departure_at": month,
        "currency": currency,
        "sorting": "price",
        "limit": int(limit),
    }
    if back:
        params["return_at"] = back
    else:
        params["one_way"] = "true"
    # 密钥绝不进缓存键（places.py 同款纪律）：失败日志会原样打出键
    cache_key = f"{origin}:{dest}:{month}:{back}:{currency}:{int(limit)}"
    payload = _flight_client.call(
        cache_key,
        lambda: fetch_json(
            _flight_client,
            api_client(),
            _PRICES_FOR_DATES,
            params=params,
            headers={"X-Access-Token": token, "User-Agent": _USER_AGENT},
        ),
        lane=BACKGROUND,
    )
    if not isinstance(payload, dict) or payload.get("success") is not True:
        logger.warning("aviasales[%s>%s %s~%s] envelope not success", origin, dest, month, back or "-")
        return None
    rows_raw = payload.get("data")
    if not isinstance(rows_raw, list):
        logger.warning("aviasales[%s>%s %s~%s] envelope without data list", origin, dest, month, back or "-")
        return None
    currency_text = str(payload.get("currency") or currency)
    rows: list[dict[str, Any]] = []
    for raw in rows_raw:
        if not isinstance(raw, dict) or any(raw.get(key) is None for key in _ROW_MIN_KEYS):
            continue
        row = dict(raw)
        row["currency"] = currency_text
        link = str(raw.get("link") or "")
        row["deep_link"] = link if link.startswith("http") else _AVIASALES_BASE + link
        rows.append(row)
    return rows
