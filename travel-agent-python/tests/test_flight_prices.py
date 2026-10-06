"""flight_prices 单测（L12）：无 key 零外呼 / token 不进 URL / 信封与行容错。"""

from __future__ import annotations

import json
from pathlib import Path

import httpx

from app.agent.data import flight_prices
from app.common.config import settings
from app.common.http_client import configure_clients

FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures" / "aviasales_prices_for_dates.json").read_text(encoding="utf-8")
)


def test_no_token_means_zero_calls(monkeypatch):
    monkeypatch.setattr(settings, "travelpayouts_token", "")
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        return httpx.Response(200, json=FIXTURE)

    configure_clients(api=httpx.Client(transport=httpx.MockTransport(handler)))
    try:
        assert flight_prices.fetch_price_dates("MAD", "BCN", "2026-11") is None
        assert requests == [], "无 token 必须零外呼"
    finally:
        configure_clients(api=None)


def test_fetch_normalizes_rows_and_keeps_token_out_of_url(monkeypatch):
    """X-Access-Token 头通道：token 不得出现在 URL；行透传 + currency/deep_link 补列。"""
    monkeypatch.setattr(settings, "travelpayouts_token", "tp-token-abc")
    urls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        urls.append(str(request.url))
        assert "token" not in request.url.params, "token 走 X-Access-Token 头，不进 URL"
        assert request.headers["X-Access-Token"] == "tp-token-abc"
        return httpx.Response(200, json=FIXTURE)

    configure_clients(api=httpx.Client(transport=httpx.MockTransport(handler)))
    try:
        rows = flight_prices.fetch_price_dates("mad", "bcn", "2026-11")
    finally:
        configure_clients(api=None)
    assert len(urls) == 1
    sent = httpx.URL(urls[0]).params
    assert sent["origin"] == "MAD" and sent["destination"] == "BCN"
    assert sent["departure_at"] == "2026-11" and sent["currency"] == "cny"
    assert sent["sorting"] == "price" and sent["one_way"] == "true" and sent["limit"] == "30"
    assert "token" not in sent
    assert rows is not None and len(rows) == 2, "fixture 第 3 行缺 price，必须整行丢弃"
    first = rows[0]
    assert first["currency"] == "usd", "currency 来自应答信封"
    assert first["deep_link"].startswith("https://www.aviasales.com/search/"), "相对 link 拼成绝对深链"
    assert first["airline"] == "VY" and first["price"] == 52


def test_bad_envelopes_degrade_to_none(monkeypatch):
    monkeypatch.setattr(settings, "travelpayouts_token", "tp-token-abc")
    bad_envelopes = [
        {"success": False, "data": []},
        {"success": True},
        {"success": True, "data": "not-a-list"},
        "totally-wrong",
    ]
    for bad in bad_envelopes:
        configure_clients(
            api=httpx.Client(transport=httpx.MockTransport(lambda request, b=bad: httpx.Response(200, json=b)))
        )
        try:
            assert flight_prices.fetch_price_dates("MAD", "BCN", "2026-11") is None, f"坏信封必须降 None: {bad}"
        finally:
            configure_clients(api=None)


def test_all_rows_invalid_returns_empty_list(monkeypatch):
    """信封健康但全部行残缺 = 问到了且没有可用报价 → []（不是 None）。"""
    monkeypatch.setattr(settings, "travelpayouts_token", "tp-token-abc")
    payload = {"success": True, "currency": "usd", "data": [{"airline": "X", "transfers": 1}]}
    configure_clients(
        api=httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload)))
    )
    try:
        rows = flight_prices.fetch_price_dates("MAD", "BCN", "2026-11")
    finally:
        configure_clients(api=None)
    assert rows == []


def test_flight_prices_enabled_reflects_token(monkeypatch):
    monkeypatch.setattr(settings, "travelpayouts_token", "  ")
    assert flight_prices.flight_prices_enabled() is False
    monkeypatch.setattr(settings, "travelpayouts_token", "t")
    assert flight_prices.flight_prices_enabled() is True
