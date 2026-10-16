"""出网守卫（app/common/egress_guard）单测：连接边界的 SSRF 防护，全离线。

打桩面只有两个，都是**受控假答案**：
- `egress_guard._lookup`：解析器接缝，返回写死的地址串（不触真实 DNS，不扫描任何真实网段）；
- `GuardedEgressTransport(socket_backend=...)`：守卫**下面**的拨号层，记录"真正把哪个地址
  交给 socket"并吐 canned HTTP 字节（不建真实 TCP/TLS）。注入点在被包裹层，守卫在它上面，
  注入不构成绕过；`verify` / `trust_env` / `proxy` / `follow_redirects` 一律没有参数口子。

用例对应的验收项（PLAN §5 T3）：保存时公网 + 连接时私网被挡、多 A/AAAA 混排被挡、
IPv6 ULA 与 IPv4-mapped 被挡、跳转到 169.254 被挡、正常公网 HTTPS 网关可用、
代理环境不能绕过、失败不提供内网探测详情、连接不复用（改绑当场生效）。
"""

from __future__ import annotations

import inspect
import ssl

import httpx
import pytest

import app.agent  # noqa: F401  先完成 agent 门面导入：存量导入环 llm_client→facade→web_search→llm_client（同 test_llm_circuit.py 的口径）。
from app.common import egress_guard, llm_client
from app.common.egress_guard import (
    GuardedEgressTransport,
    assert_public_url,
    guarded_client,
    is_blocked_ip,
)
from app.common.llm_route import LLMRoute, use_route

PUBLIC_V4 = "93.184.216.34"
PUBLIC_V6 = "2606:2800:220:1:24:21ff:fe1d:2e99"
_OK = b"HTTP/1.1 200 OK\r\ncontent-type: application/json\r\ncontent-length: 2\r\n\r\n{}"
_JUMP = b"HTTP/1.1 302 Found\r\nlocation: http://169.254.169.254/latest/meta-data\r\ncontent-length: 0\r\n\r\n"


class _FakeStream:
    """守卫下面的假 socket 流：吐出 canned HTTP 响应，并记录 TLS 握手的 server_hostname。"""

    def __init__(self, record: dict, body: bytes) -> None:
        self._record = record
        self._buffer = body

    def read(self, max_bytes: int, timeout: float | None = None) -> bytes:
        chunk, self._buffer = self._buffer[:max_bytes], self._buffer[max_bytes:]
        return chunk

    def write(self, buffer: bytes, timeout: float | None = None) -> None:
        self._record["written"] = self._record.get("written", b"") + buffer

    def close(self) -> None:
        return None

    def start_tls(
        self, ssl_context: ssl.SSLContext, server_hostname: str | None = None, timeout: float | None = None
    ) -> _FakeStream:
        self._record["server_hostname"] = server_hostname
        self._record["check_hostname"] = ssl_context.check_hostname
        self._record["verify_mode"] = ssl_context.verify_mode
        return self

    def get_extra_info(self, info: str):  # httpcore 协议面：只问 ssl_object/is_readable，这里都给 None
        return None


class _FakeDialer:
    """假拨号层：记录每次 `(address, port)`，按脚本返回假流（脚本用尽即 200）。"""

    def __init__(self, bodies: list[bytes] | None = None) -> None:
        self.dials: list[tuple[str, int]] = []
        self.record: dict = {}
        self._bodies = list(bodies or [])

    def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: object = None,
    ) -> _FakeStream:
        self.dials.append((host, port))
        body = self._bodies.pop(0) if self._bodies else _OK
        return _FakeStream(self.record, body)

    def connect_unix_socket(self, path: str, timeout: float | None = None, socket_options: object = None) -> None:
        raise AssertionError("守卫下面不该走到 Unix 套接字")

    def sleep(self, seconds: float) -> None:
        return None


def _client(dialer: _FakeDialer, *, follow_redirects: bool = False) -> httpx.Client:
    return httpx.Client(
        transport=GuardedEgressTransport(socket_backend=dialer),
        trust_env=False,
        follow_redirects=follow_redirects,
    )


def _answers(monkeypatch, *addresses: str) -> None:
    monkeypatch.setattr(egress_guard, "_lookup", lambda _host, _port: list(addresses))


# ---------- 段判定与逐地址校验（单一原语） ---------------------------------------


def test_is_blocked_ip_covers_reserved_shapes() -> None:
    for blocked in (
        "127.0.0.1",
        "10.1.2.3",
        "172.16.0.9",
        "192.168.1.4",
        "169.254.169.254",  # 云 metadata
        "0.0.0.0",
        "::1",
        "fd00::1",  # ULA
        "fe80::1",  # 链路本地
        "224.0.0.1",  # 组播
        "240.0.0.1",  # 保留
        "::ffff:10.0.0.5",  # IPv4 映射：按内嵌 IPv4 判
        "100.64.0.1",  # CGNAT：is_private 放行但不可路由，is_global 兜住
        "198.18.0.1",  # 基准测试段
        "203.0.113.7",  # 文档段
    ):
        assert is_blocked_ip(blocked) is True, blocked
    for allowed in (PUBLIC_V4, "8.8.8.8", PUBLIC_V6, "2606:4700:4700::1111"):
        assert is_blocked_ip(allowed) is False, allowed


def test_multi_answer_dns_with_one_private_is_blocked(monkeypatch) -> None:
    """一个公网混一个私网（多 A/AAAA 记录）：整串拒绝，不是"挑一个能连的"。"""
    dialer = _FakeDialer()
    _answers(monkeypatch, PUBLIC_V4, "10.1.2.3")
    with _client(dialer) as client, pytest.raises(egress_guard.EgressRefused):
        client.get("https://mixed.example/v1")
    assert dialer.dials == [], "混排里有私网答案时不该拨任何地址"


def test_ipv6_ula_and_ipv4_mapped_are_blocked(monkeypatch) -> None:
    dialer = _FakeDialer()
    for answers in (["fd00::1"], ["::ffff:127.0.0.1"], [PUBLIC_V6, "fd00::42"], ["fe80::1%eth0"]):
        _answers(monkeypatch, *answers)
        with _client(dialer) as client, pytest.raises(egress_guard.EgressRefused):
            client.get("https://v6.example/v1")
    assert dialer.dials == []


def test_public_ipv6_answer_is_pinned_not_skipped(monkeypatch) -> None:
    """只有 AAAA 的公网网关：拨的就是那个 v6 地址（不因地址族绕过校验，也不回退重新解析）。"""
    dialer = _FakeDialer()
    _answers(monkeypatch, PUBLIC_V6)
    with _client(dialer) as client:
        assert client.get("https://v6only.example/v1").status_code == 200
    assert dialer.dials == [(PUBLIC_V6, 443)]


def test_garbage_resolver_answer_fails_closed(monkeypatch) -> None:
    """解析器给出怪形状：既不能替私网开门，也不能被当成可连接地址。"""
    dialer = _FakeDialer()
    _answers(monkeypatch, "not-an-ip", "10.0.0.5")
    with _client(dialer) as client, pytest.raises(egress_guard.EgressRefused) as mixed:
        client.get("https://odd.example/v1")
    assert mixed.value.code == egress_guard.REFUSED_BLOCKED
    _answers(monkeypatch, "not-an-ip")
    with _client(dialer) as client, pytest.raises(egress_guard.EgressRefused) as alone:
        client.get("https://odd.example/v1")
    assert alone.value.code == egress_guard.REFUSED_UNRESOLVED


def test_unresolvable_host_fails_closed(monkeypatch) -> None:
    import socket

    def _raise(_host: str, _port: int | None) -> list[str]:
        raise socket.gaierror(8, "nodename nor servname provided")

    monkeypatch.setattr(egress_guard, "_lookup", _raise)
    dialer = _FakeDialer()
    with _client(dialer) as client, pytest.raises(egress_guard.EgressRefused) as excinfo:
        client.get("https://gone.example/v1")
    assert excinfo.value.code == egress_guard.REFUSED_UNRESOLVED
    assert dialer.dials == []


# ---------- 连接边界：改绑 / 复用 / 钉住 ------------------------------------------


def test_save_time_public_connect_time_private_is_blocked(monkeypatch) -> None:
    """同一 client、同一 URL：第一次解析公网放行，第二次解析改绑私网 → 挡在拨号之前。

    这条就是 "rebind-proof" 的定义：守卫在建连那刻问解析器，没有复用保存/上一次的答案。
    """
    dialer = _FakeDialer()
    state = {"calls": 0}

    def _lookup(_host: str, _port: int | None) -> list[str]:
        state["calls"] += 1
        return [PUBLIC_V4] if state["calls"] == 1 else ["169.254.169.254"]

    monkeypatch.setattr(egress_guard, "_lookup", _lookup)
    with _client(dialer) as client:
        assert client.get("https://rebind.example/v1").status_code == 200, "公网答案时正常放行"
        assert dialer.dials == [(PUBLIC_V4, 443)]
        with pytest.raises(egress_guard.EgressRefused):
            client.get("https://rebind.example/v1")
    assert dialer.dials == [(PUBLIC_V4, 443)], "改绑后的链路本地地址没有被拨号"
    assert state["calls"] >= 2, "连接时重新查询过解析器（不是拿保存时的缓存判一次）"


def test_each_request_creates_a_fresh_validated_connection(monkeypatch) -> None:
    """不复用保活连接：两次请求 = 两次建连 = 两次校验（旧连接不会让"已经变了"的地址活着）。"""
    dialer = _FakeDialer()
    _answers(monkeypatch, PUBLIC_V4)
    with _client(dialer) as client:
        client.get("https://keepalive.example/v1")
        client.get("https://keepalive.example/v1")
    assert dialer.dials == [(PUBLIC_V4, 443), (PUBLIC_V4, 443)]


def test_redirect_to_link_local_is_not_followed_and_hop_is_revalidated(monkeypatch) -> None:
    """30x 指向 169.254：BYOK 通道不跟随（302 原样交回），内网跳板从未被拨号。

    跟随与否都不是退路：同一个守卫通道把 `follow_redirects` 打开，跳转目标的连接同样
    要重新过逐地址校验（这里是 IP 字面量，不查 DNS 也照样挡）——内网仍没被拨号。
    """
    _answers(monkeypatch, PUBLIC_V4)

    dialer = _FakeDialer(bodies=[_JUMP])
    with _client(dialer) as client:
        assert client.get("https://byok.example/v1").status_code == 302, "重定向不自动跟"
    assert dialer.dials == [(PUBLIC_V4, 443)], "内网跳板没有被请求"

    following = _FakeDialer(bodies=[_JUMP])
    with _client(following, follow_redirects=True) as client:
        with pytest.raises(egress_guard.EgressRefused):
            client.get("https://byok.example/v1")
    assert following.dials == [(PUBLIC_V4, 443)], "跟随跳转时，第二跳在拨号前就被拒"


# ---------- 正常路径：公网 HTTPS 网关必须可用 -------------------------------------


def test_public_https_gateway_still_works_and_keeps_tls_hostname(monkeypatch) -> None:
    """放行时三件事同时成立：响应正常、拨的是校验过的 IP、TLS 仍按**原主机名**校验。

    钉 IP 最容易踩的坑是把 TLS 一起降成按 IP 校验（等于不校验）。这里顺手把证书校验
    没被关（check_hostname / CERT_REQUIRED）与 Host 头仍是原主机名一起钉住。
    """
    dialer = _FakeDialer()
    _answers(monkeypatch, PUBLIC_V4, PUBLIC_V6)
    with _client(dialer) as client:
        response = client.get("https://gateway.example/v1/chat/completions")
    assert response.status_code == 200
    assert dialer.dials == [(PUBLIC_V4, 443)], "第一个答案（全部逐地址校验通过后）被钉住"
    assert dialer.record["server_hostname"] == "gateway.example", "SNI/证书主机名绑原主机名"
    assert dialer.record["check_hostname"] is True and dialer.record["verify_mode"] is ssl.CERT_REQUIRED
    head = dialer.record["written"].split(b"\r\n\r\n")[0].lower()
    assert b"host: gateway.example" in head, "虚拟主机路由仍按原主机名"
    assert PUBLIC_V4.encode() not in head, "Host 头里不该出现裸 IP"


def test_ip_literal_gateway_is_validated_without_dns(monkeypatch) -> None:
    """主机名是 IP 字面量：不查 DNS 也要判段（私网字面量挡、公网字面量放行）。"""

    def _boom(_host: str, _port: int | None) -> list[str]:
        raise AssertionError("IP 字面量不该触发解析")

    monkeypatch.setattr(egress_guard, "_lookup", _boom)
    with pytest.raises(egress_guard.EgressRefused):
        assert_public_url("https://10.0.0.5/v1")
    dialer = _FakeDialer()
    with _client(dialer) as client:
        assert client.get(f"https://{PUBLIC_V4}/v1").status_code == 200
    assert dialer.dials == [(PUBLIC_V4, 443)]


# ---------- 不留降级面：明文腿 / 代理改道 / 关守卫的参数 ---------------------------


def test_guarded_channel_refuses_plaintext_http_even_for_public_hosts(monkeypatch) -> None:
    """绕过保存口直接写进库的 `http://` 网关：连接层同样拒绝，密钥不以明文出网。"""
    dialer = _FakeDialer()
    _answers(monkeypatch, PUBLIC_V4)
    with _client(dialer) as client, pytest.raises(egress_guard.EgressRefused) as excinfo:
        client.get("http://gateway.example/v1/chat/completions")
    assert excinfo.value.code == egress_guard.REFUSED_HTTPS_REQUIRED
    assert dialer.dials == [], "明文腿没有拨号"


def test_proxy_environment_cannot_reroute(monkeypatch) -> None:
    """HTTP(S)_PROXY / ALL_PROXY 改不了受守卫的腿：没有 env mount，拨号地址仍是校验过的 IP。"""
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        monkeypatch.setenv(key, "http://127.0.0.1:9")
    dialer = _FakeDialer()
    _answers(monkeypatch, PUBLIC_V4)
    with _client(dialer) as client:
        assert client.get("https://byok.example/v1").status_code == 200
        assert client.trust_env is False, "受守卫通道不读环境"
    assert dialer.dials == [(PUBLIC_V4, 443)], "没有把连接交给代理，仍是直连钉住的 IP"
    with pytest.raises(egress_guard.EgressRefused):
        # 代理指向内网也不构成"先连代理再说"：目的地校验照旧失败。
        _answers(monkeypatch, "10.0.0.5")
        with _client(_FakeDialer()) as client:
            client.get("https://byok.example/v1")


def test_guard_has_no_kill_switch(monkeypatch) -> None:
    """工厂不给"关掉守卫"的参数：verify / trust_env / proxy / follow_redirects 全部定死。"""
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
        monkeypatch.setenv(key, "http://127.0.0.1:9")
    client = guarded_client()
    with client:
        assert client.trust_env is False
        assert client.follow_redirects is False
        assert dict(client._mounts) == {}, "环境代理没有被挂成 transport"
        assert isinstance(client._transport, GuardedEgressTransport)
        assert client._transport._pool._max_keepalive_connections == 0, "保活复用被关掉（每请求重新校验）"
    assert set(inspect.signature(guarded_client).parameters) == {"max_connections", "timeout"}
    assert "socket_backend" not in inspect.signature(guarded_client).parameters, "测试注入面不进工厂签名"


def test_unix_socket_egress_is_refused(monkeypatch) -> None:
    dialer = _FakeDialer()
    _answers(monkeypatch, PUBLIC_V4)
    backend = GuardedEgressTransport(socket_backend=dialer)._pool._network_backend
    with pytest.raises(egress_guard.EgressRefused):
        backend.connect_unix_socket("/var/run/docker.sock")


# ---------- 拒绝的结论不回显内网信息 --------------------------------------------


def test_refusal_carries_no_host_or_address(monkeypatch) -> None:
    """异常文本里没有主机名、没有解析到的地址：被拒绝的一方拿不到"内网哪台活着"。"""
    _answers(monkeypatch, "10.1.2.3")
    with pytest.raises(egress_guard.EgressRefused) as excinfo:
        assert_public_url("https://internal-db.corp.example/v1")
    message = str(excinfo.value)
    for leak in ("10.1.2.3", "internal-db", "corp"):
        assert leak not in message
    assert excinfo.value.code == egress_guard.REFUSED_BLOCKED


# ---------- URL 级原语（保存面与逐跳复核共用同一份） ----------------------------


def test_assert_public_url_rules() -> None:
    with pytest.raises(egress_guard.EgressRefused) as scheme:
        assert_public_url("ftp://public.example/x")
    assert scheme.value.code == egress_guard.REFUSED_SCHEME
    with pytest.raises(egress_guard.EgressRefused) as missing:
        assert_public_url("https:///no-host")
    assert missing.value.code == egress_guard.REFUSED_HOST
    with pytest.raises(egress_guard.EgressRefused) as loop_name:
        assert_public_url("https://localhost:11434/v1")
    assert loop_name.value.code == egress_guard.REFUSED_BLOCKED
    with pytest.raises(egress_guard.EgressRefused) as metadata:
        assert_public_url("http://169.254.169.254/latest/meta-data")
    assert metadata.value.code == egress_guard.REFUSED_BLOCKED


def test_assert_public_url_requires_https_for_user_gateways(monkeypatch) -> None:
    _answers(monkeypatch, PUBLIC_V4)
    assert_public_url("https://gateway.example/v1", require_https=True)
    with pytest.raises(egress_guard.EgressRefused) as plain:
        assert_public_url("http://gateway.example/v1", require_https=True)
    assert plain.value.code == egress_guard.REFUSED_HTTPS_REQUIRED
    # 运营可信通道（图片白名单那类）不强制 HTTPS：原语按调用方口径，不在这里夹带
    assert_public_url("http://gateway.example/v1")


# ---------- 接线：BYOK 腿确实走受守卫通道 ---------------------------------------


def _stub_client(sink: list) -> httpx.Client:
    def _handler(request: httpx.Request) -> httpx.Response:
        sink.append(str(request.url))
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}], "usage": {}})

    return httpx.Client(transport=httpx.MockTransport(_handler))


def test_byok_route_leg_uses_guarded_channel_and_platform_leg_does_not(monkeypatch) -> None:
    """路由生效 → 走受守卫池；运营方通道 → 仍走共享池。两条腿不能接反（也不串味）。"""
    guarded: list = []
    shared: list = []
    monkeypatch.setattr(llm_client, "_get_guarded_http_client", lambda: _stub_client(guarded))
    monkeypatch.setattr(llm_client, "_get_http_client", lambda: _stub_client(shared))
    messages = [{"role": "user", "content": "hi"}]

    with use_route(LLMRoute(base_url="https://byok.example/v1", api_key="sk-user-key", model="m")):
        client = llm_client.get_llm_client()
        assert client._guarded_egress is True, "BYOK 路由 client 必须带守卫"
        client.chat_response(messages)
        assert client._follow_redirects is False, "BYOK 不跟随重定向"

    llm_client.LLMClient(base_url="https://platform.example/v1", api_key="platform-key", model="m").chat_response(
        messages
    )

    assert guarded == ["https://byok.example/v1/chat/completions"], "生成/恢复这条腿走的是受守卫通道"
    assert shared == ["https://platform.example/v1/chat/completions"], "运营 key 仍走共享池"
