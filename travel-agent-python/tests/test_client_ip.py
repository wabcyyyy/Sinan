"""对端地址归因（R1-5）：只有可信代理链上的跳才采信，且从右往左数。

钉住的是两个相反方向的失误，历史上都真出过：
1. 无脑取 `X-Forwarded-For` 首段 → 攻击者自带一个头就能每次换个 IP，
   按 IP 的登录/注册/分享限速整体失效；
2. 反过来完全不看 XFF（compose 里对端永远是 nginx）→ 所有访客共用一个锁桶，
   匿名者可以用 5 次失败登录把任意账号锁死 15 分钟。
"""

from __future__ import annotations

from fastapi import Request

from app.common.client_ip import client_ip
from app.common.config import settings


def _request(peer: str, xff: str | None = None) -> Request:
    headers = [] if xff is None else [(b"x-forwarded-for", xff.encode("latin-1"))]
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/api/auth/login",
        "headers": headers,
        "client": (peer, 43210),
        "query_string": b"",
        "scheme": "http",
        "server": ("testserver", 80),
        "root_path": "",
    }
    return Request(scope)


def test_untrusted_peer_cannot_choose_its_own_bucket(monkeypatch) -> None:
    monkeypatch.setattr(settings, "trusted_proxies", "127.0.0.1")
    forged = _request("203.0.113.9", "1.1.1.1, 2.2.2.2")
    assert client_ip(forged) == "203.0.113.9"


def test_trusted_peer_yields_rightmost_untrusted_hop(monkeypatch) -> None:
    """链首永远是客户端可写的，可信代理之后那一段才算数。"""
    monkeypatch.setattr(settings, "trusted_proxies", "127.0.0.1,10.0.0.0/8")
    via_proxy = _request("10.1.2.3", "9.9.9.9, 203.0.113.7, 10.9.9.9")
    assert client_ip(via_proxy) == "203.0.113.7"


def test_all_trusted_chain_falls_back_to_the_peer(monkeypatch) -> None:
    monkeypatch.setattr(settings, "trusted_proxies", "127.0.0.1,10.0.0.0/8")
    intra_net = _request("10.1.2.3", "10.5.5.5, 10.6.6.6")
    assert client_ip(intra_net) == "10.1.2.3"


def test_missing_peer_is_unknown_not_empty(monkeypatch) -> None:
    monkeypatch.setattr(settings, "trusted_proxies", "127.0.0.1")
    assert client_ip(_request("not-an-ip")) == "not-an-ip"


def test_ipv6_loopback_literal_is_trusted_without_prefix_length(monkeypatch) -> None:
    """默认值里同时有 `::1` 与 `0:0:0:0:0:0:0:1` 两种写法，都得解析成 /128。"""
    monkeypatch.setattr(settings, "trusted_proxies", "127.0.0.1,0:0:0:0:0:0:0:1,::1")
    assert client_ip(_request("::1", "203.0.113.7")) == "203.0.113.7"
