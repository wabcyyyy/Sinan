"""限流补缺（审查 P1-8）：quotes/live 用户闸 + agent 面按 token 速率桶。

两道闸都是"先过闸再动手"：quotes/live 直烧 SerpApi 共享月池，agent 面是
token 泄露后的止损。全离线：agent 面只打 /v1/tools（读注册表，不外呼）。
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.api import agent as agent_api
from app.api.business.auth import auth_router
from app.api.business.itinerary import router as itinerary_router
from app.common import cache_store
from app.common.config import settings
from app.common.envelope import install_exception_handlers
from app.db import session as db_session
from app.db.models import Base, SysUser
from app.services import state_and_sessions, user_service

PASSWORD = "example123"


@pytest.fixture
def business_client(monkeypatch, tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'rate.db'}")
    Base.metadata.create_all(engine)
    db_session.init_engine(engine, sessionmaker(bind=engine, expire_on_commit=False))
    monkeypatch.setattr(settings, "jwt_secret", "example-only-hs256-test-signing-material")
    monkeypatch.setattr(settings, "redis_url", "redis://127.0.0.1:1/0")
    cache_store.reset_for_tests()
    state_and_sessions.reset_for_tests()
    with db_session.session_scope() as session:
        session.add(SysUser(username="alice", password=user_service.hash_password(PASSWORD), status=1, role="user"))
    app = FastAPI()
    install_exception_handlers(app)
    app.include_router(auth_router)
    app.include_router(itinerary_router)
    client = TestClient(app, follow_redirects=False)
    client.post("/api/auth/login", json={"username": "alice", "password": PASSWORD})
    yield client
    db_session.init_engine(None, None)
    cache_reset = cache_store.reset_for_tests
    cache_reset()


def test_live_quote_endpoints_are_user_rate_limited(business_client, monkeypatch) -> None:
    """分钟窗先于查价逻辑：第 N+1 次直接 429，不再打 SerpApi。"""
    monkeypatch.setattr(settings, "user_live_quotes_per_minute", 2)
    monkeypatch.setattr(settings, "serpapi_key", "")  # 未配 key：查不了 → 400
    first = business_client.post("/api/itinerary/999/quotes/live", json={})
    second = business_client.post("/api/itinerary/999/hotel-quotes/live", json={})
    third = business_client.post("/api/itinerary/999/quotes/live", json={})
    # 前两次过闸（行程不存在 → 404 由查询层判定），第三次被闸门拦下
    assert first.status_code == 404 and second.status_code == 404
    assert third.status_code == 429
    assert "频繁" in third.json()["message"]

    # 两个端点共用同一只用户窗口：换了端点也照样拦（共享月池的语义）
    fourth = business_client.post("/api/itinerary/999/hotel-quotes/live", json={})
    assert fourth.status_code == 429


def test_agent_face_is_rate_limited_per_token(monkeypatch) -> None:
    """agent 面按 token 分桶限速；探活端点保持匿名且不限速。"""
    import main

    monkeypatch.setattr(settings, "agent_internal_token", "token-A")
    monkeypatch.setattr(settings, "agent_rate_limit_per_minute", 2)
    agent_api.reset_agent_rate_for_tests()
    client = TestClient(main.app)
    headers = {"X-Agent-Token": "token-A"}

    assert client.get("/api/agent/v1/tools", headers=headers).status_code == 200
    assert client.get("/api/agent/v1/tools", headers=headers).status_code == 200
    blocked = client.get("/api/agent/v1/tools", headers=headers)
    assert blocked.status_code == 429, "第 3 次超过 2/min 的桶"
    assert "token" in blocked.json()["detail"] or "many" in blocked.json()["detail"]

    # 另一个 token 是另一只桶（互不牵连）
    monkeypatch.setattr(settings, "agent_internal_token", "token-A,token-B")
    other = client.get("/api/agent/v1/tools", headers={"X-Agent-Token": "token-B"})
    assert other.status_code in (200, 401), "独立桶不该被 token-A 的计数牵连"

    # 探活端点：不带令牌、不限速（Dockerfile HEALTHCHECK 前提）
    for _ in range(5):
        assert client.get("/api/agent/health").status_code == 200
    agent_api.reset_agent_rate_for_tests()


def test_agent_face_rejects_bad_token_before_counting(monkeypatch) -> None:
    """顺序是"先认人再限速"：错令牌 → 401，且不占用别人的桶。"""
    import main

    monkeypatch.setattr(settings, "agent_internal_token", "token-A")
    monkeypatch.setattr(settings, "agent_rate_limit_per_minute", 1)
    agent_api.reset_agent_rate_for_tests()
    client = TestClient(main.app)

    assert client.get("/api/agent/v1/tools", headers={"X-Agent-Token": "wrong"}).status_code == 401
    # 正确令牌仍有完整额度（错令牌的 401 没有记进它的桶）
    assert client.get("/api/agent/v1/tools", headers={"X-Agent-Token": "token-A"}).status_code == 200
    agent_api.reset_agent_rate_for_tests()
