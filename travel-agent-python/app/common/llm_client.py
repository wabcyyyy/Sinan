"""LLM 网关客户端。

职责：
- 封装对 OpenAI 兼容聊天接口的调用，供 agent 层以最少参数发起结构化/普通对话。

实现要点：
- 进程内共享 httpx.Client（连接池 + keep-alive），避免每次请求新建 TCP；
  用户自带网关（BYOK）走**另一个**池：`_get_guarded_http_client()`，出网目的地在
  socket 前一刻受 `app.common.egress_guard` 守卫（逐地址校验 + 钉 IP + 不复用保活
  + 不读环境代理），运营方通道不受影响（口径见该模块 docstring）；
- connect/read 超时分层：连接超时短、读超时对齐 LLM_TIMEOUT / run deadline；
- LLMClient.chat 支持 system/user 消息、temperature、max_tokens、
  enable_search（联网搜索，只有百炼兼容层支持：其余通道要求它即抛错，
  防"静默丢参数→拿模型记忆冒充检索结果"，能力判据见 channel_supports_search）、
  思考开关与强度（只读 settings 的 llm_enable_thinking / llm_reasoning_effort，
  调用点不按次覆盖——单一真源，见 _apply_provider_options）与
  json_mode（response_format=json_object）；
- 通道能力（能搜不能搜、供不供某个模型）由 channel_supports_search /
  channel_serves_model 判定；require_model_served 在发请求前拦下"模型名与网关
  不匹配"的配置错（如把 qwen-vl-plus 发到 api.deepseek.com）；
- complete 是单轮 user 提示的便捷封装；get_llm_client 提供进程内单例，
  BYOK 路由（app.common.llm_route 的 ContextVar）生效时改返回该路由的专属 client；
- 通道级熔断 + main 备选链（审计 §3.1.1 / §3.2.2 / P0-2，2026-10-05 全量失败
  2 小时事故的根因模式）：连续通道级失败开冷却窗（窗内快速失败不外呼、半开探测
  恢复），状态与告警在 app/common/llm_breaker.py；主通道开窗且配了
  LLM_MAIN_FALLBACK_PROVIDER 时 main 角色解析临时切到备选，冷却窗过后切回；
- 结构化意图/草稿编辑走可配置 fast 通道（`model_registry.model_for("fast")`；默认与
  主模型同款，切档才省钱）；
- stream_chat 支持流式输出，适用于多日整段生成场景。

依赖：
- app.common.config.settings；app.common.egress_guard；httpx；外部 LLM 服务。
"""

import contextlib
import hashlib
import json
import logging
import random
import threading
import time
from collections.abc import Iterator

import httpx

from app.agent.runtime.observability import current_scene, metrics
from app.agent.runtime.run_limits import RunLimitExceeded, current_limits
from app.agent.runtime.trace import current_run_id, record_event
from app.agent.runtime.usage_store import usage_store
from app.common import egress_guard, llm_breaker, model_registry
from app.common.config import settings
from app.common.http_deadline import bounded_chunks, bounded_lines
from app.common.llm_route import LLMRoute, current_route

logger = logging.getLogger(__name__)

_http_client: httpx.Client | None = None
# 受守卫通道（BYOK/用户可控目的地）单独一个池：不与运营方通道共用保活连接。
_guarded_http_client: httpx.Client | None = None
_http_lock = threading.Lock()


def _build_timeout(read: float) -> httpx.Timeout:
    return httpx.Timeout(
        connect=settings.llm_connect_timeout,
        read=read,
        write=settings.llm_connect_timeout,
        pool=settings.llm_connect_timeout,
    )


def _get_http_client() -> httpx.Client:
    """共享连接池客户端；timeout 在单次请求上覆盖（read 可随 deadline 变化）。"""
    global _http_client
    if _http_client is None or _http_client.is_closed:
        with _http_lock:
            if _http_client is None or _http_client.is_closed:
                limits = httpx.Limits(
                    max_connections=settings.llm_pool_max_connections,
                    max_keepalive_connections=settings.llm_pool_max_keepalive,
                    keepalive_expiry=30.0,
                )
                _http_client = httpx.Client(
                    timeout=_build_timeout(settings.llm_timeout),
                    limits=limits,
                    follow_redirects=True,
                )
    return _http_client


def _get_guarded_http_client() -> httpx.Client:
    """用户自带网关（BYOK）专用通道：出网在 socket 前一刻受守卫（app.common.egress_guard）。

    与共享通道的差别都在守卫里定死，不在这里重复：目的地逐地址校验并钉到校验过的 IP、
    不复用保活连接、不跟随重定向、不读环境代理。共享通道（运营方 key）不受本函数影响
    ——它的地址来自 .env，属可信运营配置（口径见 egress_guard 模块 docstring）。
    """
    global _guarded_http_client
    if _guarded_http_client is None or _guarded_http_client.is_closed:
        with _http_lock:
            if _guarded_http_client is None or _guarded_http_client.is_closed:
                _guarded_http_client = egress_guard.guarded_client(
                    max_connections=settings.llm_pool_max_connections,
                    timeout=_build_timeout(settings.llm_timeout),
                )
    return _guarded_http_client


def close_http_client() -> None:
    """测试/进程退出：关闭共享客户端（含受守卫通道）。"""
    global _http_client, _guarded_http_client
    with _http_lock:
        if _http_client is not None and not _http_client.is_closed:
            _http_client.close()
        _http_client = None
        if _guarded_http_client is not None and not _guarded_http_client.is_closed:
            _guarded_http_client.close()
        _guarded_http_client = None


def _record_usage(
    model: str | None,
    prompt_tokens: int,
    completion_tokens: int,
    started: float,
    success: bool,
    error: str | None = None,
    *,
    provider: str | None = None,
    usage_complete: bool = False,
    record_id: int | None = None,
) -> int | None:
    """把一次调用明细写入用量库（场景由 contextvar 推断）。

    run_id 直取 trace 上下文（P1-7）：llm_calls 由此获得 run 级归因键，
    "一次 7 天生成花多少 token/钱"按 `WHERE run_id = ?` 聚合即可答；
    无 trace 上下文（如 /v1/plan-context 直调）时如实为 NULL。
    """
    # 用量记录失败不影响主流程
    with contextlib.suppress(Exception):
        limits = current_limits()
        return usage_store.record(
            scene=current_scene(),
            model=model or settings.llm_model,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            duration_ms=int((time.monotonic() - started) * 1000),
            success=success,
            error=error,
            run_id=current_run_id(),
            task_id=limits.task_id if limits else None,
            user_id=limits.user_id if limits else None,
            itinerary_id=limits.itinerary_id if limits else None,
            provider=provider,
            usage_complete=usage_complete,
            record_id=record_id,
        )
    return None


DEFAULT_SYSTEM_PROMPT = "你是一个专业的旅游行程规划助手，请用中文回答，只输出结构化结果。"

# 可重试的 HTTP 状态：限流与网关/服务端瞬时错误。4xx（除 429）是请求本身
# 的问题，重试只会放大失败，不重试。
_RETRYABLE_STATUS = {408, 409, 425, 429, 500, 502, 503, 504}
_LLM_MAX_ATTEMPTS = 2  # 含首次，即对可重试错误额外重试一次


class StreamCancelled(Exception):
    """流式生成被取消（客户端断开）。

    语义：在途 HTTP 流随即中断（退出 with 块即断开连接，网关侧停止生成），
    不属于服务故障——不重试、不计入失败指标。
    """


def _describe_error(exc: BaseException) -> str:
    """可排障的错误串：``str(exc)`` 对连接层异常（如 httpx.ConnectTimeout）是**空串**，
    只记它会让日志与 usage 库在通道级故障时只剩空消息/unknown，无法分类归因
    （2026-10-05 夜审 P0：generate 全量失败 2 小时，usage.error 全落 'unknown'）。
    统一带上异常类型名；类型名可判别 ConnectTimeout / ReadTimeout / ConnectError。
    """
    text = str(exc).strip()
    return f"{type(exc).__name__}: {text}" if text else type(exc).__name__


def _is_retryable(exc: Exception) -> bool:
    if isinstance(exc, httpx.TimeoutException):
        return True
    if isinstance(exc, httpx.TransportError):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in _RETRYABLE_STATUS
    return False


def _backoff_delay(attempt: int) -> float:
    """指数退避基数 min(2^attempt * 0.3, 2.0) 乘 ±20% 抖动。

    抖动是注释里的既有意图（L1 补充 3 落地）：同步网关的瞬时过载会被"整点
    整齐的重试波"放大，随机分量把重试时刻摊开。抽成纯函数便于单测断言区间。
    """
    base = min(2.0**attempt * 0.3, 2.0)
    return base * random.uniform(0.8, 1.2)


def _retry_sleep(attempt: int) -> None:
    # 指数退避 + 少量抖动，避免同步 LLM 网关的瞬时过载被放大。
    time.sleep(_backoff_delay(attempt))


def _is_dashscope_url(base_url: str) -> bool:
    return "dashscope" in (base_url or "").lower()


def _is_deepseek_url(base_url: str) -> bool:
    return "api.deepseek.com" in (base_url or "").lower()


def channel_supports_search(base_url: str) -> bool:
    """通道是否有联网搜索插件（百炼 ``enable_search``）。

    只有 DashScope 百炼兼容层有这个非标参数：DeepSeek 官方 API 发它直接 400，
    其余自建/第三方 OpenAI 兼容网关同样不认。**通道能力是唯一判据**——参数被
    静默丢掉时模型会拿自己的记忆冒充"检索结果"，比搜不到更糟。
    """
    return _is_dashscope_url(base_url)


def channel_serves_model(base_url: str, model: str) -> bool:
    """``model`` 是否可能由 ``base_url`` 这个通道提供（已知通道按各自服务面判）。

    - DeepSeek 官方只服务 ``deepseek-*``：把 qwen-vl-plus / qwen-max 发过去必然 404，
      白等一次往返，还被上层记成"上游不可用"（掩盖了配置错误）；
    - 百炼兼容层服务 qwen-* 等，不服务 deepseek-*；
    - 其余（BYOK / 自建网关）不做判断——用户自己的网关挂什么模型，自己清楚。
    """
    name = str(model or "").strip().lower()
    if not name:
        return False
    if _is_deepseek_url(base_url):
        return name.startswith("deepseek")
    if _is_dashscope_url(base_url):
        return not name.startswith("deepseek")
    return True


def require_model_served(base_url: str, model: str) -> None:
    """通道与模型名不匹配时立刻响亮报错（发请求之前）。

    配置错误要在本地炸掉并说清怎么修，不要伪装成"上游 502"——
    调用方据此走如实降级（业务面 502 带原因，agent 面走 fallback）。
    """
    if not channel_serves_model(base_url, model):
        raise ValueError(
            f"当前 LLM 通道（{base_url}）不提供模型 {model}："
            "请在 .env 把该模型所属网关配到对应的通道，或在设置页配置自带网关（BYOK）"
        )


def _apply_provider_options(
    payload: dict,
    base_url: str,
    enable_search: bool,
) -> None:
    """按网关供应商规范填充扩展参数（搜索插件、思考模式与思考强度）。

    - DashScope 百炼：顶层 enable_search（联网插件）与 enable_thinking（思考模式开关）；
    - DeepSeek 官方 API：thinking: {"type": "enabled"|"disabled"} 与 reasoning_effort（思考强度），
      不支持非标的 enable_search / enable_thinking（发送会触发 400 Bad Request）。

    要求联网搜索而通道没有该插件时**抛错**而不是把参数丢掉：丢掉之后模型会凭记忆
    作答，调用方却以为拿到了联网结果（来源还标着 web.search）——宁可响亮失败。
    入口层（web_search / pricing）已先判 `channel_supports_search`，正常路径到不了这里。

    思考参数的三层优先级（2026-10-04 修）：**显式关闭 > 思考强度 > 显式开启 > 不下发**。
    此前 `.env` 里 `LLM_REASONING_EFFORT=`（留空）被 pydantic-settings 解析成 `""` 而不是
    None，于是 `LLM_ENABLE_THINKING=false` 永远进不了"关闭"分支、空串还会被当成有效强度
    发给 DeepSeek（400）。所以这里统一把空串归一到"没配"。

    思考配置只读 settings（2026-10-12 拍板：正式生成以 low 为目标）：此前结构生成/修复
    按次传 enable_thinking=False，全局 low 对生成实际不生效，换环境键看不出差别。
    调用点不再提供按次覆盖，换档只改 .env。
    """
    effort = (settings.llm_reasoning_effort or "").strip() or None
    thinking = settings.llm_enable_thinking
    if enable_search and not _is_dashscope_url(base_url):
        raise ValueError(
            f"通道 {base_url} 不支持联网搜索（enable_search）：该插件只有 DashScope 百炼兼容层提供，"
            "DeepSeek 官方 API 发这个参数会被 400 拒绝；需要联网请把该通道指向百炼兼容层"
        )
    if _is_deepseek_url(base_url):
        if thinking is False:
            # 显式关闭是硬指令：此时 reasoning_effort 无意义，一并省掉
            payload["thinking"] = {"type": "disabled"}
        elif effort is not None:
            payload["reasoning_effort"] = effort
            payload["thinking"] = {"type": "enabled"}
        elif thinking is True:
            payload["thinking"] = {"type": "enabled"}
    else:
        # 走到这里 enable_search=True 必然已确认是百炼兼容层（上面拦掉了其余通道）
        if enable_search:
            payload["enable_search"] = True
        if thinking is not None:
            payload["enable_thinking"] = thinking
        if effort is not None and thinking is not False:
            payload["reasoning_effort"] = effort


def _resolve_main_binding() -> model_registry.RoleBinding:
    """main 角色最终落点（P0-2 故障转移）：主通道熔断开窗且配了备选时切到备选。

    切换点在 llm_client 而不是 model_registry：熔断状态在 llm 侧（llm_breaker），
    registry 只产出数据、不反向 import（见其 docstring 的依赖方向约定）。备选
    未配置（LLM_MAIN_FALLBACK_PROVIDER 默认空）或主通道健康时，返回值与
    model_registry.binding("main") 完全一致——默认部署零行为差异。
    BYOK 路由生效时**不切**（model_registry 的「BYOK 优先」口径）：用户自带网关
    就是这个用户的通道，熔断也不得静默改烧运营方备选 key（llm_gateway_service
    「失败响亮、不静默回退」拍板）——开窗时由 llm_breaker 快速失败如实上报。
    """
    main = model_registry.binding("main")
    if current_route() is not None:
        return main
    fallback = model_registry.fallback_binding()
    if fallback is not None and llm_breaker.is_open(main.base_url):
        return fallback
    return main


class LLMClient:
    def __init__(
        self,
        *,
        base_url: str | None = None,
        api_key: str | None = None,
        model: str | None = None,
        timeout: float | None = None,
        max_attempts: int | None = None,
        follow_redirects: bool | None = None,
        guarded_egress: bool = False,
        capabilities: frozenset[str] | None = None,
    ) -> None:
        # 显式传入即覆盖（角色 client / BYOK 路由走这里），缺省 = main 角色（见 model_registry）；
        # main 的解析带熔断备选链（P0-2）：主通道开窗且配了 LLM_MAIN_FALLBACK_PROVIDER
        # 时临时切到备选 provider，冷却窗过后自然切回（未配置/主通道健康时与
        # model_registry.binding("main") 完全一致）。
        if base_url is None or api_key is None or model is None:
            main = _resolve_main_binding()
            base_url = main.base_url if base_url is None else base_url
            api_key = main.provider.api_key if api_key is None else api_key
            model = main.model if model is None else model
            capabilities = main.capabilities() if capabilities is None else capabilities
        self._base_url = base_url
        self._api_key = api_key
        self._model = model
        self._capabilities = (
            capabilities if capabilities is not None else model_registry._capabilities("byok", self._base_url)
        )
        self._timeout = settings.llm_timeout if timeout is None else timeout
        self._max_attempts = _LLM_MAX_ATTEMPTS if max_attempts is None else max(1, max_attempts)
        # BYOK 网关地址不可信：30x 重定向可把携带用户密钥的 POST 引向内网地址
        # （SSRF 面），路由专属 client 与连通性探测一律不跟随（2026-10-02 终审）；
        # None = 沿用共享 httpx client 的默认（跟随）。
        self._follow_redirects = True if follow_redirects is None else follow_redirects
        # T3 守卫开关：True = 目的地是用户填写的地址（BYOK），这一腿走受守卫的出网通道
        # ——目的地在 socket 前一刻逐地址校验并钉到校验过的公网 IP（app.common.egress_guard）；
        # False = 运营方 .env 里的固定通道（可信配置，不套同一拒绝规则，见该模块口径）。
        self._guarded_egress = guarded_egress

    def _http(self) -> httpx.Client:
        """本次调用使用的 httpx 通道（受守卫 / 共享）。测试打桩点：两个 getter 都是模块级函数。"""
        return _get_guarded_http_client() if self._guarded_egress else _get_http_client()

    @property
    def base_url(self) -> str:
        """本 client 实际打到的网关地址（通道能力判据的唯一来源，见 channel_serves_model）。"""
        return self._base_url

    @property
    def model(self) -> str:
        return self._model

    def json_response_format(self, name: str, schema: dict) -> dict:
        if "json_schema" in self._capabilities:
            return {"type": "json_schema", "json_schema": {"name": name, "strict": True, "schema": schema}}
        return {"type": "json_object"}

    def chat_response(
        self,
        messages: list[dict],
        temperature: float = 0.7,
        max_tokens: int = 2048,
        enable_search: bool = False,
        model: str | None = None,
        json_mode: bool = False,
        response_format: dict | None = None,
        tools: list[dict] | None = None,
        tool_choice: str | dict | None = None,
    ) -> dict:
        url = self._base_url.rstrip("/") + "/chat/completions"
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": model or self._model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        _apply_provider_options(payload, self._base_url, enable_search)
        if response_format is not None:
            # response_format 原样透传；档位（json_schema 强约束 vs json_object）由调用点
            # 按 model_registry 能力位选（DeepSeek 官方 2026-10-05 起拒 json_schema，R2-F1）。
            payload["response_format"] = response_format
        elif json_mode:
            payload["response_format"] = {"type": "json_object"}
        if tools:
            payload["tools"] = tools
        if tool_choice is not None:
            payload["tool_choice"] = tool_choice
        limits = current_limits()
        try:
            # 通道熔断（P0-2）：主网关宕机时不再每个请求都全额支付超时重试
            # （2026-10-05 全量失败 2 小时事故的根因模式）；开窗中快速失败，
            # 成败在下方收口计数（llm_breaker）。
            llm_breaker.check(self._base_url)
            if limits:
                limits.check("llm")
            timeout = self._timeout
            if limits and limits.deadline_seconds > 0:
                remaining = limits.remaining()
                timeout = min(timeout, max(0.1, remaining))
            for attempt in range(self._max_attempts):
                try:
                    # 每次真实 HTTP 调用都消耗 run 预算，重试同样受限额约束。
                    if limits and attempt > 0:
                        limits.check("llm")
                    attempt_started = time.monotonic()
                    estimated = len(json.dumps(payload, ensure_ascii=False).encode("utf-8")) + 256
                    reservation = limits.reserve_llm(estimated, max_tokens) if limits else None
                    timeout = min(self._timeout, max(0.001, limits.remaining())) if limits else self._timeout
                    usage_id = _record_usage(
                        model or self._model,
                        estimated,
                        max_tokens,
                        attempt_started,
                        False,
                        "inflight",
                        provider=self._base_url,
                    )
                    with self._http().stream(
                        "POST",
                        url,
                        json=payload,
                        headers=headers,
                        timeout=_build_timeout(timeout),
                        follow_redirects=self._follow_redirects,
                    ) as resp:
                        resp.raise_for_status()
                        body = json.loads(b"".join(bounded_chunks(resp, limits)))
                    usage = body.get("usage")
                    if limits and reservation:
                        limits.settle_llm(reservation, usage)
                    actual = usage or {"prompt_tokens": estimated, "completion_tokens": max_tokens}
                    _record_usage(
                        model or self._model,
                        actual.get("prompt_tokens", estimated),
                        actual.get("completion_tokens", max_tokens),
                        attempt_started,
                        True,
                        provider=self._base_url,
                        usage_complete=bool(usage and "prompt_tokens" in usage and "completion_tokens" in usage),
                        record_id=usage_id,
                    )
                    if limits:
                        limits.check("deadline")
                    break
                except RunLimitExceeded:
                    raise
                except Exception as exc:
                    if limits and reservation:
                        limits.settle_llm(reservation, None)
                    _record_usage(
                        model or self._model,
                        estimated,
                        max_tokens,
                        attempt_started,
                        False,
                        _describe_error(exc),
                        provider=self._base_url,
                        record_id=usage_id,
                    )
                    # 瞬时错误（超时/429/5xx）指数退避后重试一次；其余直接上抛。
                    if attempt + 1 >= self._max_attempts or not _is_retryable(exc):
                        raise
                    # record_event 只进 trace 上下文——逐日兜底路径上上下文缺失时
                    # 重试在服务端日志完全隐形（2026-10-01 金路径 E2E 排障 20 分钟
                    # 才靠 py-spy 定位），这里同步落一条不依赖 trace 的日志。
                    logger.warning("llm retryable failure (attempt %s): %s", attempt + 1, _describe_error(exc))
                    record_event(
                        "llm",
                        "llm.retry",
                        status="error",
                        error=_describe_error(exc),
                        metadata={"attempt": attempt + 1},
                    )
                    _retry_sleep(attempt)
            # 成功收到响应：通道健康——清零连续失败计数；若此前开窗 = 半开探测通过，关窗
            llm_breaker.note_success(self._base_url)
        except RunLimitExceeded as exc:
            record_event("llm", "llm.request", status="error", error=str(exc), metadata={"budget_exhausted": True})
            raise
        except Exception as exc:
            if _is_retryable(exc):
                # 只有连接层错误 / 可重试白名单状态计入通道熔断；业务 4xx、内容审查
                # （空 choices）是"这条请求的问题"，不计数（llm_breaker 口径）。
                llm_breaker.note_failure(self._base_url, retryable=True)
            raise
        usage = body.get("usage") or {}
        # 全局 token 统计收口：无论是否处于 trace 上下文，每次真实调用都上报。
        metrics.record_llm_call(int(usage.get("prompt_tokens") or 0), int(usage.get("completion_tokens") or 0))

        choices = body.get("choices") or []
        choice = choices[0] if choices else {}
        record_event(
            "llm",
            "llm.request",
            metadata={
                "model": model or self._model,
                "prompt_tokens": usage.get("prompt_tokens"),
                "completion_tokens": usage.get("completion_tokens"),
                "finish_reason": choice.get("finish_reason"),
                "content_chars": len((choice.get("message") or {}).get("content") or ""),
                "reasoning_tokens": (usage.get("completion_tokens_details") or {}).get("reasoning_tokens"),
            },
        )
        if not choices:
            # 内容审查/风控等场景网关会返回空 choices；抛语义化 ValueError
            # 而不是 IndexError，上层降级链按普通失败处理。
            # AICHAIN-4：顶层 error.code/message 才是网关给的真因（如 content_filter），
            # 顶层 finish_reason 在空 choices 时恒为 None——读它只会误导排障。
            error = body.get("error") if isinstance(body.get("error"), dict) else {}
            reason = error.get("code") or error.get("message") or body.get("finish_reason")
            raise ValueError(f"LLM 未返回任何候选（{str(reason)[:200] if reason else '原因未随响应给出'}）")
        return {"message": choices[0]["message"], "usage": usage, "finish_reason": choices[0].get("finish_reason")}

    def stream_chat_deltas(
        self,
        messages: list[dict],
        temperature: float = 0.7,
        max_tokens: int = 2048,
        enable_search: bool = False,
        model: str | None = None,
        json_mode: bool = False,
        response_format: dict | None = None,
        cancel: threading.Event | None = None,
    ) -> Iterator[str]:
        """流式调用 LLM，逐段 yield 内容增量（不做重试；异常在迭代时抛出）。

        适用于整段多日生成的「边流边解析」场景：调用方增量喂给 JSON 解析器，
        每解析出一个完整对象即可开始处理，无需等全部 token 生成完毕。
        成功结束时在本生成器内完成一次用量上报（与 stream_chat 成功路径一致）。

        cancel：客户端断开的取消信号（可选）。置位后：请求前直接拒绝发起；
        请求中在每行到达时检查并抛 StreamCancelled——退出 with 块即断开在途
        HTTP 流。取消时 token 用量未知，不写 usage 行（避免把取消误计为失败），
        只落一条 cancelled 轨迹事件。
        """
        url = self._base_url.rstrip("/") + "/chat/completions"
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": model or self._model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": True,
        }
        _apply_provider_options(payload, self._base_url, enable_search)
        if response_format is not None:
            payload["response_format"] = response_format
        elif json_mode:
            payload["response_format"] = {"type": "json_object"}
        # 让网关在最后一个分片返回 usage，用于 token 统计。
        payload["stream_options"] = {"include_usage": True}

        if cancel is not None and cancel.is_set():
            # 已取消：连请求都不发起（调用方已在取消态）
            raise StreamCancelled("客户端已断开，未发起 LLM 请求")

        # 通道熔断（P0-2）：流式路径同样吃通道级故障的全额超时，开窗中快速失败不外呼。
        llm_breaker.check(self._base_url)

        limits = current_limits()
        if limits:
            limits.check("llm")
        timeout = self._timeout
        if limits and limits.deadline_seconds > 0:
            remaining = limits.remaining()
            timeout = min(timeout, max(0.1, remaining))

        started = time.monotonic()
        estimated = len(json.dumps(payload, ensure_ascii=False).encode("utf-8")) + 256
        reservation = limits.reserve_llm(estimated, max_tokens) if limits else None
        usage_id = _record_usage(
            model or self._model, estimated, max_tokens, started, False, "inflight", provider=self._base_url
        )
        expiry_timer: threading.Timer | None = None
        stream_usage: dict = {}
        finish_reason = None
        content_length = 0
        try:
            with self._http().stream(
                "POST",
                url,
                json=payload,
                headers=headers,
                timeout=_build_timeout(timeout),
                follow_redirects=self._follow_redirects,
            ) as resp:
                resp.raise_for_status()
                if limits:
                    expiry_timer = threading.Timer(max(0.001, limits.remaining()), resp.close)
                    expiry_timer.daemon = True
                    expiry_timer.start()
                for line in bounded_lines(resp, limits):
                    if limits:
                        limits.check("deadline")
                    if cancel is not None and cancel.is_set():
                        # 深度取消：抛异常退出 with 块即断开在途 HTTP 流（网关侧停止生成）。
                        # 用量分片未到（token 未知）故不写 usage 行；轨迹事件供成本排查。
                        record_event(
                            "llm",
                            "llm.stream_request",
                            status="cancelled",
                            metadata={
                                "model": model or self._model,
                                "content_length": content_length,
                                "duration_ms": int((time.monotonic() - started) * 1000),
                            },
                        )
                        raise StreamCancelled("客户端断开，LLM 流已中断")
                    if not line or not line.startswith("data: "):
                        continue
                    data_str = line[6:]
                    if data_str.strip() == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data_str)
                    except json.JSONDecodeError:
                        continue
                    if chunk.get("usage"):
                        stream_usage = chunk["usage"]
                    # usage-only 末分片的 choices 为空列表，注意防空。
                    first = chunk["choices"][0] if chunk.get("choices") else {}
                    finish_reason = first.get("finish_reason") or finish_reason
                    delta = first.get("delta", {})
                    content = delta.get("content")
                    if content:
                        content_length += len(content)
                        yield content
        except StreamCancelled:
            raise
        except Exception as exc:
            if _is_retryable(exc):
                llm_breaker.note_failure(self._base_url, retryable=True)
            raise
        finally:
            if expiry_timer is not None:
                expiry_timer.cancel()
            if limits and reservation:
                limits.settle_llm(reservation, stream_usage or None)
            _record_usage(
                model or self._model,
                stream_usage.get("prompt_tokens", estimated),
                stream_usage.get("completion_tokens", max_tokens),
                started,
                False,
                "stream-ended",
                provider=self._base_url,
                usage_complete=bool(stream_usage),
                record_id=usage_id,
            )
        # 整段收流成功：通道健康——半开探测通过则关窗并发恢复事件
        llm_breaker.note_success(self._base_url)
        if expiry_timer is not None:
            expiry_timer.cancel()
        if limits:
            limits.check("deadline")
        prompt = int(stream_usage.get("prompt_tokens") or 0)
        completion = int(stream_usage.get("completion_tokens") or 0)
        metrics.record_llm_call(prompt, completion)
        _record_usage(
            model or self._model,
            prompt if stream_usage else estimated,
            completion if stream_usage else max_tokens,
            started,
            True,
            provider=self._base_url,
            usage_complete=bool(stream_usage),
            record_id=usage_id,
        )

        record_event(
            "llm",
            "llm.stream_request",
            metadata={
                "model": model or self._model,
                "content_length": content_length,
                "prompt_tokens": prompt,
                "completion_tokens": completion,
                # 与阻塞式 llm.request 同口径：思考占 completion 预算，截断要能从轨迹看出来
                "finish_reason": finish_reason,
                "reasoning_tokens": (stream_usage.get("completion_tokens_details") or {}).get("reasoning_tokens"),
            },
        )

    def stream_chat(
        self,
        messages: list[dict],
        temperature: float = 0.7,
        max_tokens: int = 2048,
        enable_search: bool = False,
        model: str | None = None,
        json_mode: bool = False,
        response_format: dict | None = None,
    ) -> str:
        """流式调用 LLM，返回完整响应文本。适用于多日整段生成等长响应场景。"""
        content_parts: list[str] = []
        try:
            for delta in self.stream_chat_deltas(
                messages,
                temperature=temperature,
                max_tokens=max_tokens,
                enable_search=enable_search,
                model=model,
                json_mode=json_mode,
                response_format=response_format,
            ):
                content_parts.append(delta)
        except RunLimitExceeded as exc:
            record_event(
                "llm", "llm.stream_request", status="error", error=str(exc), metadata={"budget_exhausted": True}
            )
            raise
        return "".join(content_parts)

    def chat(
        self,
        messages: list[dict],
        temperature: float = 0.7,
        max_tokens: int = 2048,
        enable_search: bool = False,
        model: str | None = None,
        json_mode: bool = False,
        response_format: dict | None = None,
    ) -> str:
        response = self.chat_response(
            messages,
            temperature=temperature,
            max_tokens=max_tokens,
            enable_search=enable_search,
            model=model,
            json_mode=json_mode,
            response_format=response_format,
        )
        return str(response["message"].get("content") or "")

    def complete(
        self,
        user_prompt: str,
        system_prompt: str = DEFAULT_SYSTEM_PROMPT,
        temperature: float = 0.7,
        max_tokens: int = 2048,
        enable_search: bool = False,
        model: str | None = None,
        json_mode: bool = False,
        response_format: dict | None = None,
    ) -> str:
        return self.chat(
            [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=temperature,
            max_tokens=max_tokens,
            enable_search=enable_search,
            model=model,
            json_mode=json_mode,
            response_format=response_format,
        )

    def stream_complete(
        self,
        user_prompt: str,
        system_prompt: str = DEFAULT_SYSTEM_PROMPT,
        temperature: float = 0.7,
        max_tokens: int = 2048,
        enable_search: bool = False,
        model: str | None = None,
        json_mode: bool = False,
        response_format: dict | None = None,
    ) -> str:
        """流式单轮调用的便捷封装。"""
        return self.stream_chat(
            [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=temperature,
            max_tokens=max_tokens,
            enable_search=enable_search,
            model=model,
            json_mode=json_mode,
            response_format=response_format,
        )


_llm_client: LLMClient | None = None
# 单例按 (provider, base_url, model) 记键（P0-2）：main 角色熔断切到备选 / 冷却后
# 切回时重建单例；键不变（默认部署：无备选、通道健康）时与旧实现一样只构造一次、
# 恒同一实例——既有"单例身份"契约（test_llm_route）不受影响。
_llm_client_key: tuple[str, str, str, str] | None = None

# BYOK 路由专属 client 缓存：键 = (base_url, model, sha256(api_key)[:16])——api_key 只以
# 摘要形式进键，缓存本身不构成第二份明文落点。容量到顶整体清空重建：BYOK 是低频
# 配置面（单用户至多一条启用），LRU 精细化没有收益；LLMClient 不持有连接（共用
# _get_guarded_http_client 的受守卫池），清空无资源泄漏。
_route_clients: dict[tuple[str, str, str], LLMClient] = {}
_route_clients_lock = threading.Lock()
_ROUTE_CLIENT_CAPACITY = 8


def _routed_client(route: LLMRoute) -> LLMClient:
    digest = hashlib.sha256(route.api_key.encode("utf-8")).hexdigest()[:16]
    key = (route.base_url, route.model, digest)
    with _route_clients_lock:
        client = _route_clients.get(key)
        if client is None:
            if len(_route_clients) >= _ROUTE_CLIENT_CAPACITY:
                _route_clients.clear()
            # 用户网关不可信，两条防线各管一段：
            # - follow_redirects=False：30x 不得把携带用户密钥的 POST 引向别处（2026-10-02 终审）；
            # - guarded_egress=True：目的地在 socket 前一刻逐地址校验并钉到校验过的公网 IP
            #   ——保存时公网、连接时改绑私网也过不去（T3，app.common.egress_guard）。
            client = LLMClient(
                base_url=route.base_url,
                api_key=route.api_key,
                model=route.model,
                follow_redirects=False,
                guarded_egress=True,
            )
            _route_clients[key] = client
        return client


def get_llm_client(role: str = "main") -> LLMClient:
    """Resolve the provider, model and capabilities once for this call."""
    route = current_route()
    if route is not None:
        return _routed_client(route)
    if role not in model_registry.ROLES:
        raise ValueError(f"未知模型角色: {role}")
    raw = getattr(settings, f"llm_role_{role}").strip()
    bound = _resolve_main_binding() if role == "main" or not raw else model_registry.binding(role)
    global _llm_client, _llm_client_key
    key = (
        bound.provider.name,
        bound.base_url,
        bound.model,
        hashlib.sha256(bound.provider.api_key.encode()).hexdigest(),
    )
    if role == "main" or not raw:
        if _llm_client is None or _llm_client_key != key:
            _llm_client = LLMClient(
                base_url=bound.base_url,
                api_key=bound.provider.api_key,
                model=bound.model,
                capabilities=bound.capabilities(),
            )
            _llm_client_key = key
        return _llm_client
    return LLMClient(
        base_url=bound.base_url, api_key=bound.provider.api_key, model=bound.model, capabilities=bound.capabilities()
    )


def get_judge_client() -> LLMClient:
    """judge 专用 client（PR-7 / D7）：跑 LLM_ROLE_JUDGE（留空 = 复用 main）。"""
    client = get_llm_client("judge")
    # 模型必须真由该通道提供：判分模型与网关不匹配时（如 qwen-max 发到 api.deepseek.com）
    # 只会吃一个 404，被读成"judge 不可用"。配置错要在发请求前响亮报出。
    require_model_served(client.base_url, client.model)
    return client
