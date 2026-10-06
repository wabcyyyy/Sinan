"""前端错误探针端点测试（SQLite，无网络/Redis）。

断言焦点：
1. 匿名可达：无会话 POST 200（登录页崩溃是探针存在的首要理由）；
2. 有会话时身份进日志（caplog 断言 username），响应仍恒 ok 信封；
3. 字段上限：message>2000 / stack>8000 / source>40 均 422；
4. 按 IP 分钟窗：同 IP 第 31 次起 429（sliding_hit 与登录/分享限速同一实现）。
"""

from __future__ import annotations

import logging

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.api.business.auth import auth_router, user_router
from app.api.business.client_errors import router as client_errors_router
from app.common.config import settings
from app.common.envelope import install_exception_handlers
from app.db import session as db_session
from app.db.models import Base, SysUser
from app.services import state_and_sessions, user_service

JWT_MATERIAL = "example-only-hs256-test-signing-material"


@pytest.fixture(autouse=True)
def db(monkeypatch, tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'client_errors.db'}")
    Base.metadata.create_all(engine)
    db_session.init_engine(engine, sessionmaker(bind=engine, expire_on_commit=False))
    monkeypatch.setattr(settings, "jwt_secret", JWT_MATERIAL)
    hashed = user_service.hash_password("x")
    with db_session.session_scope() as session:
        session.add(SysUser(username="alice", password=hashed, status=1, role="user"))
    # 限速窗口是跨用例共享的进程内状态（conftest 钉死 Redis 不可达，走本地回落表）：
    # 每个用例前重置，保证 31 次连发的断言不受前面用例的 2 次上报影响
    state_and_sessions.reset_for_tests()
    yield
    db_session.init_engine(None, None)
    state_and_sessions.reset_for_tests()


def _client(authenticated: bool = False) -> TestClient:
    app = FastAPI()
    install_exception_handlers(app)
    app.include_router(auth_router)
    app.include_router(user_router)
    app.include_router(client_errors_router)
    client = TestClient(app, follow_redirects=False)
    if authenticated:
        assert client.post("/api/auth/login", json={"username": "alice", "password": "x"}).status_code == 200
    return client


REPORT = {
    "message": "Uncaught TypeError: boom is not a function",
    "stack": "TypeError: boom is not a function\n    at HomePage (HomePage.tsx:1:1)",
    "source": "window",
    "path": "/",
    "userAgent": "vitest",
    "ts": "2026-09-30T00:00:00.000Z",
}


def test_anonymous_report_is_accepted() -> None:
    response = _client().post("/api/client-errors", json=REPORT)
    assert response.status_code == 200
    assert response.json()["code"] == 200


def test_authenticated_report_logs_username(caplog) -> None:
    with caplog.at_level(logging.ERROR, logger="app.client_errors"):
        response = _client(authenticated=True).post("/api/client-errors", json=REPORT)
    assert response.status_code == 200
    record = next(r for r in caplog.records if r.name == "app.client_errors")
    assert "user=alice" in record.getMessage()
    assert REPORT["message"] in record.getMessage()


@pytest.mark.parametrize(
    ("field", "size"),
    [("message", 2001), ("stack", 8001), ("source", 41)],
)
def test_oversized_fields_are_rejected(field: str, size: int) -> None:
    payload = {**REPORT, field: "x" * size}
    # 仓库统一口径：校验失败经 install_exception_handlers 归一为 400 信封（非裸 422）
    assert _client().post("/api/client-errors", json=payload).status_code == 400


def test_per_ip_minute_window_throttles() -> None:
    client = _client()
    for _ in range(30):
        assert client.post("/api/client-errors", json=REPORT).status_code == 200
    assert client.post("/api/client-errors", json=REPORT).status_code == 429
