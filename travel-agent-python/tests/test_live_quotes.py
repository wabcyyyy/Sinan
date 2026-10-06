"""live_quotes 单测（L12）：无 key 零外呼 / 月配额计数（fake clock 语义）/ 字段容错。"""

from __future__ import annotations

import json
from pathlib import Path

import httpx

from app.agent.data import live_quotes
from app.common.config import settings
from app.common.http_client import configure_clients

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "serpapi_google_flights.json").read_text(encoding="utf-8"))


def test_no_key_means_zero_calls(monkeypatch):
    monkeypatch.setattr(settings, "serpapi_key", "")
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        return httpx.Response(200, json=FIXTURE)

    configure_clients(api=httpx.Client(transport=httpx.MockTransport(handler)))
    try:
        assert live_quotes.fetch_live_quotes("CDG", "LHR", "2026-10-03", "2026-10-10") is None
        assert requests == [], "无 key 必须零外呼"
    finally:
        configure_clients(api=None)


def test_fetch_merges_buckets_and_drops_priceless(monkeypatch):
    monkeypatch.setattr(settings, "serpapi_key", "serp-key-xyz")
    live_quotes.reset_quota_for_tests()

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["engine"] == "google_flights"
        assert request.url.params["departure_id"] == "CDG"
        return httpx.Response(200, json=FIXTURE)

    configure_clients(api=httpx.Client(transport=httpx.MockTransport(handler)))
    try:
        rows = live_quotes.fetch_live_quotes("CDG", "LHR", "2026-10-03", "2026-10-10")
    finally:
        configure_clients(api=None)
    assert rows is not None and len(rows) == 2, "fixture 中 price=null 的 itinerary 必须丢弃"
    assert rows[0]["source_bucket"] == "best_flights" and rows[0]["price"] == 1180
    assert rows[1]["source_bucket"] == "other_flights"
    assert rows[1]["flights"][0]["departure_airport"]["id"] == "ORY"
    assert all("booking_token" in row for row in rows), "booking_token 是续查出口，必须透传"


def test_monthly_quota_stops_burn(monkeypatch):
    """配额尽快速返回 None 且零外呼；计数只认真实外呼（缓存命中不扣）。"""
    monkeypatch.setattr(settings, "serpapi_key", "serp-key-xyz")
    monkeypatch.setattr(settings, "serpapi_monthly_quota", 2)
    live_quotes.reset_quota_for_tests()
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        return httpx.Response(200, json=FIXTURE)

    configure_clients(api=httpx.Client(transport=httpx.MockTransport(handler)))
    try:
        assert live_quotes.fetch_live_quotes("CDG", "LHR", "2026-10-03", "2026-10-10") is not None
        assert live_quotes.fetch_live_quotes("CDG", "LHR", "2026-10-04", "2026-10-11") is not None
        assert live_quotes.quota_used() == 2
        # 配额尽：零外呼直接 None
        assert live_quotes.fetch_live_quotes("CDG", "LHR", "2026-10-05", "2026-10-12") is None
        assert len(requests) == 2
        # 同参数复查命中进程内缓存：同样不再外呼、不再扣配额
        assert live_quotes.fetch_live_quotes("CDG", "LHR", "2026-10-03", "2026-10-10") is not None
        assert len(requests) == 2
    finally:
        configure_clients(api=None)


def test_month_rollover_resets_counter(monkeypatch):
    """月份翻转清零（重启清零的同构口径）：上个月的计数不追到这个月。"""
    monkeypatch.setattr(settings, "serpapi_key", "serp-key-xyz")
    monkeypatch.setattr(settings, "serpapi_monthly_quota", 2)
    live_quotes.reset_quota_for_tests()
    live_quotes._spend.update(month="2000-01", used=2)
    monkeypatch.setattr(live_quotes, "current_month", lambda: "2000-02")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=FIXTURE)

    configure_clients(api=httpx.Client(transport=httpx.MockTransport(handler)))
    try:
        assert live_quotes.quota_exhausted() is False, "新月份不继承上个月计数"
        assert live_quotes.fetch_live_quotes("CDG", "LHR", "2026-10-03", "2026-10-10") is not None
        assert live_quotes.quota_used() == 1
    finally:
        configure_clients(api=None)


def test_429_and_error_field_degrade_to_none(monkeypatch, caplog):
    monkeypatch.setattr(settings, "serpapi_key", "serp-key-xyz")
    live_quotes.reset_quota_for_tests()

    configure_clients(api=httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(429))))
    try:
        with caplog.at_level("WARNING", logger="app.common.external_client"):
            assert live_quotes.fetch_live_quotes("CDG", "LHR", "2026-10-03", "2026-10-10") is None
        assert "serp-key-xyz" not in caplog.text, "api_key 走 query，异常日志必须脱敏"
    finally:
        configure_clients(api=None)

    live_quotes.reset_quota_for_tests()
    live_quotes._serpapi_client.clear_cache()
    configure_clients(
        api=httpx.Client(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, json={"error": "You have exhausted your API key search limit"})
            )
        )
    )
    try:
        assert live_quotes.fetch_live_quotes("CDG", "LHR", "2026-10-03", "2026-10-10") is None
    finally:
        configure_clients(api=None)
