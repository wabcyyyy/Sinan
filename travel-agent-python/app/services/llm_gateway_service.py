"""用户自带 LLM 网关（BYOK）配置面：CRUD、互斥启用、连通性测试与路由解析。

安全口径（本模块的立身之本）：
- api_key 明文只进 encrypt_secret 之前的内存与上游请求头构造瞬间；落库只存
  Fernet 密文 + 尾 4 位提示；任何 VO/日志/异常文案都不回显明文；
- 归属校验一律 404（不用 403 暴露资源是否存在），与 envelope.py 的既有约定一致；
- base_url 只准指向公网地址（SSRF 防线，2026-10-02 终审）：保存/启用/测试时
  解析主机名并拒绝 loopback/private/link-local/ULA/组播等保留段；残余风险 =
    保存后 DNS 改绑（TOCTOU/rebinding），已由"BYOK 路由不跟随 30x 重定向 +
  生成热路径不重复解析"收窄到需攻击者自控 DNS 才可利用。

路由语义（失败回退拍板，2026-10-02）：**网关调用失败响亮报错、不静默回退默认通道**
——静默回退会把用户自愿承担的成本转嫁给运营方 key，且同一段生成中途换模型家族会
产出割裂的行程。例外只有一类：resolve_route 遇到**基础设施故障**（网关表不可读）
时 fail-open 回默认通道并 warning（可用性优先，路由保真度让位）。密文解不开
（ApiError 409）不在此列——必须响亮上抛，让用户重录密钥，而不是无声替他烧运营方 key。
"""

from __future__ import annotations

import ipaddress
import logging
import socket
import time
from collections.abc import Iterator
from contextlib import contextmanager
from urllib.parse import urlsplit

import httpx
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from app.common import gateway_crypto
from app.common.envelope import ApiError
from app.common.llm_client import LLMClient
from app.common.llm_route import LLMRoute, use_route
from app.db.models import UserLlmGateway
from app.db.session import session_scope
from app.schemas.business.llm_gateway import LlmGatewayCreateBody, LlmGatewayUpdateBody
from app.services import quota_service

logger = logging.getLogger(__name__)

# 连通性探测的紧约束（不走 settings：专属紧闸，拉 env 键是配置面噪音）。
# 240s 读超时 ×2 次重试的同步 /test 指向黑洞地址即占线程 ~480s——压到 15s ×1 次。
_TEST_TIMEOUT_SECONDS = 15.0
_TEST_MAX_ATTEMPTS = 1


def _vo(row: UserLlmGateway) -> dict:
    """行 -> 脱敏 VO dict（camelCase 键，与 schemas/business/llm_gateway.py 对齐）。"""
    return {
        "id": row.id,
        "name": row.name,
        "baseUrl": row.base_url,
        "model": row.model,
        "apiKeyHint": row.api_key_hint,
        "enabled": bool(row.enabled),
        "createdAt": row.created_at.isoformat() if row.created_at else None,
        "updatedAt": row.updated_at.isoformat() if row.updated_at else None,
    }


def _require_row(session, user_id: int, gateway_id: int) -> UserLlmGateway:
    row = session.get(UserLlmGateway, gateway_id)
    if row is None or row.user_id != user_id:
        raise ApiError(404, "网关配置不存在")
    return row


# ---------- SSRF 防线：base_url 只准公网（2026-10-02 终审） ----------


def _resolve_host_ips(host: str) -> list[str]:
    """DNS 解析出主机名的全部地址（薄封装，测试在此打桩避免外网）。"""
    return [str(info[4][0]) for info in socket.getaddrinfo(host, None)]


def _is_blocked_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """保留段判定：回环/私网/链路本地（含 169.254.169.254 云 metadata）/ULA/未指定/组播/保留。"""
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped  # ::ffff:10.0.0.5 之类按内嵌 IPv4 判
    return bool(
        ip.is_loopback
        or ip.is_private
        or ip.is_link_local
        or ip.is_unspecified
        or ip.is_multicast
        or ip.is_reserved
        or getattr(ip, "is_unique_local", False)
    )


def _assert_public_base_url(base_url: str) -> None:
    """服务端会向 base_url POST（携带用户密钥），内网/保留段地址一律 400。

    判定口径：主机名是 IP 字面量则直接判段；否则 DNS 解析后逐地址判段
    （覆盖 `internal-svc` 这类内网域名与改 A 记录指私网的域名）。
    """
    host = urlsplit(base_url).hostname
    if not host:
        raise ApiError(400, "网关地址缺少主机名")
    lowered = host.lower()
    try:
        ipaddress.ip_address(host)  # 字面量（urlsplit 已剥 [::1] 的方括号）
        candidates: list[str] = [host]
    except ValueError:
        if lowered == "localhost" or lowered.endswith(".localhost"):
            raise ApiError(400, "网关地址不允许指向回环或内网地址") from None
        try:
            candidates = _resolve_host_ips(host)
        except socket.gaierror as exc:
            raise ApiError(400, "网关主机名无法解析，请检查地址") from exc
        if not candidates:
            raise ApiError(400, "网关主机名无法解析，请检查地址") from None
    for candidate in candidates:
        try:
            ip = ipaddress.ip_address(candidate.split("%", 1)[0])  # 剥 IPv6 zone id
        except ValueError:
            continue  # 解析器给出怪形状：不让单个坏结果放行整串地址（空串已挡）
        if _is_blocked_ip(ip):
            raise ApiError(400, "网关地址不允许指向回环或内网地址")


def list_configs(user_id: int) -> list[dict]:
    with session_scope() as session:
        rows = (
            session.execute(select(UserLlmGateway).where(UserLlmGateway.user_id == user_id).order_by(UserLlmGateway.id))
            .scalars()
            .all()
        )
        return [_vo(row) for row in rows]


def create_config(user_id: int, body: LlmGatewayCreateBody) -> dict:
    _assert_public_base_url(body.baseUrl)
    with session_scope() as session:
        row = UserLlmGateway(
            user_id=user_id,
            name=body.name,
            base_url=body.baseUrl,
            api_key_cipher=gateway_crypto.encrypt_secret(body.apiKey),
            api_key_hint=gateway_crypto.key_hint(body.apiKey),
            model=body.model,
            enabled=False,
        )
        session.add(row)
        try:
            session.flush()
        except IntegrityError as exc:
            raise ApiError(409, "同名网关配置已存在，请换一个名字") from exc
        session.refresh(row)
        return _vo(row)


def update_config(user_id: int, gateway_id: int, body: LlmGatewayUpdateBody) -> dict:
    """apiKey 留空 = 不改密钥；改动时重加密并重算尾 4 位提示。"""
    if body.baseUrl is not None:
        _assert_public_base_url(body.baseUrl)
    with session_scope() as session:
        row = _require_row(session, user_id, gateway_id)
        if body.name is not None:
            row.name = body.name
        if body.baseUrl is not None:
            row.base_url = body.baseUrl
        if body.model is not None:
            row.model = body.model
        if body.apiKey is not None:
            row.api_key_cipher = gateway_crypto.encrypt_secret(body.apiKey)
            row.api_key_hint = gateway_crypto.key_hint(body.apiKey)
        try:
            session.flush()
        except IntegrityError as exc:
            raise ApiError(409, "同名网关配置已存在，请换一个名字") from exc
        session.refresh(row)
        return _vo(row)


def delete_config(user_id: int, gateway_id: int) -> None:
    """硬删（凭据面不留墓碑）；删的是启用中配置即同时停用——路由自然回默认通道。"""
    with session_scope() as session:
        row = _require_row(session, user_id, gateway_id)
        session.delete(row)


def enable_config(user_id: int, gateway_id: int) -> dict:
    """互斥启用：同一事务内先清本用户全部 enabled，再置 1（MySQL 无部分唯一索引）。

    启用即校验密文可解密：解不开（加密 key 轮换过）的配置不允许持有启用位，
    用户当场拿到 409 重录，而不是启用后每次生成才炸（2026-10-02 终审）。
    """
    with session_scope() as session:
        row = _require_row(session, user_id, gateway_id)
        _route_of(row)
        session.execute(update(UserLlmGateway).where(UserLlmGateway.user_id == user_id).values(enabled=False))
        row.enabled = True
        session.flush()
        session.refresh(row)
        return _vo(row)


def disable_config(user_id: int, gateway_id: int) -> dict:
    with session_scope() as session:
        row = _require_row(session, user_id, gateway_id)
        row.enabled = False
        session.flush()
        session.refresh(row)
        return _vo(row)


def _sanitize_error(message: str, api_key: str) -> str:
    """错误文案脱敏：防御性抹掉密钥材料并截断（上游异常一般不含 key，这里是保底）。"""
    safe = message.replace(api_key, "sk-***") if api_key else message
    return safe[:200] or "连接失败"


def _describe_test_failure(exc: Exception) -> str:
    """按异常类型映射连通性失败文案。

    httpx 异常的 str() 内嵌完整请求 URL（含网关地址/端口），直接回显等于把
    "这个内网地址通不通、返回什么状态"递给用户当探测结果（2026-10-02 终审）——
    一律不透传 str(exc)，只给类型化的结论。
    """
    if isinstance(exc, httpx.HTTPStatusError):
        return f"上游返回 HTTP {exc.response.status_code}"
    if isinstance(exc, httpx.TimeoutException):
        return f"连接超时（网关未在 {int(_TEST_TIMEOUT_SECONDS)} 秒内响应）"
    if isinstance(exc, httpx.ConnectError):
        return "无法建立连接（域名解析失败或地址不可达）"
    if isinstance(exc, httpx.TransportError):
        return "网络传输错误"
    return f"连接失败（{type(exc).__name__}）"


def test_connection(user_id: int, gateway_id: int) -> dict:
    """发一条 1-token 消息探测连通性。

    探测走运行时同一 LLMClient 类与共享连接池，但配**专属紧约束**：15s 读超时 +
    不重试 + 不跟随重定向——/test 是同步端点，默认 240s×2 的黑洞地址会占满业务
    线程池（2026-10-02 终审）；另有分钟窗限频（quota_service）。
    """
    quota_service.enforce_gateway_test_budget(user_id)
    with session_scope() as session:
        row = _require_row(session, user_id, gateway_id)
        route = _route_of(row)
    _assert_public_base_url(route.base_url)
    probe = LLMClient(
        base_url=route.base_url,
        api_key=route.api_key,
        model=route.model,
        timeout=_TEST_TIMEOUT_SECONDS,
        max_attempts=_TEST_MAX_ATTEMPTS,
        follow_redirects=False,
    )
    started = time.monotonic()
    try:
        probe.chat([{"role": "user", "content": "ping"}], max_tokens=1)
    except Exception as exc:
        message = _sanitize_error(_describe_test_failure(exc), route.api_key)
        logger.info("gateway %s connectivity test failed for user %s: %s", gateway_id, user_id, message)
        return {
            "ok": False,
            "latencyMs": int((time.monotonic() - started) * 1000),
            "message": message,
        }
    return {"ok": True, "latencyMs": int((time.monotonic() - started) * 1000), "message": "连接成功"}


def _route_of(row: UserLlmGateway) -> LLMRoute:
    try:
        api_key = gateway_crypto.decrypt_secret(row.api_key_cipher)
    except ValueError:
        # 解不开（加密 key 轮换过）：这里测的是连通性不是密文，响亮指出需重录
        raise ApiError(409, "密钥密文无法解密（加密 key 已轮换），请重新填写 API 密钥") from None
    return LLMRoute(base_url=row.base_url, api_key=api_key, model=row.model, label=row.name)


def resolve_route(user_id: int) -> LLMRoute | None:
    """当前生效路由：enabled=1 的行 + 解密。

    失败语义（对齐模块 docstring 的拍板）：
    - **基础设施故障**（网关表不可读等非 ApiError 异常）：fail-open 回默认通道
      + warning——可用性优先，路由保真度让位；
    - **密文解不开**（_route_of 的 ApiError 409）：响亮上抛。吞掉它等于轮换
      加密 key 后全员静默切回运营方付费通道，且设置页还挂着"生效中"横幅
      （2026-10-02 终审）。
    """
    try:
        with session_scope() as session:
            row = (
                session.execute(select(UserLlmGateway).where(UserLlmGateway.user_id == user_id, UserLlmGateway.enabled))
                .scalars()
                .first()
            )
            if row is None:
                return None
            return _route_of(row)
    except ApiError:
        raise
    except Exception as exc:
        logger.warning("resolve llm gateway route failed for user %s, falling back to default: %s", user_id, exc)
        return None


@contextmanager
def route_scope(user_id: int) -> Iterator[LLMRoute | None]:
    """烧 LLM 的业务腿统一入口：把该用户的生效网关压进 ContextVar，退出还原。

    注入点纪律见 app/common/llm_route.py：contextvars 不跨线程池传播，必须在
    显式持有 user_id 的 worker 入口内部进入。
    """
    with use_route(resolve_route(user_id)):
        yield
