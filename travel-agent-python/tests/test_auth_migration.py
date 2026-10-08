"""M2 迁移验证：注册/登录/登出/多端登出/用户信息。

这里刻意不打鉴权桩——`/api/user/info` 走真实的 Cookie→JWT→会话→sys_user 全链路
（SQLite 真库），因为迁移最容易出的错正是"链路上某一环的口径没对齐"，
打桩会把这类错误藏起来。
"""

from __future__ import annotations

import logging
from typing import Any, cast

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Table, create_engine, select
from sqlalchemy.orm import sessionmaker

from app.api.business.auth import auth_router, user_router
from app.common import jwt_compat
from app.common.client_ip import client_ip
from app.common.config import settings
from app.common.envelope import install_exception_handlers
from app.db import session as db_session
from app.db.models import Base, SysUser
from app.services import state_and_sessions as sessions
from app.services import user_service

JWT_MATERIAL = "example-only-hs256-test-signing-material"
# 带 example 标记：口令形状的测试字面量会被 scripts/check-secrets.ps1 按占位符放行
PASSWORD = "example123"


@pytest.fixture(autouse=True)
def isolated_state(monkeypatch, tmp_path):
    """Redis 指向不可达端口 → 限速与会话一律走进程内兜底，测试可复现。"""
    engine = create_engine(f"sqlite:///{tmp_path / 'auth.db'}")
    Base.metadata.create_all(engine)
    db_session.init_engine(engine, sessionmaker(bind=engine, expire_on_commit=False))
    monkeypatch.setattr(settings, "jwt_secret", JWT_MATERIAL)
    monkeypatch.setattr(settings, "redis_url", "redis://127.0.0.1:1/0")
    monkeypatch.setattr(settings, "trusted_proxies", "")
    sessions.reset_for_tests()
    yield
    db_session.init_engine(None, None)
    sessions.reset_for_tests()


@pytest.fixture
def client() -> TestClient:
    app = FastAPI()
    install_exception_handlers(app)
    app.include_router(auth_router)
    app.include_router(user_router)
    return TestClient(app, base_url="http://testserver")


def _register(client: TestClient, username: str = "alice", password: str = PASSWORD, **kw) -> dict:
    return client.post("/api/auth/register", json={"username": username, "password": password, **kw}).json()


# ---------- 注册 ----------


def test_register_persists_bcrypt_2a_hash(client: TestClient) -> None:
    body = _register(client)
    assert body["code"] == 200 and body["data"] is None
    with db_session.session_scope() as session:
        stored = session.execute(select(SysUser).where(SysUser.username == "alice")).scalar_one()
    # $2a$ 前缀是 Spring BCryptPasswordEncoder 的默认输出；不匹配则存量账号登录不上
    assert stored.password.startswith("$2a$")
    assert user_service.verify_password(PASSWORD, stored.password)


def test_register_rejects_duplicate_with_vague_message(client: TestClient) -> None:
    _register(client)
    body = _register(client)
    assert body["code"] == 400
    assert body["message"] == "注册失败，请检查用户名或稍后重试"


def test_register_requires_letter_and_digit(client: TestClient) -> None:
    assert _register(client, password="exampleabcd")["message"] == "密码需同时包含字母和数字"


@pytest.mark.parametrize(
    "payload,expected",
    [
        ({"username": "ab", "password": PASSWORD}, "用户名长度需在 3-32 之间"),
        ({"username": "al!ce", "password": PASSWORD}, "用户名仅支持中英文、数字与下划线"),
        ({"username": "alice", "password": "12345"}, "密码长度需在 6-24 之间"),
        ({"username": "alice", "password": "abc 123"}, "密码包含非法字符"),
        ({"username": "   ", "password": PASSWORD}, "用户名不能为空"),
    ],
)
def test_register_validation_messages_match_java_beans_validation(client, payload, expected):
    response = client.post("/api/auth/register", json=payload)
    assert response.status_code == 400, "FastAPI 默认 422，必须映射成 Java 同款 400"
    assert response.json()["message"] == expected


def test_login_throttle_after_five_attempts_per_ip(client: TestClient) -> None:
    for _ in range(5):
        _register(client, username="bob1", password=PASSWORD)
    body = _register(client, username="bob2", password=PASSWORD)
    assert body["code"] == 429 and body["message"] == "注册过于频繁，请稍后再试"


# ---------- 登录按 IP 总量窗（审计 §3.3.3） ----------


def test_login_ip_total_window_blocks_password_spray(client: TestClient, monkeypatch) -> None:
    """失败锁的粒度是 (IP, 用户名)：换用户名即重新计数，单 IP 可全速喷密码兼烧 bcrypt。
    这里钉住按 IP 的总量闸：窗口内第 N+1 次登录尝试（无论用户名成败）429。
    本文件 isolated_state 把 Redis 钉到不可达端口——本用例同时在验证进程内兜底路径。
    conftest 已把阈值放开到极大值，这里改回真实默认 30 再验。
    """
    monkeypatch.setattr(settings, "login_ip_rate_per_minute", 30)
    assert sessions.login_ip_key("1.2.3.4") == "auth:login:ip:1.2.3.4"
    _register(client)
    # 对 30 个不同用户名各试一次（每个 (IP, 用户名) 失败锁只计 1/5，全都不锁——
    # 正是 §3.3.3 描述的"换用户名即重置"喷洒形态；用户不存在 → 不烧 bcrypt，快）
    for i in range(30):
        assert (
            client.post("/api/auth/login", json={"username": f"victim{i}", "password": "bad1234"}).json()["code"] == 400
        )
    sprayed = client.post("/api/auth/login", json={"username": "alice", "password": PASSWORD})
    body = sprayed.json()
    assert body["code"] == 429 and body["message"] == "登录过于频繁，请稍后再试"


def test_login_ip_total_window_fires_before_user_lookup(client: TestClient, monkeypatch) -> None:
    """总量闸在 _find_user/bcrypt 之前：超限 IP 的第 N+1 次连用户查询都不进（CPU 止损）。"""
    monkeypatch.setattr(settings, "login_ip_rate_per_minute", 5)
    looked_up: list[str] = []
    monkeypatch.setattr(user_service, "_find_user", lambda username: looked_up.append(username))
    for _ in range(5):
        client.post("/api/auth/login", json={"username": "whoever", "password": "bad1234"})
    looked_up.clear()
    blocked = client.post("/api/auth/login", json={"username": "alice", "password": PASSWORD}).json()
    assert blocked["code"] == 429
    assert not looked_up, "超限请求不应触发用户查询，更不应烧 bcrypt"


def test_login_ip_total_window_counts_success_too(client: TestClient, monkeypatch) -> None:
    """总窗不看成败：同一 IP 的高速成功登录（脚本化会话刷新）同样被闸。"""
    monkeypatch.setattr(settings, "login_ip_rate_per_minute", 5)
    _register(client)
    for _ in range(5):
        assert client.post("/api/auth/login", json={"username": "alice", "password": PASSWORD}).json()["code"] == 200
    body = client.post("/api/auth/login", json={"username": "alice", "password": PASSWORD}).json()
    assert body["code"] == 429 and body["message"] == "登录过于频繁，请稍后再试"


# ---------- 登录 ----------


def test_login_sets_httponly_cookie_and_never_returns_token(client: TestClient) -> None:
    _register(client)
    response = client.post("/api/auth/login", json={"username": "alice", "password": PASSWORD})
    body = response.json()
    assert body["code"] == 200
    assert body["data"]["user"]["username"] == "alice"
    assert "token" not in body["data"], "凭据只进 HttpOnly Cookie，body 回传会扩大 XSS 面"
    cookie = response.headers["set-cookie"].lower()
    assert "ta_auth=" in cookie and "httponly" in cookie and "samesite=lax" in cookie
    assert "max-age=86400" in cookie  # 24h，与 Java AuthCookieSupport 的 maxAge 口径一致


def test_login_wrong_password_and_failure_counter(client: TestClient) -> None:
    _register(client)
    for _ in range(5):  # 前 5 次失败各计一次（Java: getCount >= 5 才拒绝）
        assert (
            client.post("/api/auth/login", json={"username": "alice", "password": "bad1234"}).json()["message"]
            == "用户名或密码错误"
        )
    locked = client.post("/api/auth/login", json={"username": "alice", "password": "bad1234"}).json()
    assert locked["code"] == 429 and locked["message"] == "登录失败次数过多，请稍后再试"


def test_successful_login_resets_failure_counter(client: TestClient) -> None:
    _register(client)
    for _ in range(3):
        client.post("/api/auth/login", json={"username": "alice", "password": "bad1234"})
    assert client.post("/api/auth/login", json={"username": "alice", "password": PASSWORD}).json()["code"] == 200
    for _ in range(5):  # 重置后再失败 5 次仍只到阈值、不锁
        assert client.post("/api/auth/login", json={"username": "alice", "password": "bad1234"}).json()["code"] == 400
    assert client.post("/api/auth/login", json={"username": "alice", "password": "bad1234"}).json()["code"] == 429


def test_login_disabled_account_is_403(client: TestClient) -> None:
    _register(client)
    with db_session.session_scope() as session:
        session.execute(cast(Table, SysUser.__table__).update().where(SysUser.username == "alice").values(status=0))
    body = client.post("/api/auth/login", json={"username": "alice", "password": PASSWORD}).json()
    assert body["code"] == 403 and body["message"] == "账号已禁用"


# ---------- 会话链路 / 用户信息 ----------


def test_user_info_requires_session(client: TestClient) -> None:
    assert client.get("/api/user/info").status_code == 401
    _register(client)
    client.post("/api/auth/login", json={"username": "alice", "password": PASSWORD})
    body = client.get("/api/user/info").json()
    assert set(body["data"]) == {"id", "username", "nickname", "phone", "role"}
    assert body["data"]["nickname"] == "alice"  # 昵称为空时回落用户名（同 Java）


def test_logout_revokes_the_issued_token_for_both_channels(client: TestClient) -> None:
    _register(client)
    client.post("/api/auth/login", json={"username": "alice", "password": PASSWORD})
    assert client.get("/api/user/info").status_code == 200
    issued = client.cookies.get("TA_AUTH")
    assert issued, "登录应下发 TA_AUTH Cookie"
    assert client.post("/api/auth/logout").json()["code"] == 200
    # 同一张票改走 Bearer 也必须失效：证明吊销落在 token 本身而非仅清 Cookie
    assert client.get("/api/user/info", headers={"Authorization": f"Bearer {issued}"}).status_code == 401


def test_logout_all_revokes_every_registered_session(client: TestClient) -> None:
    _register(client)
    first = _issue_token("alice")
    second = _issue_token("alice")
    sessions.register_session("alice", jwt_compat.decode_token(first, JWT_MATERIAL)["jti"], first, 3600)
    sessions.register_session("alice", jwt_compat.decode_token(second, JWT_MATERIAL)["jti"], second, 3600)
    client.cookies.set("TA_AUTH", first)
    body = client.post("/api/auth/logout-all").json()
    assert body["data"]["revoked"] >= 2
    assert client.get("/api/user/info", headers={"Authorization": f"Bearer {second}"}).status_code == 401


def _issue_token(username: str) -> str:
    return jwt_compat.encode_token(username, JWT_MATERIAL, 3600)


# ---------- IP 归属与共享键 ----------


class _Addr:
    def __init__(self, host):
        self.host = host


class _Req:
    def __init__(self, host, headers):
        self.client = _Addr(host)
        self.headers = headers


def test_forwarded_for_ignored_unless_peer_is_trusted(monkeypatch) -> None:
    spoofable = _Req("203.0.113.9", {"x-forwarded-for": "1.2.3.4"})
    monkeypatch.setattr(settings, "trusted_proxies", "")
    assert client_ip(cast(Any, spoofable)) == "203.0.113.9", "不可信对端的 XFF 必须忽略，否则限速可被逐次换 IP 绕过"
    monkeypatch.setattr(settings, "trusted_proxies", "203.0.113.9")
    assert client_ip(cast(Any, spoofable)) == "1.2.3.4"
    # R1-5：多跳时取的是**最右**的不可信跳。链首（9.9.9.9）永远是客户端自己写的，
    # 取它等于让攻击者每次换个限速桶；口径细节见 tests/test_client_ip.py。
    assert client_ip(cast(Any, _Req("203.0.113.9", {"x-forwarded-for": "9.9.9.9, 8.8.8.8"}))) == "8.8.8.8"


def test_rate_limit_and_session_keys_match_java() -> None:
    """键名必须与 Java 一致，否则双跑期各算各的额度/各管各的会话。"""
    assert sessions.login_fail_key("1.2.3.4", "bob") == "auth:login:fail:1.2.3.4|bob"
    assert sessions.register_key("1.2.3.4") == "auth:register:1.2.3.4"
    assert sessions.SESSION_KEY_PREFIX == "auth:sess:"


def test_malformed_stored_hash_is_auth_failure_not_500() -> None:
    assert user_service.verify_password(PASSWORD, "not-a-bcrypt-hash") is False
    assert user_service.verify_password(PASSWORD, "") is False


def test_every_business_route_is_guarded_or_publicly_listed() -> None:
    """装配后的全站自检：`/api/**` 只能被"挂守卫"或"进 permitAll 白名单"覆盖。

    Spring 侧是 `anyRequest().authenticated()` 的默认拒绝；这里靠逐条 router/路由声明
    `enforce_business_auth`，所以漏挂一条就变成一个静默匿名端点（不会报错、测试也可能照样绿）。
    白名单是**显式**列表（`app/api/security.py:PUBLIC_PATHS`），新增一条就得在评审里露一次脸。
    公开路径同样允许带守卫：`permitAll` 不等于"不看票"，`/api/auth/logout-all` 这类
    "公开但要认人"的端点正是靠守卫拿到当前用户。
    """
    from fastapi.routing import APIRoute

    import main
    from app.api.security import enforce_business_auth, is_public_path

    unguarded: list[str] = []
    for route in main.app.routes:
        if not isinstance(route, APIRoute) or not route.path.startswith("/api/"):
            continue
        if route.path.startswith("/api/agent"):  # 内部面走 X-Agent-Token，不是用户会话
            continue
        guarded = any(dep.dependency is enforce_business_auth for dep in route.dependencies)
        if not guarded and not is_public_path(route.path):
            unguarded.append(f"{sorted(route.methods)} {route.path}")
    assert not unguarded, f"既无守卫也不在白名单的路由: {unguarded}"


def test_register_conflict_does_not_log_credentials(client: TestClient, monkeypatch, caplog) -> None:
    """落库真撞唯一索引时，日志只准有异常类型。

    `register()` 先做 `_find_user` 预检，重复用户名通常在那儿就返回模糊文案；
    走到 `except` 的是"预检与落库之间被并发插入抢位"这条竞态。SQLAlchemy 的
    StatementError 会把 SQL **与绑定参数**一起打进 str(exc)，而注册路径的参数里
    有 bcrypt 口令哈希（离线可破）——所以异常全文不得进日志。
    """
    _register(client)
    monkeypatch.setattr(user_service, "_find_user", lambda username: None)  # 造竞态：预检说"没有"
    with caplog.at_level(logging.INFO, logger="app.services.user_service"):
        body = _register(client, password="example456")
    assert body["message"] == "注册失败，请检查用户名或稍后重试"
    # 盯的是"异常正文不进日志"这件事本身：SQLite 下正文只有约束名，MySQL 下
    # SQLAlchemy 会把它扩成 `[SQL: INSERT ...] [parameters: {...bcrypt 哈希...}]`。
    # 断言不含正文片段，两驱动下都是真实的收窄。
    assert "register failed: IntegrityError" in caplog.text
    assert "constraint" not in caplog.text.lower(), "异常正文（驱动相关，MySQL 下含 SQL 与绑定参数）进了日志"
    assert "$2a$" not in caplog.text and "example456" not in caplog.text
