"""用户自带 LLM 网关（BYOK）配置面：CRUD、互斥启用、连通性测试与路由解析。

安全口径（本模块的立身之本）：
- api_key 明文只进 encrypt_secret 之前的内存与上游请求头构造瞬间；落库只存
  Fernet 密文 + 尾 4 位提示；任何 VO/日志/异常文案都不回显明文；
- 归属校验一律 404（不用 403 暴露资源是否存在），与 envelope.py 的既有约定一致；
- base_url 只准指向**公网 HTTPS** 地址：保存/启用/测试时先做 URL 级校验（快反馈、
  可解释的 400），**真正的约束在连接层**——带用户密钥的每一腿由 `guarded_egress`
  走 app/common/egress_guard 的受守卫通道：目的地在 socket 前一刻重新解析、逐个答案
  判段、并钉到校验过的 IP（SNI/证书校验仍按原主机名），不复用保活连接、不跟随重定向、
  不读环境代理（T3，2026-10-17）。旧口径"保存时解析一次 + 不跟随 30x"关不掉
  DNS 改绑竞态，已被这条取代；被拒绝的尝试只回"不允许指向内网"，不回显内网探测结果。

统一口径：create / update / enable / test 走同一个 `_assert_public_base_url`，
generate 与 resume（resolve_route → route_scope → _routed_client）走同一个受守卫通道，
没有"某条路径忘了校验"这种缝。

路由语义（失败回退拍板，2026-10-02）：**网关调用失败响亮报错、不静默回退默认通道**
——静默回退会把用户自愿承担的成本转嫁给运营方 key，且同一段生成中途换模型家族会
产出割裂的行程。例外只有一类：resolve_route 遇到**基础设施故障**（网关表不可读）
时 fail-open 回默认通道（可用性优先，路由保真度让位）。但这一腿烧的就不再是用户的 key，
所以同步登记**归属降级**（V17/T2）：成本账记 platform 并留 `byok_lookup_failed` 原因、
ERROR 日志、指标计数——"静默把用户的成本转给平台" 从此是可追责事件，不是一句 warning。
密文解不开（ApiError 409）不在此列——必须响亮上抛，让用户重录密钥，而不是无声替他烧运营方 key。
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterator
from contextlib import contextmanager

import httpx
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from app.agent import metrics
from app.common import cost_policy, egress_guard, gateway_crypto, llm_route
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


# ---------- 公网 HTTPS 约束：入口校验（连接层的硬约束在 app/common/egress_guard） ----------

_REFUSED_MESSAGES = {
    egress_guard.REFUSED_SCHEME: "网关地址必须是 http(s) URL",
    egress_guard.REFUSED_HTTPS_REQUIRED: "网关地址必须使用 HTTPS：这一腿携带你的 API 密钥，明文 http 不放行",
    egress_guard.REFUSED_HOST: "网关地址缺少主机名",
    egress_guard.REFUSED_UNRESOLVED: "网关主机名无法解析，请检查地址",
    egress_guard.REFUSED_BLOCKED: "网关地址不允许指向回环或内网地址",
}


def _assert_public_base_url(base_url: str) -> None:
    """入口校验：主机名可解析且**全部**解析地址均为公网、且必须是 HTTPS，否则 400。

    这是给用户快反馈的一道，**不是**防线本身——防线在连接边界（`guarded_egress`，
    见 app/common/egress_guard）：那里才是"保存时公网、连接时私网"过不去的地方。
    两处共用同一份段判定与解析器，不各写一套（单一真源）。
    """
    try:
        egress_guard.assert_public_url(base_url, require_https=True)
    except egress_guard.EgressRefused as exc:
        # 不回显解析到的地址：拒绝就是拒绝，不给"内网哪台活着"的探测面。
        raise ApiError(400, _REFUSED_MESSAGES.get(exc.code, _REFUSED_MESSAGES[egress_guard.REFUSED_BLOCKED])) from None


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
    启用同样复核公网 HTTPS 地址（T3）：绕过保存口的存量脏行（直接改库、旧版留的
    http:// 明文地址）不得占用启用位——生成/恢复路径另有连接层守卫，这里先把
    "配置面"这条路堵上，用户当场拿到 400 而不是每次生成都失败。
    """
    with session_scope() as session:
        row = _require_row(session, user_id, gateway_id)
        _assert_public_base_url(row.base_url)
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

    出网守卫的拒绝（T3）走**同一句**文案，不按原因码分叉：连接时"改绑到私网被挡"与
    "解析失败/地址不可达"对用户必须是同一个结论，否则 /test 就成了"内网哪台活着"的
    探测口。运维侧要区分请读日志（守卫层与被拒绝的地址在 logger 里可见）。
    """
    if isinstance(exc, egress_guard.EgressRefused):
        return "网关地址被出网策略拒绝（不允许指向回环或内网地址）"
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

    探测与生成走**同一条受守卫通道**（`guarded_egress=True` → app/common/egress_guard：
    目的地在 socket 前逐地址校验并钉 IP、不跟随重定向、不复用保活连接、不读环境代理），
    但配**专属紧约束**：15s 读超时 + 不重试——/test 是同步端点，默认 240s×2 的黑洞地址
    会占满业务线程池（2026-10-02 终审）；另有分钟窗限频（quota_service）。
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
        guarded_egress=True,
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
      + warning——可用性优先，路由保真度让位。**但这一腿从此烧的是运营方的 key**，
      所以就地登记归属降级（V17/T2）：账记 platform 是真话，`attribution_note` 留原因，
      ERROR 级日志 + 指标计数让"静默转嫁"变成可追责事件，而不是只有模块 docstring 里
      一句"可用性优先"。
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
        llm_route.mark_route_degradation(cost_policy.NOTE_BYOK_LOOKUP_FAILED)
        metrics.record_llm_route_degradation()
        logger.error(
            "BYOK route lookup failed for user %s; this leg falls back to the PLATFORM key (%s): %s",
            user_id,
            cost_policy.NOTE_BYOK_LOOKUP_FAILED,
            exc,
        )
        return None


@contextmanager
def route_scope(user_id: int) -> Iterator[LLMRoute | None]:
    """烧 LLM 的业务腿统一入口：把该用户的生效网关压进 ContextVar，退出还原。

    注入点纪律见 app/common/llm_route.py：contextvars 不跨线程池传播，必须在
    显式持有 user_id 的 worker 入口内部进入。

    出网口径（T3）：路由生效期间 `get_llm_client()` 返回的是受守卫 client
    （llm_client._routed_client → guarded_egress=True），所以生成与恢复这两条
    不在配置面上的路径，目的地同样在连接边界被逐地址校验——不在这里重复解析 DNS，
    免得每条腿多付一次查询、还给人"这里才是防线"的错觉。
    """
    with use_route(resolve_route(user_id)):
        yield
