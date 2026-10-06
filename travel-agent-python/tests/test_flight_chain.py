"""L14 航班链路单测：IATA 映射、三分支取数、缺席可见、预算观测价、实时价端点。

验收对应（SPEC L14）：
- MockTransport 三分支：有报价 / 无 token / 上游挂；
- origin 未映射 → 缺席 + 日志可见（不许静默）；
- 报价落库口径（空写 NULL 不回填）+ 详情 VO 槽位；
- 「交通」预算档在有报价时用观测值并挂证据 remark；
- 实时价端点三条错误边界（无出发地 400 / 无日期 400 / 配额尽 429）。
"""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.agent.data import city_iata, live_quotes
from app.agent.tools import flight_quotes
from app.api.business.auth import auth_router
from app.api.business.itinerary import router as itinerary_router
from app.common import cache_store, jwt_compat
from app.common.config import settings
from app.common.envelope import install_exception_handlers
from app.common.http_client import configure_clients
from app.db import session as db_session
from app.db.models import Base, ItineraryMain, SysUser
from app.services import budget_engine, day_persistence, itinerary_query

FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures" / "aviasales_prices_for_dates.json").read_text(encoding="utf-8")
)
SERP_FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures" / "serpapi_google_flights.json").read_text(encoding="utf-8")
)
IATA_SNAPSHOT = json.loads((Path(__file__).parent / "fixtures" / "city_iata_snapshot.json").read_text(encoding="utf-8"))
JWT_MATERIAL = "example-only-hs256-test-signing-material"

#: fixture 的往返行：MAD→BCN 出发 2026-11-07、返程 2026-11-14（含一行缺 price 的脏行）
ROUND_TRIP_FIXTURE = {
    "success": True,
    "currency": "cny",
    "data": [
        {
            "origin": "MAD",
            "destination": "BCN",
            "price": 1520,
            "airline": "VY",
            "flight_number": "VY 6402",
            "departure_at": "2026-11-07",
            "return_at": "2026-11-14",
            "transfers": 0,
            "link": "/search/MAD0711BCN1",
        },
        {
            "origin": "MAD",
            "destination": "BCN",
            "price": 2180,
            "airline": "IB",
            "flight_number": "IB 6252",
            "departure_at": "2026-11-07",
            "return_at": "2026-11-14",
            "transfers": 1,
            "link": "/search/MAD0711BCN1?t=IB",
        },
        # 邻近日期的行：精确窗口过滤必须丢掉它
        {
            "origin": "MAD",
            "destination": "BCN",
            "price": 990,
            "airline": "FR",
            "flight_number": "FR 1",
            "departure_at": "2026-11-08",
            "return_at": "2026-11-15",
            "transfers": 0,
            "link": "/search/OTHER",
        },
    ],
}


def _mock(handler) -> None:
    configure_clients(api=httpx.Client(transport=httpx.MockTransport(handler)))


# ---- 城市 → IATA 映射 ---------------------------------------------------------


def test_resolve_iata_known_and_unknown():
    assert city_iata.resolve_iata("上海") == "PVG"
    assert city_iata.resolve_iata(" 巴黎 ") == "PAR"
    assert city_iata.resolve_iata("上海市") == "PVG", "去掉「市」后缀归一"
    assert city_iata.resolve_iata("某个不存在的小城") is None, "未命中一律 None，不猜"
    assert city_iata.resolve_iata("") is None
    assert city_iata.resolve_iata(None) is None


def test_iata_codes_are_three_letter_and_unique_by_value():
    """表内代码形状校验（正确性由人工责任，形状由机检兜住）。"""
    for city in city_iata.known_cities():
        code = city_iata.resolve_iata(city)
        assert code is not None and len(code) == 3 and code.isalpha() and code.isupper(), f"{city} → {code}"


def test_iata_codes_exist_in_validated_snapshot():
    """LA3 存在性校验：表里每个代码必须在离线校验快照中有据（形状校验之上的事实层）。

    快照由 `uv run python scripts/iata_snapshot.py` 对 Travelpayouts 免费数据集
    （airports/cities/routes）离线生成，只含被数据集背书的条目；表与快照双向
    一致——改表必须重跑脚本刷新快照，否则红。校验不过的码不进快照，此时红 =
    事实断言被数据集否决（"映射错机场是事实错误"的机检半边，另半边是人工责任）。
    """
    snapshot_codes = set(IATA_SNAPSHOT["entries"])
    table_codes = {city_iata.resolve_iata(city) for city in city_iata.known_cities()}
    assert None not in table_codes
    assert table_codes == snapshot_codes, (
        "city_iata 表与校验快照不一致：改了表就重跑 scripts/iata_snapshot.py 刷新快照；"
        "重跑后仍缺 = 该代码过不了数据集校验，人工复核、勿强行入表"
    )


# ---- 三分支取数 ---------------------------------------------------------------


def test_branch_quotes_present(monkeypatch):
    """分支一：有报价 → FlightQuote 口径行，价格升序、深链为绝对地址。"""
    monkeypatch.setattr(settings, "travelpayouts_token", "tp-token")
    _mock(lambda request: httpx.Response(200, json=ROUND_TRIP_FIXTURE))
    try:
        rows = flight_quotes.search_flight_quotes("马德里", "巴塞罗那", "2026-11-07", "2026-11-14")
    finally:
        configure_clients(api=None)
    assert len(rows) == 2, "邻近日期的行必须被精确窗口过滤掉"
    assert [r["price"] for r in rows] == [1520, 2180], "按价升序"
    first = rows[0]
    assert first["origin_iata"] == "MAD" and first["destination_iata"] == "BCN"
    assert first["depart_date"] == "2026-11-07" and first["return_date"] == "2026-11-14"
    assert first["provider"] == "aviasales"
    assert first["deep_link"].startswith("https://www.aviasales.com/search/")
    assert first["retrieved_at"] and first["expires_at"], "证据票必须带观测时点与时效"
    assert first["expires_at"] > first["retrieved_at"]
    assert first["currency"] == "cny"


def test_branch_no_token_means_zero_calls(monkeypatch, caplog):
    """分支二：无 token → 零外呼 + 缺席可见。"""
    monkeypatch.setattr(settings, "travelpayouts_token", "")
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        return httpx.Response(200, json=ROUND_TRIP_FIXTURE)

    _mock(handler)
    try:
        with caplog.at_level("WARNING", logger="app.agent.tools.impl"):
            rows = flight_quotes.search_flight_quotes("马德里", "巴塞罗那", "2026-11-07", "2026-11-14")
    finally:
        configure_clients(api=None)
    assert rows == []
    assert requests == [], "无 token 必须零外呼（硬纪律）"
    assert "no_token" in caplog.text, "缺席原因必须可见"


def test_branch_upstream_down_degrades_with_visible_reason(monkeypatch, caplog):
    """分支三：上游挂 → 空列表 + 日志可见（不静默、不冒充）。"""
    monkeypatch.setattr(settings, "travelpayouts_token", "tp-token")
    _mock(lambda request: httpx.Response(503))
    try:
        with caplog.at_level("WARNING", logger="app.agent.tools.impl"):
            rows = flight_quotes.search_flight_quotes("马德里", "巴塞罗那", "2026-11-07", "2026-11-14")
    finally:
        configure_clients(api=None)
    assert rows == []
    assert "no_quote_rows" in caplog.text


def test_unmapped_city_absent_and_visible(monkeypatch, caplog):
    """origin/destination 未映射 → 缺席 + 日志可见，**不换近似城市去查**。"""
    monkeypatch.setattr(settings, "travelpayouts_token", "tp-token")
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        return httpx.Response(200, json=ROUND_TRIP_FIXTURE)

    _mock(handler)
    try:
        with caplog.at_level("WARNING", logger="app.agent.tools.impl"):
            assert flight_quotes.search_flight_quotes("上海", "某个不存在的小城", "2026-11-07") == []
    finally:
        configure_clients(api=None)
    assert requests == [], "未映射即缺席，不得外呼"
    assert "city_not_mapped" in caplog.text


def test_no_origin_absent(monkeypatch, caplog):
    monkeypatch.setattr(settings, "travelpayouts_token", "tp-token")
    with caplog.at_level("WARNING", logger="app.agent.tools.impl"):
        assert flight_quotes.search_flight_quotes("", "巴黎", "2026-11-07") == []
    assert "no_origin" in caplog.text


# ---- 实时价（SerpApi）整形 ----------------------------------------------------


def test_live_quotes_shaping_and_google_link(monkeypatch):
    monkeypatch.setattr(settings, "serpapi_key", "serp-key")
    live_quotes.reset_quota_for_tests()
    _mock(lambda request: httpx.Response(200, json=SERP_FIXTURE))
    try:
        rows = flight_quotes.search_live_flight_quotes("巴黎", "伦敦", "2026-10-03", "2026-10-10")
    finally:
        configure_clients(api=None)
    assert rows, "有实时价时必须产出 FlightQuote 口径行"
    first = rows[0]
    assert first["provider"] == "google_flights"
    # origin_iata 记的是**查询用的**代码（城市码 PAR/LON，与 Aviasales 路径同口径）
    assert first["origin_iata"] == "PAR" and first["destination_iata"] == "LON"
    assert first["depart_date"] == "2026-10-03" and first["return_date"] == "2026-10-10"
    assert first["deep_link"].startswith("https://www.google.com/travel/flights?q="), "核实用搜索深链"
    assert "serp-key" not in json.dumps(rows), "api_key 绝不进任何返回字段"


def test_live_quotes_requires_date_window(monkeypatch, caplog):
    monkeypatch.setattr(settings, "serpapi_key", "serp-key")
    with caplog.at_level("WARNING", logger="app.agent.tools.impl"):
        assert flight_quotes.search_live_flight_quotes("巴黎", "伦敦", "2026-10-03", None) == []
    assert "no_date_window" in caplog.text


# ---- 持久化 / 详情 VO / 预算 --------------------------------------------------


@pytest.fixture()
def db(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'l14.db'}")
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
    yield
    db_session.init_engine(None, None)
    cache_store.reset_for_tests()


_QUOTE = {
    "origin_iata": "PVG",
    "destination_iata": "PAR",
    "origin_city": "上海",
    "destination_city": "巴黎",
    "depart_date": "2026-11-07",
    "return_date": "2026-11-14",
    "airline": "AF",
    "flight_number": "AF 111",
    "transfers": 0,
    "price": 4200.0,
    "currency": "cny",
    "provider": "aviasales",
    "source_url": "https://www.aviasales.com/search/x",
    "retrieved_at": "2026-09-25T10:00:00+08:00",
    "expires_at": "2026-09-26T10:00:00+08:00",
    "deep_link": "https://www.aviasales.com/search/x",
}


def test_save_quotes_and_detail_slot(db):
    day_persistence.save_flight_quotes(1, [_QUOTE])
    day_persistence.save_origin_city(1, "上海")
    payload = itinerary_query.build_detail(1, 1)
    assert payload["originCity"] == "上海"
    assert len(payload["flightQuotes"]) == 1
    assert payload["flightQuotes"][0]["price"] == 4200.0
    assert payload["flightQuotes"][0]["deepLink"].endswith("/search/x")


def test_save_quotes_empty_clears_instead_of_keeping_stale(db):
    """空即写 NULL：绝不保留上一轮的旧价（观测事实不许回填）。"""
    day_persistence.save_flight_quotes(1, [_QUOTE])
    assert itinerary_query.build_detail(1, 1)["flightQuotes"]
    day_persistence.save_flight_quotes(1, [])
    assert itinerary_query.build_detail(1, 1)["flightQuotes"] == []


def test_save_origin_city_never_clears_existing(db):
    day_persistence.save_origin_city(1, "上海")
    day_persistence.save_origin_city(1, None)
    day_persistence.save_origin_city(1, "  ")
    assert itinerary_query.build_detail(1, 1)["originCity"] == "上海", "用户填的出发地不该被生成流程抹掉"


def test_budget_transport_uses_observed_price_with_evidence(db):
    day_persistence.save_flight_quotes(1, [_QUOTE])
    rows = budget_engine.recalculate(1)
    transport = next(r for r in rows if r.category == "交通")
    assert float(transport.amount) == 8400.0, "4200 × 2 人"
    assert "aviasales" in (transport.remark or "")
    assert "观测" in (transport.remark or ""), "证据口径必须写明是观测价而非估算"
    assert "2026-09-25" in (transport.remark or ""), "观测时点随行"


def test_budget_transport_falls_back_to_estimate_without_quotes(db):
    rows = budget_engine.recalculate(1)
    transport = next(r for r in rows if r.category == "交通")
    assert float(transport.amount) == 35.0 * 3 * 2, "无报价维持城市系数估算"
    assert transport.remark == "按城市系数×天数×人数"


# ---- 实时价端点 ---------------------------------------------------------------


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


def test_live_endpoint_requires_origin(client):
    resp = client.post("/api/itinerary/1/quotes/live", json={})
    assert resp.status_code == 400
    assert "出发地" in resp.json()["message"]


def test_live_endpoint_quota_exhausted_returns_429(client, monkeypatch):
    day_persistence.save_origin_city(1, "上海")
    monkeypatch.setattr(settings, "serpapi_key", "serp-key")
    monkeypatch.setattr(settings, "serpapi_monthly_quota", 1)
    live_quotes.reset_quota_for_tests()
    live_quotes._spend.update(month=live_quotes.current_month(), used=1)
    resp = client.post("/api/itinerary/1/quotes/live", json={})
    assert resp.status_code == 429
    assert "配额" in resp.json()["message"]
    live_quotes.reset_quota_for_tests()


def test_live_endpoint_returns_quotes(client, monkeypatch):
    day_persistence.save_origin_city(1, "上海")
    monkeypatch.setattr(settings, "serpapi_key", "serp-key")
    live_quotes.reset_quota_for_tests()
    _mock(lambda request: httpx.Response(200, json=SERP_FIXTURE))
    try:
        resp = client.post("/api/itinerary/1/quotes/live", json={})
    finally:
        configure_clients(api=None)
    assert resp.status_code == 200
    body = resp.json()["data"]
    assert body["flightQuotes"], "有实时价时返回报价行"
    assert body["originCity"] == "上海"
    assert body["startDate"] == "2026-11-07" and body["endDate"] == "2026-11-14"
    assert body["reason"] is None


def test_live_endpoint_empty_result_carries_reason(client, monkeypatch):
    """问过但没有 → 200 + 空列表 + reason（与"查不了"的 400 分开）。"""
    day_persistence.save_origin_city(1, "上海")
    monkeypatch.setattr(settings, "serpapi_key", "serp-key")
    live_quotes.reset_quota_for_tests()
    _mock(lambda request: httpx.Response(200, json={"best_flights": [], "other_flights": []}))
    try:
        resp = client.post("/api/itinerary/1/quotes/live", json={})
    finally:
        configure_clients(api=None)
    assert resp.status_code == 200
    body = resp.json()["data"]
    assert body["flightQuotes"] == []
    assert body["reason"]


def test_live_endpoint_does_not_persist(client, monkeypatch):
    """实时价是"看一眼"，不落库——不冲掉生成时观测到的聚合价。"""
    day_persistence.save_flight_quotes(1, [_QUOTE])
    day_persistence.save_origin_city(1, "上海")
    monkeypatch.setattr(settings, "serpapi_key", "serp-key")
    live_quotes.reset_quota_for_tests()
    _mock(lambda request: httpx.Response(200, json=SERP_FIXTURE))
    try:
        assert client.post("/api/itinerary/1/quotes/live", json={}).status_code == 200
    finally:
        configure_clients(api=None)
    stored = itinerary_query.build_detail(1, 1)["flightQuotes"]
    assert len(stored) == 1 and stored[0]["provider"] == "aviasales", "落库的仍是生成时的聚合价"
