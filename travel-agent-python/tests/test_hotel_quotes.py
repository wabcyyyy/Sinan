"""LA2 酒店实时价工具 + 候选卡估价路径单测。

Hotellook 观测价机器已随死端点摘除（LA1 探针 `dead`，2026-09-25）：
- `_hotel_options` 只剩估价链路：price_fact 恒 None——源不可用绝不允许冒充 observed；
- 每张候选卡挂地图核实深链（search_link，map_link 统一口径：国内高德/海外谷歌）；
- `search_live_hotel_quotes` 是「查实时价」按需通道（google_hotels），缺席全可见。
"""

from __future__ import annotations

from datetime import date

import pytest

from app.agent.data import live_quotes
from app.agent.editing.chat_draft import hotel as hotel_module
from app.agent.editing.chat_draft.hotel_intent import HotelIntent
from app.agent.tools import hotel_quotes
from app.common.config import settings
from app.common.season import season_factor
from app.schemas.trip import ChatTurnRequest

# ---- 工具：search_live_hotel_quotes ------------------------------------------


def _fetch_rows() -> list[dict]:
    return [
        {
            "name": "B Hotel",
            "nightly_price": 500.0,
            "total_price": 1000.0,
            "currency": "cny",
            "source": "Booking.com",
            "rating": 4.2,
        },
        {
            "name": "A Hotel",
            "nightly_price": 380.0,
            "total_price": 760.0,
            "currency": "cny",
            "source": "Expedia",
            "rating": 4.6,
        },
    ]


def test_live_hotel_quotes_sorts_by_nightly_and_limits(monkeypatch):
    monkeypatch.setattr(settings, "serpapi_key", "serp-key")
    monkeypatch.setattr(live_quotes, "fetch_hotel_live_quotes", lambda *a, **k: list(_fetch_rows()))
    rows = hotel_quotes.search_live_hotel_quotes("巴黎", "2026-11-07", "2026-11-14", limit=1)
    assert [row["name"] for row in rows] == ["A Hotel"], "按每晚价升序后再截断"
    assert rows[0]["nightlyPrice"] == 380.0, "wire 形状 camelCase（与航班报价行同约定）"
    assert len(rows) == 1


def test_live_hotel_quotes_absence_reasons(monkeypatch, caplog):
    """缺席（无 key / 无窗口 / 上游无价）一律 [] 且日志可见，绝不静默。"""
    monkeypatch.setattr(settings, "serpapi_key", "")
    with caplog.at_level("WARNING"):
        assert hotel_quotes.search_live_hotel_quotes("巴黎", "2026-11-07", "2026-11-14") == []
    assert "no_key" in caplog.text

    monkeypatch.setattr(settings, "serpapi_key", "serp-key")
    assert hotel_quotes.search_live_hotel_quotes("巴黎", "", "") == [], "无窗口不猜"

    monkeypatch.setattr(live_quotes, "fetch_hotel_live_quotes", lambda *a, **k: [])
    with caplog.at_level("WARNING"):
        assert hotel_quotes.search_live_hotel_quotes("巴黎", "2026-11-07", "2026-11-14") == []
    assert "no_live_rows" in caplog.text


# ---- 候选卡：估价路径 + search_link ------------------------------------------


def _request(**overrides) -> ChatTurnRequest:
    payload = {
        "city": "Barcelona",
        "days": 3,
        "persons": 2,
        "start_date": "2026-11-07",
        "message": "换个便宜点的酒店",
        "plans": [{"day_no": 1, "items": [{"item_type": "hotel", "poi_name": "旧酒店", "cost": 500}]}],
    }
    payload.update(overrides)
    return ChatTurnRequest(**payload)


def _intent(**overrides) -> HotelIntent:
    payload = {
        "action": "cheaper",
        "target_tier": "舒适型",
        "base_tier": "舒适型",
        "requested_nights": 2,
        "requested_day_nos": (),
    }
    payload.update(overrides)
    return HotelIntent(**payload)


def test_hotel_options_estimate_path_keeps_no_observed_evidence():
    """LA2 验收：源不可用时出"估价"卡——price_fact 恒 None，不冒充 observed。"""
    hotel = {"id": 9, "name": "Plain Hotel", "ticket_price": 300, "description": "舒适型酒店"}
    options = hotel_module._hotel_options(_request(), [hotel], _intent())
    assert options
    option = options[0]
    assert option.base_price == pytest.approx(300)
    assert option.price_fact is None
    assert option.reason == "舒适型酒店", "估价链路的 reason 口径不变"
    assert option.season_factor == pytest.approx(season_factor(date(2026, 11, 7))), "估价叠季节系数"


def test_hotel_options_search_link_domestic_uses_amap(monkeypatch):
    monkeypatch.setattr(hotel_module.map_link.city_reference, "get_city_geo", lambda name: {"is_domestic": 1})
    hotel = {"id": 1, "name": "北京饭店", "avg_cost": 400, "latitude": 39.909, "longitude": 116.397}
    options = hotel_module._hotel_options(_request(city="北京"), [hotel], _intent())
    assert options and options[0].search_link
    link = options[0].search_link
    assert link.startswith("https://uri.amap.com/marker?"), "国内有坐标走高德 marker 核实"
    assert "position=" in link and "name=" in link, "深链指向这家酒店（坐标 + 名称）"


def test_hotel_options_search_link_overseas_uses_google():
    hotel = {"id": 2, "name": "Sample Hotel Diagonal", "ticket_price": 300}
    options = hotel_module._hotel_options(_request(), [hotel], _intent())
    assert options and options[0].search_link
    assert options[0].search_link.startswith("https://www.google.com/maps/search/"), "海外走谷歌核实"
    assert "Sample%20Hotel" in options[0].search_link or "Sample+Hotel" in options[0].search_link
