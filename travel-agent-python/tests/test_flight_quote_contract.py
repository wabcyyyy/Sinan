"""L13 契约槽位测试：报价相关 schema 一次性扩展的形状与零破坏。

契约卡的本职：全字段 optional/带默认（旧客户端零破坏）、wire 键 camelCase、
detail VO 的 flightQuotes 槽位常置空列表（键缺席 ≠ 空列表，对消费方是两种契约）。
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import session as db_session
from app.db.models import Base, ItineraryMain, SysUser
from app.schemas.business.itinerary import GenerateTripRequest
from app.schemas.trip import FactEvidence, FlightQuote, GenerateRequest, HotelOption
from app.services import itinerary_query

# ---- FlightQuote：全默认 + wire 形状 -----------------------------------------


def test_flight_quote_all_fields_optional_with_defaults():
    dumped = FlightQuote().model_dump(by_alias=True)
    assert set(dumped) == {
        "originIata",
        "destinationIata",
        "originCity",
        "destinationCity",
        "departDate",
        "returnDate",
        "airline",
        "flightNumber",
        "transfers",
        "price",
        "currency",
        "provider",
        "sourceUrl",
        "retrievedAt",
        "expiresAt",
        "deepLink",
    }
    assert all(value is None for value in dumped.values()), "全字段 optional：缺省即 null（wire 全键语义）"


def test_flight_quote_observed_round_trip():
    quote = FlightQuote(
        origin_iata="MAD",
        destination_iata="BCN",
        airline="VY",
        flight_number="VY 6402",
        price=52.0,
        currency="CNY",
        provider="aviasales",
        retrieved_at="2026-09-25T10:00:00+08:00",
        expires_at="2026-09-26T10:00:00+08:00",
        deep_link="https://www.aviasales.com/search/MAD0711BCN1",
    )
    assert quote.model_dump(by_alias=True)["originIata"] == "MAD"
    assert FlightQuote.model_validate(quote.model_dump(by_alias=True)) == quote, "camelCase wire 往返一致"


# ---- GenerateRequest.origin_city / GenerateTripRequest.originCity ------------


def test_generate_request_origin_city_optional():
    assert GenerateRequest(city="巴黎").origin_city is None, "不带 origin 的旧请求零破坏"
    assert GenerateRequest(city="巴黎", origin_city="上海").origin_city == "上海"
    with pytest.raises(ValidationError):
        GenerateRequest(city="巴黎", origin_city="x" * 65)  # 线级上限 64


def test_generate_trip_request_origin_city_camel_slot():
    assert GenerateTripRequest.model_validate({"city": "巴黎", "originCity": "上海"}).originCity == "上海"
    assert GenerateTripRequest.model_validate({"city": "巴黎"}).originCity is None


# ---- HotelOption.price_fact：复用 FactEvidence 单一真源 -----------------------


def _bare_option() -> HotelOption:
    return HotelOption(
        id="h1",
        hotel_name="Sample",
        tier="舒适型",
        base_price=100.0,
        season_factor=1.0,
        season_label="平季",
        nightly_price=100.0,
        nights=3,
        rooms=1,
        total_price=300.0,
        within_budget=True,
        reason="距离近",
    )


def test_hotel_option_price_fact_defaults_to_none_and_round_trips():
    option = _bare_option()
    assert option.price_fact is None, "估价链路不带证据票（L15 才填 observed）"
    option.price_fact = FactEvidence(
        provider="hotellook",
        source_url="https://engine.hotellook.com/api/v2/cache.json",
        retrieved_at="2026-09-25T10:00:00+08:00",
        verification_status="verified",
        value_kind="observed",
    )
    wire = option.model_dump(by_alias=True)
    assert wire["priceFact"]["valueKind"] == "observed"


# ---- 详情 VO 的 flightQuotes 槽位 ---------------------------------------------


@pytest.fixture()
def detail_db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'l13.db'}")
    Base.metadata.create_all(engine)
    db_session.init_engine(engine, sessionmaker(bind=engine, expire_on_commit=False))
    from app.services import user_service

    with db_session.session_scope() as session:
        session.add(
            SysUser(username="carol", password=user_service.hash_password("x"), nickname=None, status=1, role="user")
        )
        session.flush()
        session.add(
            ItineraryMain(
                user_id=1,
                title="巴黎3日",
                city="巴黎",
                start_date=date(2026, 10, 1),
                end_date=date(2026, 10, 3),
                days=3,
                persons=1,
                budget=Decimal("5000.00"),
                status=2,
            )
        )
    yield
    db_session.init_engine(None, None)


def test_detail_vo_carries_empty_flight_quotes_slot(detail_db):
    """键常在、值常为空列表：L14 填充前，消费方（前端 QuoteStrip）按空不渲染。"""
    payload = itinerary_query.build_detail(1, 1)
    assert payload["flightQuotes"] == []
