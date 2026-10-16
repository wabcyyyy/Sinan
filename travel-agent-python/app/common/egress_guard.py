"""不可信出网防护：把"只准打公网地址"落在真正的**连接边界**上（T3，2026-10-17）。

为什么是这个形状：BYOK 网关地址由用户填写，服务端会带着**用户的密钥**向它 POST。旧实现
只在保存/测试时解析一次 DNS 再判段，于是"保存时公网、连接时私网"（DNS 改绑、多 A/AAAA
混排、30x 跳转、环境代理改道）仍然能把请求引到 169.254.169.254 这类内网地址。
"请求前再查一次 DNS"也关不掉竞态——查到的地址和真正连上去的地址不是同一份。
判据因此下移到 socket 连接前一刻：

- 解析主机名 → 对**每一个**返回地址判段，任一非公网即整串拒绝（多答案混排不能漏）；
- 直接连到**刚校验过的那个 IP**：拨号参数是 IP 字面量，操作系统不会再解析一次，
  主机名只留在 TLS 里——SNI 与证书主机名校验仍按原主机名进行，钉 IP 不削弱 HTTPS；
- 不复用保活连接（`max_keepalive_connections=0`）：每个请求都重新解析、重新校验，
  改绑后的私网地址不会靠一条旧连接"活着"；
- 不读环境代理：`trust_env=False` + 显式 transport（httpx 只在 `transport is None`
  时才把 `HTTP(S)_PROXY` 挂成 mount）+ `proxy=None`，代理改道够不到这一腿；
- 重定向：受守卫通道一律不跟随；即使跟随，每一跳的连接仍要重新过本模块（跳板地址
  不会因为是"上游给的"而被放行）；
- 受守卫腿只走 HTTPS——这一腿携带的是交给用户网关的凭据，明文 http 在连接层同样拒绝
  （入口校验之外不留退路）。

**覆盖范围（诚实边界）**：本模块只服务**用户可控**的目的地（BYOK 网关、用户给的 URL）。
运营方固定的通道——平台 LLM、OpenTripMap/Nominatim/Overpass、告警 webhook、白名单图床
——不走这里：它们的地址来自 `.env`，不是攻击者输入，套用同一拒绝规则只会在 DNS/CDN
抖动时误伤生产。放行策略按"谁控制目的地"分，不按"是不是内网地址"混同。

密钥口径：本模块不接触请求头，异常消息也**永不含**主机名与解析到的地址——
被拒绝的一方只能知道"这个地址不被允许"，拿不到"内网哪台活着"。
"""

from __future__ import annotations

import ipaddress
import logging
import socket
import typing
from urllib.parse import urlsplit

import httpx

logger = logging.getLogger(__name__)

# 拒绝原因码（异常消息一律不带主机名/地址，原因码只用于上层换文案）。
REFUSED_SCHEME = "scheme"  # 非 http(s)
REFUSED_HTTPS_REQUIRED = "https_required"  # 用户网关必须是 HTTPS
REFUSED_HOST = "host"  # 缺主机名 / URL 形状不对
REFUSED_UNRESOLVED = "unresolved"  # 解析不出地址（fail closed）
REFUSED_BLOCKED = "blocked"  # 任一地址落在回环/私网/链路本地/ULA/保留段

_GENERIC_MESSAGE = "出网目标不是公网可路由地址"


class EgressRefused(RuntimeError):
    """拒绝出网（fail closed）。`code` 是原因码；消息里没有任何可探测内网的细节。"""

    def __init__(self, code: str = REFUSED_BLOCKED) -> None:
        super().__init__(_GENERIC_MESSAGE)
        self.code = code


def is_blocked_ip(ip: str | ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """保留段判定：回环/私网/链路本地（含 169.254.169.254 云 metadata）/未指定/组播/保留/ULA。

    两条额外口径：
    - `::ffff:10.0.0.5` 这类 IPv4 映射地址按内嵌 IPv4 判（否则 `::ffff:93.184.216.34` 会绕过）；
    - `is_global` 兜住 `is_private` 放行但也不可路由的段：CGNAT `100.64.0.0/10`、
      基准测试段 `198.18.0.0/15`、文档段等。公网网关不会住在这些段里，误伤面为零。
    """
    address = ipaddress.ip_address(ip) if isinstance(ip, str) else ip
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    return bool(
        address.is_loopback
        or address.is_private
        or address.is_link_local
        or address.is_unspecified
        or address.is_multicast
        or address.is_reserved
        or getattr(address, "is_unique_local", False)
        or not address.is_global
    )


def _lookup(host: str, port: int | None) -> list[str]:
    """解析主机名为地址列表（薄接缝：测试在此打桩，既不触真实 DNS 也不扫描任何真实网段）。"""
    return [str(info[4][0]) for info in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)]


def _normalize_address(candidate: str) -> str | None:
    """把解析结果规整成纯 IP（剥 IPv6 方括号与 `%zone`）；怪形状返回 None。"""
    raw = candidate.strip().strip("[]").split("%", 1)[0]
    try:
        ipaddress.ip_address(raw)
    except ValueError:
        return None
    return raw


def resolve_addresses(host: str, port: int | None = None) -> list[str]:
    """IP 字面量直接用；域名走解析器。解析失败/空答案/怪形状一律 fail closed。"""
    literal = _normalize_address(host)
    if literal is not None:
        return [literal]
    try:
        candidates = _lookup(host, port)
    except (socket.gaierror, OSError) as exc:
        raise EgressRefused(REFUSED_UNRESOLVED) from exc
    # 逐个规整：解析器给出一个怪形状不代表整串地址可用，但也不能让它替私网开门。
    addresses = [address for address in map(_normalize_address, candidates) if address is not None]
    if not addresses:
        raise EgressRefused(REFUSED_UNRESOLVED)
    return list(dict.fromkeys(addresses))


def validated_addresses(host: str, port: int | None = None) -> list[str]:
    """解析 + **逐地址**判段：任一答案非公网 → 整串拒绝。"""
    addresses = resolve_addresses(host, port)
    for address in addresses:
        if is_blocked_ip(address):
            raise EgressRefused(REFUSED_BLOCKED)
    return addresses


def pinned_address(host: str, port: int | None = None) -> str:
    """连接边界用的一步：返回**本请求刚解析并校验过**的第一个地址。"""
    return validated_addresses(host, port)[0]


def assert_public_url(url: str, *, require_https: bool = False) -> None:
    """URL 级校验（保存时反馈 + 跳转逐跳复核）；真正的目的地约束在 connect_tcp 里。

    `require_https`：用户自带的 LLM 网关必须走 HTTPS——这一腿携带用户密钥，
    明文 http 等于把密钥交给路径上任何一跳。运营可信通道不用这个参数。
    """
    try:
        parts = urlsplit(url)
    except ValueError as exc:
        raise EgressRefused(REFUSED_HOST) from exc
    scheme = parts.scheme.lower()
    if scheme not in ("http", "https"):
        raise EgressRefused(REFUSED_SCHEME)
    if require_https and scheme != "https":
        raise EgressRefused(REFUSED_HTTPS_REQUIRED)
    host = (parts.hostname or "").lower()
    if not host:
        raise EgressRefused(REFUSED_HOST)
    if host == "localhost" or host.endswith(".localhost"):
        raise EgressRefused(REFUSED_BLOCKED)
    validated_addresses(host, parts.port or (443 if scheme == "https" else 80))


class _GuardedSocketBackend:
    """httpcore socket 后端的守卫层：只改"连到哪儿"，其余全部转交被包的 backend。

    协议面（`connect_tcp` / `connect_unix_socket` / `sleep`）与 httpcore 的
    `NetworkBackend` 一致；这里不 import httpcore，避免给它加直接依赖名义——
    被包对象就是连接池自己造出来的那个后端实例。
    """

    def __init__(self, inner: typing.Any) -> None:
        self._inner = inner

    def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: typing.Any = None,
    ) -> typing.Any:
        # 每次建连都重新解析：改绑后的私网地址在这里被挡，拨出去的也一定是校验过的 IP。
        address = pinned_address(host, port)
        logger.debug("egress guard: validated outbound connection to %s:%s", host, port)
        return self._inner.connect_tcp(
            address, port, timeout=timeout, local_address=local_address, socket_options=socket_options
        )

    def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,
        socket_options: typing.Any = None,
    ) -> typing.Any:
        raise EgressRefused(REFUSED_BLOCKED)

    def sleep(self, seconds: float) -> None:
        self._inner.sleep(seconds)


class GuardedEgressTransport(httpx.HTTPTransport):
    """受守卫的 httpx 传输层：连接池/TLS/异常映射用 httpx 自己的实现，拨号后端换成守卫层。

    为什么不是"自己建 httpcore 池"或"改写 URL 到 IP"：前者要复制 httpx 的 TLS/limits
    装配（副本必漂移），后者会把 SNI/证书主机名一起换成 IP（httpx 0.27 的 SNI 取 URL
    host），还会让不同主机名共用同一 IP 时的连接复用串味。httpx 0.27 的
    `HTTPTransport.__init__` 不接 `network_backend`（实测签名），所以这里在构造完成后
    替换连接池的拨号后端——只换这一步，池子里其余逻辑一行没动。

    `socket_backend` 是测试注入点：替换守卫**下面**的拨号层，用于离线断言"交给 socket 的
    地址就是校验过的 IP"。守卫在注入层之上，注入不构成绕过；生产调用点一律不传。
    """

    def __init__(self, *, max_connections: int = 20, socket_backend: typing.Any = None) -> None:
        super().__init__(
            verify=True,  # 证书校验不关，也不给参数口子（没有"关掉守卫"这条路）
            trust_env=False,  # 不吃 SSL_CERT_FILE / 代理一类环境开关
            proxy=None,  # 没有改道面
            retries=0,
            # 不复用保活连接：每个请求都重新解析+重新校验（改绑要当场生效，不靠旧连接活着）。
            limits=httpx.Limits(
                max_connections=max_connections,
                max_keepalive_connections=0,
                keepalive_expiry=0.0,
            ),
        )
        # 只换"拨号后端"这一步：池子里其余逻辑（TLS 上下文装配、limits、ALPN、异常映射）
        # 全部沿用 httpx 自己的实现。类型上这里把池当 Any 写——httpx 0.27 的
        # HTTPTransport 不接 network_backend 参数（实测签名），这是本模块唯一一处
        # 伸手进连接池的地方；proxy=None 决定了 _pool 一定是普通 ConnectionPool
        # （代理池不接受 network_backend，也不允许存在——见上面的 trust_env/proxy）。
        # 不为这一行去 import httpcore：它只是 httpx 的传递依赖，声明成直接依赖
        # 等于给仓库加一条没人要求的边（AGENTS：新增依赖要有理由）。
        pool: typing.Any = self._pool
        backend = socket_backend if socket_backend is not None else pool._network_backend
        pool._network_backend = _GuardedSocketBackend(backend)

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        """受守卫腿一律 HTTPS：不给明文通道留口子，也不给调用方关掉的参数。

        这一腿携带的是"交给用户网关的凭据"。入口校验（create/update/enable/test）之外
        再钉一次：绕过保存口直接写进库的 `http://` 地址在这里同样过不去，
        密钥不会以明文出网。scheme 之外的目的地约束在 connect_tcp 里，两处共用原因码。
        """
        if request.url.scheme != "https":
            raise EgressRefused(REFUSED_HTTPS_REQUIRED)
        return super().handle_request(request)


def guarded_client(*, max_connections: int = 20, timeout: httpx.Timeout | float | None = None) -> httpx.Client:
    """用户可控目的地的出网通道（唯一入口）。

    刻意不开放 `verify` / `trust_env` / `proxy` / `follow_redirects` / scheme 相关参数：
    这些就是守卫本身，能被调用方关掉就不叫边界。需要跟随跳转的调用方必须在每一跳自己调
    `assert_public_url`（跳转目的地的实际连接仍会过守卫层）。
    """
    transport = GuardedEgressTransport(max_connections=max_connections)
    return httpx.Client(
        transport=transport,
        trust_env=False,
        follow_redirects=False,
        timeout=timeout if timeout is not None else httpx.Timeout(30.0, connect=5.0),
    )
