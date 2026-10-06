"""LA2 酒店实时价链路单测：google_hotels 取数、与航班共用的月配额、端点错误边界。

对应 SPEC LA2 验收：
- 「查实时价」走 SerpApi google_hotels 时配额计数正确（与 google_flights 同池）；
- 配额尽返回明确文案（429，复用 L14 机制，不重复实现）；
- 问过但没有 → 200 + 空列表 + reason（与"查不了"400 分开）；
- HotelOption.searchLink 契约槽位（optional，wire camelCase）。
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.agent.data import live_quotes
from app.api.business.auth import auth_router
from app.api.business.itinerary import router as itinerary_router
from app.common import cache_store, jwt_compat
from app.common.config import settings
from app.common.envelope import install_exception_handlers
from app.common.http_client import configure_clients
from app.db import session as db_session
from app.db.models import Base, ItineraryMain, SysUser
from app.schemas.trip import HotelOption

JWT_MATERIAL = "example-only-hs256-test-signing-material"

HOTEL_FIXTURE = {
    "search_metadata": {"google_hotels_url": "https://www.google.com/travel/hotels/x"},
    "properties": [
        {
            "name": "Hotel Paris Opéra",
            "rate_per_night": {"extracted_lowest": 420, "extracted_higher": 510},
            "total_rate": {"extracted_lowest": 840},
            "source": "Booking.com",
            "overall_rating": 4.5,
        },
        {"name": "No Price Hotel", "source": "Expedia"},  # 缺每晚价 → 丢
        {"rate_per_night": {"extracted_lowest": 100}},  # 缺名 → 丢
        {
            "name": "Budget Hôtel",
            "rate_per_night": {"extracted_lowest": "310"},  # 字符串数字容错
            "total_rate": {},
            "overall_rating": 0,  # 0 分 → None（不是有效评分）
        },
    ],
}


def _mock(handler) -> None:
    configure_clients(api=httpx.Client(transport=httpx.MockTransport(handler)))


# ---- 数据层：fetch_hotel_live_quotes -----------------------------------------


def test_hotel_no_key_means_zero_calls(monkeypatch):
    monkeypatch.setattr(settings, "serpapi_key", "")
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        return httpx.Response(200, json=HOTEL_FIXTURE)

    _mock(handler)
    try:
        assert live_quotes.fetch_hotel_live_quotes("巴黎", "2026-11-07", "2026-11-14") is None
        assert requests == [], "无 key 必须零外呼"
    finally:
        configure_clients(api=None)


def test_hotel_rows_parsed_and_invalid_dropped(monkeypatch):
    monkeypatch.setattr(settings, "serpapi_key", "serp-key-xyz")
    live_quotes.reset_quota_for_tests()

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["engine"] == "google_hotels"
        assert request.url.params["q"] == "巴黎"
        assert request.url.params["check_in_date"] == "2026-11-07"
        return httpx.Response(200, json=HOTEL_FIXTURE)

    _mock(handler)
    try:
        rows = live_quotes.fetch_hotel_live_quotes("巴黎", "2026-11-07", "2026-11-14")
    finally:
        configure_clients(api=None)
    assert rows is not None and len(rows) == 2, "缺名/缺每晚价的行必须丢弃"
    assert rows[0]["nightly_price"] == 420 and rows[0]["total_price"] == 840
    assert rows[0]["source"] == "Booking.com" and rows[0]["rating"] == 4.5
    assert rows[1]["nightly_price"] == 310, "字符串数字要容错"
    assert rows[1]["total_price"] is None and rows[1]["rating"] is None


def test_hotel_quota_shared_with_flights(monkeypatch):
    """LA2 验收核心：两个引擎共用同一只月配额计数器（250 次/月按 key 计）。"""
    monkeypatch.setattr(settings, "serpapi_key", "serp-key-xyz")
    monkeypatch.setattr(settings, "serpapi_monthly_quota", 1)
    live_quotes.reset_quota_for_tests()
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        if "google_flights" in str(request.url):
            return httpx.Response(
                200,
                json={"best_flights": [{"price": 1180, "flights": [{"departure_airport": {"id": "ORY"}}]}]},
            )
        return httpx.Response(200, json=HOTEL_FIXTURE)

    _mock(handler)
    try:
        # 航班先烧掉最后 1 次配额
        assert live_quotes.fetch_live_quotes("CDG", "LHR", "2026-10-03", "2026-10-10") is not None
        assert live_quotes.quota_used() == 1
        # 酒店再查：配额尽，零外呼直接 None
        assert live_quotes.fetch_hotel_live_quotes("巴黎", "2026-11-07", "2026-11-14") is None
        assert len(requests) == 1, "配额尽时酒店查询不得外呼"
    finally:
        configure_clients(api=None)


def test_hotel_429_and_error_degrade_to_none(monkeypatch, caplog):
    monkeypatch.setattr(settings, "serpapi_key", "serp-key-xyz")
    live_quotes.reset_quota_for_tests()

    _mock(lambda request: httpx.Response(429))
    try:
        with caplog.at_level("WARNING", logger="app.common.external_client"):
            assert live_quotes.fetch_hotel_live_quotes("巴黎", "2026-11-07", "2026-11-14") is None
        assert "serp-key-xyz" not in caplog.text, "api_key 走 query，异常日志必须脱敏"
    finally:
        configure_clients(api=None)

    live_quotes.reset_quota_for_tests()
    live_quotes._serpapi_client.clear_cache()
    _mock(lambda request: httpx.Response(200, json={"error": "You have exhausted your API key search limit"}))
    try:
        assert live_quotes.fetch_hotel_live_quotes("巴黎", "2026-11-07", "2026-11-14") is None
    finally:
        configure_clients(api=None)


def test_hotel_bad_envelope_degrades_to_none(monkeypatch):
    monkeypatch.setattr(settings, "serpapi_key", "serp-key-xyz")
    live_quotes.reset_quota_for_tests()
    _mock(lambda request: httpx.Response(200, json={"search_metadata": {}}))
    try:
        assert live_quotes.fetch_hotel_live_quotes("巴黎", "2026-11-07", "2026-11-14") is None
    finally:
        configure_clients(api=None)


# ---- 契约：HotelOption.searchLink --------------------------------------------


def test_hotel_option_search_link_optional_camel():
    dumped = HotelOption(
        id="1",
        hotel_name="H",
        tier="舒适型",
        base_price=300,
        season_factor=1.0,
        season_label="平季",
        nightly_price=300,
        nights=1,
        rooms=1,
        total_price=300,
        within_budget=True,
        reason="x",
    ).model_dump(by_alias=True)
    assert dumped["searchLink"] is None and dumped["priceFact"] is None, "缺省即 null（旧客户端零破坏）"
    full = HotelOption(
        id="2",
        hotel_name="H",
        tier="舒适型",
        base_price=300,
        season_factor=1.0,
        season_label="平季",
        nightly_price=300,
        nights=1,
        rooms=1,
        total_price=300,
        within_budget=True,
        reason="x",
        search_link="https://uri.amap.com/search?keyword=x",
    )
    assert full.model_dump(by_alias=True)["searchLink"].startswith("https://uri.amap.com/")


# ---- 端点：POST /{id}/hotel-quotes/live --------------------------------------


@pytest.fixture()
def client(db):
    app = FastAPI()
    install_exception_handlers(app)
    app.include_router(auth_router)
    app.include_router(itinerary_router)
    token = jwt_compat.encode_token("dora", JWT_MATERIAL, 3600)
    api = TestClient(app)
    api.headers.update({"Authorization": f"Bearer {token}"})
    return api


@pytest.fixture()
def db(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'la2.db'}")
    Base.metadata.create_all(engine)
    db_session.init_engine(engine, sessionmaker(bind=engine, expire_on_commit=False))
    monkeypatch.setattr(settings, "jwt_secret", JWT_MATERIAL)
    cache_store.reset_for_tests()
    from app.services import user_service

    with db_session.session_scope() as session:
        session.add(
            SysUser(username="dora", password=user_service.hash_password("x"), nickname=None, status=1, role="user")
        )
        session.flush()
        session.add(
            ItineraryMain(
                user_id=1,
                title="巴黎3日",
                city="巴黎",
                start_date=date(2026, 11, 7),
                end_date=date(2026, 11, 14),
                days=3,
                persons=2,
                budget=Decimal("20000.00"),
                status=2,
            )
        )
        session.add(
            ItineraryMain(
                user_id=1,
                title="无日期行程",
                city="巴黎",
                days=3,
                persons=2,
                status=2,
            )
        )
    yield
    db_session.init_engine(None, None)
    cache_store.reset_for_tests()


def test_hotel_live_endpoint_missing_dates_returns_400(client):
    resp = client.post("/api/itinerary/2/hotel-quotes/live", json={})
    assert resp.status_code == 400
    assert "日期" in resp.json()["message"]


def test_hotel_live_endpoint_quota_exhausted_returns_429(client, monkeypatch):
    monkeypatch.setattr(settings, "serpapi_key", "serp-key")
    monkeypatch.setattr(settings, "serpapi_monthly_quota", 1)
    live_quotes.reset_quota_for_tests()
    live_quotes._spend.update(month=live_quotes.current_month(), used=1)
    resp = client.post("/api/itinerary/1/hotel-quotes/live", json={})
    assert resp.status_code == 429
    assert "配额" in resp.json()["message"]
    live_quotes.reset_quota_for_tests()


def test_hotel_live_endpoint_returns_quotes(client, monkeypatch):
    monkeypatch.setattr(settings, "serpapi_key", "serp-key")
    live_quotes.reset_quota_for_tests()
    _mock(lambda request: httpx.Response(200, json=HOTEL_FIXTURE))
    try:
        resp = client.post("/api/itinerary/1/hotel-quotes/live", json={})
    finally:
        configure_clients(api=None)
    assert resp.status_code == 200
    body = resp.json()["data"]
    assert body["hotelQuotes"], "有实时价时返回报价行"
    assert body["city"] == "巴黎"
    assert body["checkIn"] == "2026-11-07" and body["checkOut"] == "2026-11-14"
    assert body["reason"] is None
    assert body["hotelQuotes"][0]["nightlyPrice"] == 310, "wire camelCase；每晚价升序（310 < 420）"


def test_hotel_live_endpoint_empty_result_carries_reason(client, monkeypatch):
    """问过但没有 → 200 + 空列表 + reason（与"查不了"的 400 分开）。"""
    monkeypatch.setattr(settings, "serpapi_key", "serp-key")
    live_quotes.reset_quota_for_tests()
    _mock(lambda request: httpx.Response(200, json={"properties": []}))
    try:
        resp = client.post("/api/itinerary/1/hotel-quotes/live", json={})
    finally:
        configure_clients(api=None)
    assert resp.status_code == 200
    body = resp.json()["data"]
    assert body["hotelQuotes"] == []
    assert body["reason"]


def test_hotel_live_endpoint_does_not_persist(client, monkeypatch):
    """实时价是"看一眼"，不落库——不产生行程状态变更。"""
    monkeypatch.setattr(settings, "serpapi_key", "serp-key")
    live_quotes.reset_quota_for_tests()
    _mock(lambda request: httpx.Response(200, json=HOTEL_FIXTURE))
    try:
        assert client.post("/api/itinerary/1/hotel-quotes/live", json={}).status_code == 200
    finally:
        configure_clients(api=None)
