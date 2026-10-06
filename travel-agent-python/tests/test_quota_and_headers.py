"""按用户的 LLM 花费闸门（R1-9）与全站安全响应头（R1-3）。

配额部分钉的是"每个消耗 LLM 的业务入口都真的调了闸门"——只在 service 里
写一个 enforce() 而没人调用，等于没有闸门。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import main as entry
from app.api.deps import AuthUser
from app.api.security import enforce_business_auth
from app.common.config import settings
from app.common.envelope import ApiError
from app.services import quota_service

_LLM_HANDLERS = (
    "post_clarify",
    "post_city_guide",
    "post_poi_nearby",
    "post_generate",
    "post_nl_edit",
    "chat_edit",
    "chat_edit_stream",
)


def _unique_uid() -> int:
    """限速窗口在离线套件里是进程内共享表，用互不相干的 key 隔开用例。"""
    _unique_uid.counter += 1  # type: ignore[attr-defined]
    return 900_000 + _unique_uid.counter  # type: ignore[attr-defined]


_unique_uid.counter = 0  # type: ignore[attr-defined]


def test_minute_window_blocks_the_extra_run(monkeypatch) -> None:
    monkeypatch.setattr(settings, "user_llm_runs_per_minute", 3)
    monkeypatch.setattr(settings, "user_daily_llm_runs", 10_000)
    uid = _unique_uid()
    for _ in range(3):
        quota_service.enforce_llm_budget(uid)
    with pytest.raises(ApiError) as exc:
        quota_service.enforce_llm_budget(uid)
    assert exc.value.status == 429


def test_daily_window_blocks_the_extra_run(monkeypatch) -> None:
    monkeypatch.setattr(settings, "user_llm_runs_per_minute", 10_000)
    monkeypatch.setattr(settings, "user_daily_llm_runs", 2)
    uid = _unique_uid()
    quota_service.enforce_llm_budget(uid)
    quota_service.enforce_llm_budget(uid)
    with pytest.raises(ApiError) as exc:
        quota_service.enforce_llm_budget(uid)
    assert exc.value.status == 429


def test_every_llm_handler_calls_the_gate() -> None:
    """co_names 证明"处理器里真的调了这个方法"，而不是注释里提了一句。"""
    from app.api.business import itinerary as itinerary_api
    from app.api.business import pois as pois_api

    for name in _LLM_HANDLERS:
        handler = getattr(itinerary_api, name)
        assert "enforce_llm_budget" in handler.__code__.co_names, name
    # /api/pois 的补池同样烧联网搜索 LLM（审查 P1-2），同口径钉住
    assert "enforce_llm_budget" in pois_api.list_pois.__code__.co_names


def test_gate_fires_before_any_generation_work(monkeypatch) -> None:
    """以 /clarify 为样：超限请求必须 429，且不会走到 agent 能力上。"""
    from app.services import itinerary_city

    called: list[str] = []
    monkeypatch.setattr(itinerary_city, "clarify", lambda *_a, **_k: called.append("clarify"))
    monkeypatch.setattr(settings, "user_llm_runs_per_minute", 1)
    uid = _unique_uid()  # 同一个用户才会落在同一个窗口里
    # 直接改装配后 app 的 overrides：TestClient.app 是 ASGI2 包装，拿不到该属性
    entry.app.dependency_overrides[enforce_business_auth] = lambda: AuthUser(id=uid, username="q", role="user")
    client = TestClient(entry.app)
    try:
        assert client.post("/api/itinerary/clarify", json={"message": "三天"}).status_code == 200
        assert client.post("/api/itinerary/clarify", json={"message": "三天"}).status_code == 429
    finally:
        entry.app.dependency_overrides.clear()
    assert called == ["clarify"]


def test_poi_search_gate_fires_before_any_work(monkeypatch) -> None:
    """/api/pois 的镜像（P1-2）：补池烧联网搜索 LLM，超限必须 429 且不打到检索上。"""
    from app.services import poi_search

    called: list[str] = []
    monkeypatch.setattr(poi_search, "search_local", lambda *_a, **_k: called.append("search_local"))
    monkeypatch.setattr(settings, "user_llm_runs_per_minute", 1)
    uid = _unique_uid()
    entry.app.dependency_overrides[enforce_business_auth] = lambda: AuthUser(id=uid, username="q", role="user")
    client = TestClient(entry.app)
    try:
        assert client.get("/api/pois", params={"city": "北京"}).status_code == 200
        assert client.get("/api/pois", params={"city": "北京"}).status_code == 429
    finally:
        entry.app.dependency_overrides.clear()
    assert called == ["search_local"]


def test_security_headers_are_on_every_response() -> None:
    client = TestClient(entry.app)
    for path, expected_status in (("/api/test/hello", 200), ("/api/itinerary", 401)):
        response = client.get(path)
        assert response.status_code == expected_status, path
        assert response.headers["x-content-type-options"] == "nosniff", path
        assert response.headers["x-frame-options"] == "DENY", path
        assert "frame-ancestors" in response.headers["content-security-policy"], path
