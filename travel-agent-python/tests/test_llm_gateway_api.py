"""BYOK 网关管理端点（/api/llm-gateway）单测：CRUD 全链 / 脱敏 / 互斥启用 / 归属 404 /
SSRF 防线 / 探测紧约束与限频 / 密文解不开的响亮语义。

夹具抄 tests/test_cover_api.py（TestClient + sqlite + Bearer 头）；上游连通性测试
mock 掉 llm_gateway_service.LLMClient（探测 client 在 service 内构造）、SSRF 的
DNS 解析打桩 _resolve_host_ips——全部用例零外网。假密钥保持 <32 字符
（secret scan 熵模式口径）。
"""

from __future__ import annotations

import socket
from collections.abc import Iterator

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, update
from sqlalchemy.orm import sessionmaker

from app.api import deps
from app.api.business.llm_gateway import router as gateway_router
from app.common.envelope import ApiError, install_exception_handlers
from app.common.jwt_compat import encode_token
from app.db import session as db_session
from app.db.models import Base, UserLlmGateway
from app.services import llm_gateway_service, state_and_sessions

SIGNING_MATERIAL = "example-only-hs256-signing-material-32b"

# 短占位密钥（<32 字符，不触发 secret scan 熵模式）
KEY_A = "sk-test-aaaa1111"
KEY_B = "sk-test-bbbb2222"

CREATE_BODY = {"name": "我的百炼", "baseUrl": "https://dashscope.example/v1", "apiKey": KEY_A, "model": "qwen-max"}


@pytest.fixture
def client(tmp_path, monkeypatch) -> Iterator[TestClient]:
    engine = create_engine(f"sqlite:///{tmp_path / 'gateway.db'}")
    Base.metadata.create_all(engine)
    db_session.init_engine(engine, sessionmaker(bind=engine, expire_on_commit=False))

    monkeypatch.setattr(deps.settings, "jwt_secret", SIGNING_MATERIAL)
    monkeypatch.setattr(deps.token_revocation, "is_revoked", lambda _t: False)
    monkeypatch.setattr(
        deps.user_repository,
        "find_by_username",
        lambda _u: {"id": 42, "username": "alice", "role": "user", "status": 1},
    )
    # SSRF 校验的 DNS 解析一律打桩为公网地址（专项用例自行覆盖成私网/解析失败）
    monkeypatch.setattr(llm_gateway_service, "_resolve_host_ips", lambda _host: ["93.184.216.34"])
    # /test 分钟窗是进程内滑动窗：逐用例清零，防止 wall-clock 60s 窗跨用例串味出 429
    state_and_sessions.reset_for_tests()

    app = FastAPI()
    install_exception_handlers(app)
    app.include_router(gateway_router)
    yield TestClient(app)
    db_session.init_engine(None, None)
    state_and_sessions.reset_for_tests()


def _headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {encode_token('alice', SIGNING_MATERIAL, 3600)}"}


def _create(client: TestClient, **overrides) -> dict:
    body = {**CREATE_BODY, **overrides}
    response = client.post("/api/llm-gateway", json=body, headers=_headers())
    assert response.status_code == 200, response.text
    return response.json()["data"]


def _cipher_of(gateway_id: int) -> str:
    with db_session.session_scope() as session:
        row = session.get(UserLlmGateway, gateway_id)
        assert row is not None
        return row.api_key_cipher


# ---------- CRUD 与脱敏 ----------


def test_create_and_list_without_plaintext_key(client: TestClient) -> None:
    created = _create(client)
    assert created["apiKeyHint"] == KEY_A[-4:]
    assert created["enabled"] is False
    assert "apiKey" not in created and "api_key" not in created

    listed = client.get("/api/llm-gateway", headers=_headers()).json()["data"]
    assert len(listed) == 1
    assert listed[0]["id"] == created["id"]
    # 全链任何响应都不含明文密钥（只有尾 4 位）
    assert KEY_A not in str(listed)

    # 落库是密文：与明文不同且可逆
    cipher = _cipher_of(created["id"])
    assert KEY_A not in cipher


def test_requires_auth(client: TestClient) -> None:
    assert client.get("/api/llm-gateway").status_code == 401
    assert client.post("/api/llm-gateway", json=CREATE_BODY).status_code == 401


def test_create_missing_required_field_is_400(client: TestClient) -> None:
    for drop in ("name", "baseUrl", "apiKey", "model"):
        body = {k: v for k, v in CREATE_BODY.items() if k != drop}
        response = client.post("/api/llm-gateway", json=body, headers=_headers())
        assert response.status_code == 400, (drop, response.text)


def test_create_rejects_bad_base_url(client: TestClient) -> None:
    response = client.post("/api/llm-gateway", json={**CREATE_BODY, "baseUrl": "ftp://x"}, headers=_headers())
    assert response.status_code == 400


def test_duplicate_name_is_409(client: TestClient) -> None:
    _create(client)
    response = client.post("/api/llm-gateway", json=CREATE_BODY, headers=_headers())
    assert response.status_code == 409


def test_update_blank_api_key_keeps_cipher_and_hint(client: TestClient) -> None:
    created = _create(client)
    before = _cipher_of(created["id"])

    response = client.put(
        f"/api/llm-gateway/{created['id']}",
        json={"model": "qwen-max-latest", "apiKey": ""},
        headers=_headers(),
    )
    assert response.status_code == 200, response.text
    assert response.json()["data"]["model"] == "qwen-max-latest"
    assert _cipher_of(created["id"]) == before, "apiKey 留空必须不动密文"


def test_update_new_api_key_reencrypts(client: TestClient) -> None:
    created = _create(client)
    before = _cipher_of(created["id"])

    response = client.put(f"/api/llm-gateway/{created['id']}", json={"apiKey": KEY_B}, headers=_headers())
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["apiKeyHint"] == KEY_B[-4:], "换 key 必须重算尾 4 位提示"
    after = _cipher_of(created["id"])
    assert after != before and KEY_B not in after


def test_foreign_gateway_is_404(client: TestClient) -> None:
    with db_session.session_scope() as session:
        from app.common import gateway_crypto

        foreign = UserLlmGateway(
            user_id=999,
            name="别人的",
            base_url="https://elsewhere.example/v1",
            api_key_cipher=gateway_crypto.encrypt_secret("sk-foreign-99"),
            api_key_hint="c-99",
            model="m",
            enabled=True,
        )
        session.add(foreign)
        session.flush()
        foreign_id = foreign.id

    for method, path, kwargs in (
        ("put", f"/api/llm-gateway/{foreign_id}", {"json": {}}),
        ("delete", f"/api/llm-gateway/{foreign_id}", {}),
        ("post", f"/api/llm-gateway/{foreign_id}/enable", {}),
        ("post", f"/api/llm-gateway/{foreign_id}/disable", {}),
        ("post", f"/api/llm-gateway/{foreign_id}/test", {}),
    ):
        response = getattr(client, method)(path, headers=_headers(), **kwargs)
        assert response.status_code == 404, (method, response.text)


def test_delete_removes_row(client: TestClient) -> None:
    created = _create(client)
    assert client.delete(f"/api/llm-gateway/{created['id']}", headers=_headers()).status_code == 200
    assert client.get("/api/llm-gateway", headers=_headers()).json()["data"] == []
    assert client.delete(f"/api/llm-gateway/{created['id']}", headers=_headers()).status_code == 404


# ---------- 互斥启用 ----------


def test_enable_is_mutually_exclusive(client: TestClient) -> None:
    first = _create(client)
    second = _create(client, name="备选网关", apiKey=KEY_B)

    assert client.post(f"/api/llm-gateway/{first['id']}/enable", headers=_headers()).status_code == 200
    assert client.post(f"/api/llm-gateway/{second['id']}/enable", headers=_headers()).status_code == 200

    rows = {row["id"]: row for row in client.get("/api/llm-gateway", headers=_headers()).json()["data"]}
    assert rows[second["id"]]["enabled"] is True, "后启用的持有唯一启用位"
    assert rows[first["id"]]["enabled"] is False, "启用互斥：先启用的必须被顶下"

    # 停用后无任何启用项
    assert client.post(f"/api/llm-gateway/{second['id']}/disable", headers=_headers()).status_code == 200
    assert all(not row["enabled"] for row in client.get("/api/llm-gateway", headers=_headers()).json()["data"])


def test_resolve_route_reads_enabled_row(client: TestClient, monkeypatch) -> None:
    """端点链路落库后，resolve_route 能解出该用户的生效路由（解密在 service 内闭环）。"""
    from app.common.llm_route import current_route

    created = _create(client)
    assert llm_gateway_service.resolve_route(42) is None, "未启用时无路由"
    client.post(f"/api/llm-gateway/{created['id']}/enable", headers=_headers())

    with llm_gateway_service.route_scope(42):
        route = current_route()
    assert route is not None
    assert route.base_url == CREATE_BODY["baseUrl"] and route.model == CREATE_BODY["model"]
    assert route.api_key == KEY_A


# ---------- 连通性测试端点（全 mock） ----------


class _OkClient:
    def chat(self, *_args, **_kwargs) -> str:
        return "pong"


class _BoomClient:
    def chat(self, *_args, **_kwargs) -> str:
        raise RuntimeError(f"upstream 500 with key {KEY_A} embedded")


def test_test_connection_ok(client: TestClient, monkeypatch) -> None:
    created = _create(client)
    monkeypatch.setattr(llm_gateway_service, "LLMClient", lambda **_kw: _OkClient())
    response = client.post(f"/api/llm-gateway/{created['id']}/test", headers=_headers())
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["ok"] is True and data["message"] == "连接成功"
    assert isinstance(data["latencyMs"], int)


def test_test_connection_builds_tight_probe(client: TestClient, monkeypatch) -> None:
    """探测 client 必须带紧约束：短超时 + 不重试 + 不跟随重定向（线程池占用收口）。"""
    seen: dict = {}

    class _Recording:
        def __init__(self, **kwargs) -> None:
            seen.update(kwargs)

        def chat(self, *_args, **_kwargs) -> str:
            return "pong"

    monkeypatch.setattr(llm_gateway_service, "LLMClient", _Recording)
    created = _create(client)
    assert client.post(f"/api/llm-gateway/{created['id']}/test", headers=_headers()).status_code == 200
    assert seen["timeout"] == llm_gateway_service._TEST_TIMEOUT_SECONDS
    assert seen["max_attempts"] == 1
    assert seen["follow_redirects"] is False
    assert seen["base_url"] == CREATE_BODY["baseUrl"]


def test_test_connection_failure_sanitizes_key(client: TestClient, monkeypatch) -> None:
    created = _create(client)
    monkeypatch.setattr(llm_gateway_service, "LLMClient", lambda **_kw: _BoomClient())
    response = client.post(f"/api/llm-gateway/{created['id']}/test", headers=_headers())
    assert response.status_code == 200, "连通性失败是结果字段，不是 HTTP 错"
    data = response.json()["data"]
    assert data["ok"] is False
    assert KEY_A not in data["message"], "错误文案不得含密钥（异常里带了也要抹掉）"


def test_test_connection_does_not_echo_internal_url(client: TestClient, monkeypatch) -> None:
    """httpx 异常文案内嵌完整内部 URL：只准回显类型化结论，不得当内网探测结果。"""
    created = _create(client)
    request = httpx.Request("POST", "http://10.0.0.5:8080/chat/completions")
    status_error = httpx.HTTPStatusError(
        "Client error '404 Not Found' for url 'http://10.0.0.5:8080/chat/completions'",
        request=request,
        response=httpx.Response(404, request=request),
    )

    class _NotFound:
        def chat(self, *_args, **_kwargs) -> str:
            raise status_error

    monkeypatch.setattr(llm_gateway_service, "LLMClient", lambda **_kw: _NotFound())
    response = client.post(f"/api/llm-gateway/{created['id']}/test", headers=_headers())
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["ok"] is False and "404" in data["message"]
    assert "10.0.0.5" not in data["message"] and "http://" not in data["message"]


def test_test_connection_rate_limited(client: TestClient, monkeypatch) -> None:
    """分钟窗限频：小窗下第二次 /test 429（防黑洞地址占满业务线程池）。"""
    from app.services import quota_service

    monkeypatch.setattr(quota_service, "_GATEWAY_TESTS_PER_MINUTE", 1)
    monkeypatch.setattr(llm_gateway_service, "LLMClient", lambda **_kw: _OkClient())
    created = _create(client)
    assert client.post(f"/api/llm-gateway/{created['id']}/test", headers=_headers()).status_code == 200
    assert client.post(f"/api/llm-gateway/{created['id']}/test", headers=_headers()).status_code == 429


# ---------- 密文解不开：响亮报错，不静默回退（2026-10-02 终审） ----------


def _seed_broken_cipher_row(user_id: int = 42) -> int:
    """密文损坏/加密 key 轮换后的存量行：decrypt_secret 必抛 ValueError。"""
    with db_session.session_scope() as session:
        row = UserLlmGateway(
            user_id=user_id,
            name="坏密文",
            base_url="https://dashscope.example/v1",
            api_key_cipher="not-a-fernet-token",
            api_key_hint="????",
            model="m",
            enabled=False,
        )
        session.add(row)
        session.flush()
        return row.id


def test_enable_rejects_undecryptable_cipher(client: TestClient) -> None:
    gateway_id = _seed_broken_cipher_row()
    response = client.post(f"/api/llm-gateway/{gateway_id}/enable", headers=_headers())
    assert response.status_code == 409, "解不开的配置不允许持有启用位"
    listed = {row["id"]: row for row in client.get("/api/llm-gateway", headers=_headers()).json()["data"]}
    assert listed[gateway_id]["enabled"] is False


def test_test_connection_undecryptable_cipher_is_409(client: TestClient) -> None:
    gateway_id = _seed_broken_cipher_row()
    response = client.post(f"/api/llm-gateway/{gateway_id}/test", headers=_headers())
    assert response.status_code == 409
    assert "重新填写" in response.json()["message"]


def test_resolve_route_raises_loudly_on_undecryptable_cipher(client: TestClient) -> None:
    """拍板口径：fail-open 只限网关表不可读；密文解不开必须上抛，不许静默烧运营方 key。"""
    gateway_id = _seed_broken_cipher_row()
    with db_session.session_scope() as session:
        session.execute(update(UserLlmGateway).where(UserLlmGateway.id == gateway_id).values(enabled=True))
    with pytest.raises(ApiError) as excinfo:
        llm_gateway_service.resolve_route(42)
    assert excinfo.value.status == 409


# ---------- SSRF 防线：base_url 只准公网（2026-10-02 终审） ----------


def test_create_rejects_private_base_urls(client: TestClient) -> None:
    for index, bad in enumerate(
        (
            "http://127.0.0.1:8080/v1",
            "http://10.0.0.5:8080/v1",
            "http://172.16.1.9/v1",
            "http://192.168.1.4/v1",
            "http://169.254.169.254/v1",  # 云 metadata 段（link-local）
            "http://[fd00::1]/v1",  # IPv6 ULA
            "http://[::ffff:127.0.0.1]:8080/v1",  # IPv4-mapped IPv6
            "http://localhost:11434/v1",  # 回环域名
        )
    ):
        response = client.post(
            "/api/llm-gateway",
            json={**CREATE_BODY, "baseUrl": bad, "name": f"探测{index}"},
            headers=_headers(),
        )
        assert response.status_code == 400, (bad, response.text)


def test_create_rejects_private_dns_resolution(client: TestClient, monkeypatch) -> None:
    monkeypatch.setattr(llm_gateway_service, "_resolve_host_ips", lambda _host: ["203.0.113.7", "10.1.2.3"])
    response = client.post("/api/llm-gateway", json=CREATE_BODY, headers=_headers())
    assert response.status_code == 400, "解析出任一私网地址即拒绝（多 A 记录混排也不能漏）"


def test_create_rejects_unresolvable_host(client: TestClient, monkeypatch) -> None:
    def _gaierror(_host: str) -> list[str]:
        raise socket.gaierror(8, "nodename nor servname provided")

    monkeypatch.setattr(llm_gateway_service, "_resolve_host_ips", _gaierror)
    response = client.post("/api/llm-gateway", json=CREATE_BODY, headers=_headers())
    assert response.status_code == 400


def test_update_rejects_private_base_url(client: TestClient) -> None:
    created = _create(client)
    response = client.put(
        f"/api/llm-gateway/{created['id']}",
        json={"baseUrl": "http://172.16.0.9/v1"},
        headers=_headers(),
    )
    assert response.status_code == 400


def test_test_connection_rejects_private_base_url_of_stale_row(client: TestClient) -> None:
    """绕过保存口的存量脏行（直接改库）：/test 侧也校验，且不发探测请求。"""
    created = _create(client)
    with db_session.session_scope() as session:
        row = session.get(UserLlmGateway, created["id"])
        assert row is not None
        row.base_url = "http://10.9.9.9/v1"
    response = client.post(f"/api/llm-gateway/{created['id']}/test", headers=_headers())
    assert response.status_code == 400


def test_routers_mounted_on_real_app() -> None:
    """回归防线：business_routers 漏注册时端点会 404 而单域测试仍绿。"""
    import main

    paths = {getattr(route, "path", None) for route in main.app.routes}
    assert "/api/llm-gateway" in paths
    assert any(path and path.startswith("/api/llm-gateway/") for path in paths)
