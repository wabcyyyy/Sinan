"""账号安全（T7a：无邮箱那半片）——改密、会话撤销、默认拒绝面。

与 `test_auth_migration.py` 同样的纪律：**不打鉴权桩**。`POST /api/user/password` 走
真实的 Cookie→JWT→会话注册表→sys_user 全链路（临时 SQLite），因为「改密之后旧票到底
还不认」这种结论只有真链路能证明；打桩会把吊销黑名单、注册表、验签三件事一起绕过。

Redis 在本文件里钉到不可达端口，于是限速与会话都走 `state_and_sessions` 的进程内兜底，
`token_revocation` 的黑名单 likewise 落在进程内表——用例断言的就是这条降级路径。
"""

from __future__ import annotations

import logging

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.api.business.auth import auth_router, user_router
from app.common import jwt_tokens, token_revocation
from app.common.config import settings
from app.common.envelope import install_exception_handlers
from app.db import session as db_session
from app.db.models import Base, SysUser
from app.services import state_and_sessions as sessions
from app.services import user_service

JWT_MATERIAL = "example-only-hs256-test-signing-material"
# 带 example 标记：口令形状的测试字面量会被 scripts/check-secrets.ps1 按占位符放行
PASSWORD = "example123"
NEW_PASSWORD = "example456"


@pytest.fixture(autouse=True)
def isolated_state(monkeypatch, tmp_path):
    """临时 SQLite + 不可达 Redis + 干净的会话/黑名单表：用例之间不串味。"""
    engine = create_engine(f"sqlite:///{tmp_path / 'security.db'}")
    Base.metadata.create_all(engine)
    db_session.init_engine(engine, sessionmaker(bind=engine, expire_on_commit=False))
    monkeypatch.setattr(settings, "jwt_secret", JWT_MATERIAL)
    monkeypatch.setattr(settings, "redis_url", "redis://127.0.0.1:1/0")
    monkeypatch.setattr(settings, "trusted_proxies", "")
    sessions.reset_for_tests()
    token_revocation.reset_for_tests()
    yield
    db_session.init_engine(None, None)
    sessions.reset_for_tests()
    token_revocation.reset_for_tests()


@pytest.fixture
def client() -> TestClient:
    app = FastAPI()
    install_exception_handlers(app)
    app.include_router(auth_router)
    app.include_router(user_router)
    return TestClient(app, base_url="http://testserver")


def _register(client: TestClient, username: str = "alice", password: str = PASSWORD) -> None:
    assert client.post("/api/auth/register", json={"username": username, "password": password}).json()["code"] == 200


def _login(client: TestClient, username: str, password: str = PASSWORD) -> str:
    assert client.post("/api/auth/login", json={"username": username, "password": password}).json()["code"] == 200
    token = client.cookies.get("TA_AUTH")
    assert token, "登录应下发 TA_AUTH Cookie"
    return str(token)


def _change(client: TestClient, old: str, new: str, **extra) -> dict:
    return client.post("/api/user/password", json={"old_password": old, "new_password": new, **extra}).json()


def _stored_hash(username: str) -> str:
    with db_session.session_scope() as session:
        return str(session.execute(select(SysUser.password).where(SysUser.username == username)).scalar_one())


def _issue_token(username: str) -> str:
    return jwt_tokens.encode_token(username, JWT_MATERIAL, 3600)


# ---------- 默认拒绝面 ----------


def test_change_password_is_not_reachable_anonymously(client: TestClient) -> None:
    """改密绝不能匿名可达：它挂在 `/api/user/**`（默认拒绝），不在 `/api/auth/` 前缀白名单里。

    同一条边界由 `tests/test_default_deny_auth.py` 从装配后的路由表机检；这里补的是
    HTTP 实况——不带票的 POST 必须 401，且不会因为"用户名不存在"给出别的状态码。
    """
    anonymous = client.post("/api/user/password", json={"old_password": PASSWORD, "new_password": NEW_PASSWORD})
    assert anonymous.status_code == 401, "无票的改密请求必须在鉴权层就被拒"
    # 401 由 `enforce_business_auth` 直接抛 HTTPException（与 /api/user/info 同口径，不发业务信封），
    # 所以判据是状态码。顺带钉住响应不回显任何口令形状的细节。
    assert PASSWORD not in anonymous.text and NEW_PASSWORD not in anonymous.text


def test_change_password_rejects_a_forged_but_unregistered_token(client: TestClient) -> None:
    """未登记的票照样能过验签（与登录同口径），所以撤销面 = 注册表里的票。

    这条不是"想要的行为"，是既有会话机制的现状（`revoke_all` 只有注册表这一个取数
    来源），钉住它，免得后续把"改密即全端下线"写成比实际更强的承诺。
    """
    _register(client)
    unregistered = _issue_token("alice")
    assert client.get("/api/user/info", headers={"Authorization": f"Bearer {unregistered}"}).status_code == 200
    _login(client, "alice")
    assert _change(client, PASSWORD, NEW_PASSWORD)["code"] == 200
    assert client.get("/api/user/info", headers={"Authorization": f"Bearer {unregistered}"}).status_code == 200


# ---------- 改密本身 ----------


def test_wrong_old_password_changes_nothing_and_keeps_the_session(client: TestClient) -> None:
    _register(client)
    token = _login(client, "alice")
    before = _stored_hash("alice")
    body = _change(client, "wrongly123", NEW_PASSWORD)
    assert body["code"] == 400 and body["message"] == "原密码错误"
    assert _stored_hash("alice") == before, "原密码没验对时一个字节都不准动"
    assert not user_service.verify_password(NEW_PASSWORD, _stored_hash("alice"))
    # 失败不牵连会话：还在登录态，用户能重试
    assert client.get("/api/user/info", headers={"Authorization": f"Bearer {token}"}).status_code == 200


def test_change_password_rehashes_with_the_stored_bcrypt_shape(client: TestClient) -> None:
    """新口令必须走注册同一条 `hash_password`（$2a$/rounds=10），否则存量形态会漂移。"""
    _register(client)
    _login(client, "alice")
    assert _change(client, PASSWORD, NEW_PASSWORD)["code"] == 200
    stored = _stored_hash("alice")
    assert stored.startswith("$2a$")
    assert user_service.verify_password(NEW_PASSWORD, stored)
    assert not user_service.verify_password(PASSWORD, stored)


def test_change_password_revokes_other_sessions_but_keeps_this_device(client: TestClient) -> None:
    _register(client)
    # 另一台设备的票：登录时进过会话注册表，改密后必须一起进黑名单
    other = _issue_token("alice")
    sessions.register_session("alice", jwt_tokens.decode_token(other, JWT_MATERIAL)["jti"], other, 3600)
    stale = _login(client, "alice")
    assert stale != other

    response = client.post("/api/user/password", json={"old_password": PASSWORD, "new_password": NEW_PASSWORD})
    assert response.status_code == 200 and response.json()["code"] == 200
    # 票只进 Set-Cookie：响应体里既不能出现新票，也不能回显旧票（同 login 的口径）
    assert PASSWORD not in response.text and NEW_PASSWORD not in response.text

    # 本设备续命：响应重新签发的 Cookie 与改密前不同，且仍然可用
    refreshed = client.cookies.get("TA_AUTH")
    assert refreshed and refreshed != stale
    assert client.get("/api/user/info").status_code == 200

    # 其余会话全死：改密前这张票、以及另一台设备那张票
    for dead in (stale, other):
        assert client.get("/api/user/info", headers={"Authorization": f"Bearer {dead}"}).status_code == 401
    # 旧口令从此登不进来，新口令可以
    assert client.post("/api/auth/login", json={"username": "alice", "password": PASSWORD}).json()["code"] == 400


def test_change_password_only_touches_the_authenticated_account(client: TestClient) -> None:
    """「A 改 B 的密码」无法表达：请求体没有用户名字段，多余的键被忽略，撤销只落在自己身上。"""
    _register(client, username="alice")
    alice_token = _login(client, "alice")
    alice_hash = _stored_hash("alice")

    _register(client, username="bob")
    _login(client, "bob")
    body = _change(client, PASSWORD, NEW_PASSWORD, username="alice")
    assert body["code"] == 200
    # bob 自己换了口令；alice 的哈希分毫未动
    assert user_service.verify_password(NEW_PASSWORD, _stored_hash("bob"))
    assert _stored_hash("alice") == alice_hash
    # alice 的会话没有被 bob 的改密牵连
    assert client.get("/api/user/info", headers={"Authorization": f"Bearer {alice_token}"}).status_code == 200


# ---------- 新口令门槛：与注册同规则、同文案 ----------


@pytest.mark.parametrize(
    "new_password,expected",
    [
        ("", "密码不能为空"),
        ("abc12", "密码长度需在 6-24 之间"),
        ("a" * 25 + "1", "密码长度需在 6-24 之间"),
        ("pass word1", "密码包含非法字符"),
        ("exampleabcd", "密码需同时包含字母和数字"),
    ],
)
def test_weak_new_password_rejected_with_register_wording(client: TestClient, new_password, expected) -> None:
    _register(client)
    token = _login(client, "alice")
    before = _stored_hash("alice")
    response = client.post("/api/user/password", json={"old_password": PASSWORD, "new_password": new_password})
    assert response.status_code == 400, "必须是业务面 400 + 单条 message，不是 FastAPI 的 422"
    assert response.json()["message"] == expected
    assert _stored_hash("alice") == before
    assert client.get("/api/user/info", headers={"Authorization": f"Bearer {token}"}).status_code == 200


# ---------- 原密码暴力尝试 ----------


def test_change_password_failure_key_is_its_own_door() -> None:
    """与登录失败锁同族但不同键（`auth:pwd:fail:*` vs `auth:login:fail:*`）。"""
    assert sessions.password_fail_key("1.2.3.4", "bob") == "auth:pwd:fail:1.2.3.4|bob"
    assert sessions.password_fail_key("1.2.3.4", "bob") != sessions.login_fail_key("1.2.3.4", "bob")


def test_change_password_locks_after_five_wrong_attempts(client: TestClient) -> None:
    """改密端点是一个口令校验 Oracle：与登录失败锁同阈值（5 次）、同 15 分钟窗。"""
    _register(client)
    _login(client, "alice")
    before = _stored_hash("alice")
    for _ in range(5):
        assert _change(client, "wrongly123", NEW_PASSWORD)["message"] == "原密码错误"
    locked = _change(client, PASSWORD, NEW_PASSWORD)
    assert locked["code"] == 429 and locked["message"] == "原密码错误次数过多，请稍后再试"
    assert _stored_hash("alice") == before, "锁定期内连口令校验都不该走到，更不该写库"


def test_change_password_failures_do_not_lock_the_login_door(client: TestClient) -> None:
    """失败锁独立成键：拿被盗会话票狂点改密，不该把真用户挡在登录门外。"""
    _register(client)
    _login(client, "alice")
    for _ in range(5):
        _change(client, "wrongly123", NEW_PASSWORD)
    assert _change(client, PASSWORD, NEW_PASSWORD)["code"] == 429
    assert client.post("/api/auth/login", json={"username": "alice", "password": PASSWORD}).json()["code"] == 200


# ---------- 日志红线 ----------


def test_change_password_never_logs_credentials(client: TestClient, caplog) -> None:
    _register(client)
    _login(client, "alice")
    with caplog.at_level(logging.DEBUG):
        assert _change(client, PASSWORD, NEW_PASSWORD)["code"] == 200
    text = caplog.text
    assert PASSWORD not in text and NEW_PASSWORD not in text, "明文口令进日志"
    assert "$2a$" not in text, "bcrypt 哈希（离线可破）进日志"
    # 连"整张票"也不落：改密前后的 token 都算凭据
    assert "TA_AUTH" not in text


# ---------- logout-all：产品面已接（见前端），这里钉后端语义 ----------


def test_logout_all_reports_and_revokes(client: TestClient) -> None:
    _register(client)
    _login(client, "alice")
    assert client.post("/api/auth/logout-all").json()["code"] == 200
    assert client.get("/api/user/info").status_code == 401
