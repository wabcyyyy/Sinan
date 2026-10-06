"""LLM 网关客户端。

职责：
- 封装对 OpenAI 兼容聊天接口的调用，供 agent 层以最少参数发起结构化/普通对话。

实现要点：
- 进程内共享 httpx.Client（连接池 + keep-alive），避免每次请求新建 TCP；
- connect/read 超时分层：连接超时短、读超时对齐 LLM_TIMEOUT / run deadline；
- LLMClient.chat 支持 system/user 消息、temperature、max_tokens、
  enable_search（联网搜索，只有百炼兼容层支持：其余通道要求它即抛错，
  防"静默丢参数→拿模型记忆冒充检索结果"，能力判据见 channel_supports_search）、
  enable_thinking（思考开关，llm_enable_thinking 显式配置时才发送）与
  json_mode（response_format=json_object）；
- 通道能力（能搜不能搜、供不供某个模型）由 channel_supports_search /
  channel_serves_model 判定；require_model_served 在发请求前拦下"模型名与网关
  不匹配"的配置错（如把 qwen-vl-plus 发到 api.deepseek.com）；
- complete 是单轮 user 提示的便捷封装；get_llm_client 提供进程内单例，
  BYOK 路由（app.common.llm_route 的 ContextVar）生效时改返回该路由的专属 client；
- 结构化意图/草稿编辑走可配置 fast 通道（`model_registry.model_for("fast")`；默认与
  主模型同款，切档才省钱）；
- stream_chat 支持流式输出，适用于多日整段生成场景。

依赖：
- app.common.config.settings；httpx；外部 LLM 服务。
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
from app.common import model_registry
from app.common.config import settings
from app.common.llm_route import LLMRoute, current_route

logger = logging.getLogger(__name__)

_http_client: httpx.Client | None = None
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


def close_http_client() -> None:
    """测试/进程退出：关闭共享客户端。"""
    global _http_client
    with _http_lock:
        if _http_client is not None and not _http_client.is_closed:
            _http_client.close()
        _http_client = None


def _record_usage(
    model: str | None,
    prompt_tokens: int,
    completion_tokens: int,
    started: float,
    success: bool,
    error: str | None = None,
) -> None:
    """把一次调用明细写入用量库（场景由 contextvar 推断）。

    run_id 直取 trace 上下文（P1-7）：llm_calls 由此获得 run 级归因键，
    "一次 7 天生成花多少 token/钱"按 `WHERE run_id = ?` 聚合即可答；
    无 trace 上下文（如 /v1/plan-context 直调）时如实为 NULL。
    """
    # 用量记录失败不影响主流程
    with contextlib.suppress(Exception):
        usage_store.record(
            scene=current_scene(),
            model=model or settings.llm_model,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            duration_ms=int((time.monotonic() - started) * 1000),
            success=success,
            error=error,
            run_id=current_run_id(),
        )


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
    reasoning_effort: str | None = None,
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
    """
    effort = (reasoning_effort or settings.llm_reasoning_effort or "").strip() or None
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
    ) -> None:
        # 显式传入即覆盖（角色 client / BYOK 路由走这里），缺省 = main 角色（见 model_registry）
        main = model_registry.binding("main")
        self._base_url = base_url or main.base_url
        self._api_key = api_key or main.provider.api_key
        self._model = model or main.model
        self._timeout = settings.llm_timeout if timeout is None else timeout
        self._max_attempts = _LLM_MAX_ATTEMPTS if max_attempts is None else max(1, max_attempts)
        # BYOK 网关地址不可信：30x 重定向可把携带用户密钥的 POST 引向内网地址
        # （SSRF 面），路由专属 client 与连通性探测一律不跟随（2026-10-02 终审）；
        # None = 沿用共享 httpx client 的默认（跟随）。
        self._follow_redirects = True if follow_redirects is None else follow_redirects

    @property
    def base_url(self) -> str:
        """本 client 实际打到的网关地址（通道能力判据的唯一来源，见 channel_serves_model）。"""
        return self._base_url

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
        reasoning_effort: str | None = None,
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
        _apply_provider_options(payload, self._base_url, enable_search, reasoning_effort)
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
            if limits:
                limits.check("llm")
            timeout = self._timeout
            if limits and limits.deadline_seconds > 0:
                remaining = limits.deadline_seconds - (time.monotonic() - limits.started_at)
                timeout = min(timeout, max(0.1, remaining))
            started = time.monotonic()
            for attempt in range(self._max_attempts):
                try:
                    # 每次真实 HTTP 调用都消耗 run 预算，重试同样受限额约束。
                    if limits and attempt > 0:
                        limits.check("llm")
                    resp = _get_http_client().post(
                        url,
                        json=payload,
                        headers=headers,
                        timeout=_build_timeout(timeout),
                        follow_redirects=self._follow_redirects,
                    )
                    resp.raise_for_status()
                    body = resp.json()
                    break
                except Exception as exc:
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
        except RunLimitExceeded as exc:
            record_event("llm", "llm.request", status="error", error=str(exc), metadata={"budget_exhausted": True})
            raise
        except Exception as exc:
            _record_usage(model, 0, 0, started, False, _describe_error(exc))
            raise
        usage = body.get("usage") or {}
        # 全局 token 统计收口：无论是否处于 trace 上下文，每次真实调用都上报。
        metrics.record_llm_call(int(usage.get("prompt_tokens") or 0), int(usage.get("completion_tokens") or 0))
        _record_usage(
            model, int(usage.get("prompt_tokens") or 0), int(usage.get("completion_tokens") or 0), started, True
        )
        if limits:
            try:
                limits.record_llm(usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0))
            except RunLimitExceeded as exc:
                record_event(
                    "llm",
                    "llm.request",
                    status="error",
                    error=str(exc),
                    metadata={
                        "budget_exhausted": True,
                        "prompt_tokens": usage.get("prompt_tokens"),
                        "completion_tokens": usage.get("completion_tokens"),
                    },
                )
                raise
        record_event(
            "llm",
            "llm.request",
            metadata={
                "model": model or self._model,
                "prompt_tokens": usage.get("prompt_tokens"),
                "completion_tokens": usage.get("completion_tokens"),
            },
        )
        choices = body.get("choices") or []
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
        reasoning_effort: str | None = None,
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
        _apply_provider_options(payload, self._base_url, enable_search, reasoning_effort)
        if response_format is not None:
            payload["response_format"] = response_format
        elif json_mode:
            payload["response_format"] = {"type": "json_object"}
        # 让网关在最后一个分片返回 usage，用于 token 统计。
        payload["stream_options"] = {"include_usage": True}

        if cancel is not None and cancel.is_set():
            # 已取消：连请求都不发起（调用方已在取消态）
            raise StreamCancelled("客户端已断开，未发起 LLM 请求")

        limits = current_limits()
        if limits:
            limits.check("llm")
        timeout = self._timeout
        if limits and limits.deadline_seconds > 0:
            remaining = limits.deadline_seconds - (time.monotonic() - limits.started_at)
            timeout = min(timeout, max(0.1, remaining))

        started = time.monotonic()
        stream_usage: dict = {}
        content_length = 0
        with _get_http_client().stream(
            "POST",
            url,
            json=payload,
            headers=headers,
            timeout=_build_timeout(timeout),
            follow_redirects=self._follow_redirects,
        ) as resp:
            resp.raise_for_status()
            for line in resp.iter_lines():
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
                delta = chunk.get("choices", [{}])[0].get("delta", {}) if chunk.get("choices") else {}
                content = delta.get("content")
                if content:
                    content_length += len(content)
                    yield content

        prompt = int(stream_usage.get("prompt_tokens") or 0)
        completion = int(stream_usage.get("completion_tokens") or 0)
        metrics.record_llm_call(prompt, completion)
        _record_usage(model, prompt, completion, started, True)
        # 流式与阻塞式共用同一份 RunLimits：不记这一次，max_llm_calls / max_tokens
        # 在产品主路径（逐日流式）上永远读不到消耗，额度门等于只装了非流式那半边。
        # 顺序照抄 `chat_response`：**先**记用量与账单，**后**扣额度——
        # 否则恰好触发上限的那一次调用（最贵的一次）从用量表里消失，成本报表偏低。
        if limits:
            try:
                limits.record_llm(prompt, completion)
            except RunLimitExceeded as exc:
                record_event(
                    "llm",
                    "llm.stream_request",
                    status="error",
                    error=str(exc),
                    metadata={"budget_exhausted": True, "prompt_tokens": prompt, "completion_tokens": completion},
                )
                raise
        record_event(
            "llm",
            "llm.stream_request",
            metadata={
                "model": model or self._model,
                "content_length": content_length,
                "prompt_tokens": prompt,
                "completion_tokens": completion,
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
        reasoning_effort: str | None = None,
    ) -> str:
        """流式调用 LLM，返回完整响应文本。适用于多日整段生成等长响应场景。"""
        started = time.monotonic()
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
                reasoning_effort=reasoning_effort,
            ):
                content_parts.append(delta)
        except RunLimitExceeded as exc:
            record_event(
                "llm", "llm.stream_request", status="error", error=str(exc), metadata={"budget_exhausted": True}
            )
            raise
        except Exception as exc:
            _record_usage(model, 0, 0, started, False, _describe_error(exc))
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
        reasoning_effort: str | None = None,
    ) -> str:
        response = self.chat_response(
            messages,
            temperature=temperature,
            max_tokens=max_tokens,
            enable_search=enable_search,
            model=model,
            json_mode=json_mode,
            response_format=response_format,
            reasoning_effort=reasoning_effort,
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
        reasoning_effort: str | None = None,
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
            reasoning_effort=reasoning_effort,
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
        reasoning_effort: str | None = None,
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
            reasoning_effort=reasoning_effort,
        )


_llm_client: LLMClient | None = None

# BYOK 路由专属 client 缓存：键 = (base_url, model, sha256(api_key)[:16])——api_key 只以
# 摘要形式进键，缓存本身不构成第二份明文落点。容量到顶整体清空重建：BYOK 是低频
# 配置面（单用户至多一条启用），LRU 精细化没有收益；LLMClient 不持有连接（共用
# _get_http_client 池），清空无资源泄漏。
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
            # follow_redirects=False：用户网关不可信，30x 不得把 POST 引向别处（SSRF）
            client = LLMClient(
                base_url=route.base_url, api_key=route.api_key, model=route.model, follow_redirects=False
            )
            _route_clients[key] = client
        return client


def get_llm_client() -> LLMClient:
    """进程内 client 工厂：BYOK 路由生效时返回该路由的专属 client，否则返回默认单例。

    名字与签名保持不变（agent 层 16 处调用与测试 mock 契约全靠它）；路由上下文由
    services 层的 route_scope 在 worker 入口设置（contextvars 不跨线程池传播）。
    """
    global _llm_client
    route = current_route()
    if route is not None:
        return _routed_client(route)
    if _llm_client is None:
        _llm_client = LLMClient()
    return _llm_client


def get_role_client(role: str) -> LLMClient:
    """按角色取 client（model_registry 是唯一解析点）：judge / search / stt 等专用通道。

    不用 `get_llm_client` 的单例：角色可能指向另一个 provider（例如联网搜索留在百炼
    而主模型在 DeepSeek），共用单例会把密钥与地址串味。
    """
    bound = model_registry.binding(role)
    return LLMClient(base_url=bound.base_url, api_key=bound.provider.api_key, model=bound.model)


def get_judge_client() -> LLMClient:
    """judge 专用 client（PR-7 / D7）：跑 LLM_ROLE_JUDGE（留空 = 复用 main）。"""
    bound = model_registry.binding("judge")
    client = get_role_client("judge")
    # 模型必须真由该通道提供：判分模型与网关不匹配时（如 qwen-max 发到 api.deepseek.com）
    # 只会吃一个 404，被读成"judge 不可用"。配置错要在发请求前响亮报出。
    require_model_served(client.base_url, bound.model)
    return client
