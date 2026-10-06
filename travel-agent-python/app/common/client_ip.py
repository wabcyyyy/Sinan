"""对端地址解析：只有可信代理链上的跳才采信，且从右往左数。

为什么不能直接取 `X-Forwarded-For` 的第一段：那是**客户端自己写的最左跳**。
一旦部署里有人把代理地址加进 `TRUSTED_PROXIES`（这正是"锁桶所有人共用一个 IP"的标准
修法），取第一段就等于"攻击者自带 `X-Forwarded-For: 1.2.3.4` 每次换个 IP"，
按 IP 的登录/注册/分享限速当场失效。

正确口径（RFC 7239 的简化版）：
- 直接对端（`request.client.host`）不在可信集合里 → 它送来的 XFF 一律不信，用对端地址；
- 对端可信 → 从**右**往左找第一个不可信跳，那才是我们能如实归因的客户端地址。

可信集合来自 `settings.trusted_proxies`（逗号分隔），支持 CIDR 写法
（compose 网络里 nginx 的地址是动态的，只能按网段信任）。

前提：链路上每台可信代理都必须**替换**而不是追加 XFF
（`travel-frontend-react/nginx.conf` 用 `proxy_set_header X-Forwarded-For $remote_addr;`）。
用 `$proxy_add_x_forwarded_for` 追加时，最左跳永远是客户端可伪造的字段。
"""

from __future__ import annotations

import ipaddress
import logging
from functools import lru_cache

from fastapi import Request

from app.common.config import parse_trusted_proxy, settings

logger = logging.getLogger(__name__)

Network = ipaddress.IPv4Network | ipaddress.IPv6Network


@lru_cache(maxsize=1)
def _trusted_networks(raw: str) -> tuple[Network, ...]:
    nets: list[Network] = []
    for item in raw.split(","):
        if not item.strip():
            continue
        try:
            nets.append(parse_trusted_proxy(item))
        except ValueError:
            # Settings.validate_boot 已在启动期拒绝这种配置；这里兜住的是测试里
            # 直接改 settings 的场合——坏条目按"不信任"处理，绝不放宽成"全信"。
            logger.warning("TRUSTED_PROXIES 忽略无法解析的条目: %r", item)
    return tuple(nets)


def is_trusted_address(address: str) -> bool:
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    return any(ip in net for net in _trusted_networks(settings.trusted_proxies))


def client_ip(request: Request) -> str:
    """归因用的客户端地址：无对端信息时退回 `unknown`（与既有锁桶口径一致）。"""
    remote = request.client.host if request.client else None
    if not remote:
        return "unknown"
    forwarded = request.headers.get("x-forwarded-for") or ""
    hops = [hop.strip() for hop in forwarded.split(",") if hop.strip()]
    if not hops or not is_trusted_address(remote):
        return remote
    for hop in reversed(hops):
        if not is_trusted_address(hop):
            return hop
    return remote
