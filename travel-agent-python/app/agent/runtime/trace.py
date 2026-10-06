"""Agent 运行轨迹采集。

轨迹只记录节点/工具的名称、状态、耗时和脱敏摘要，不保存完整 Prompt、密钥或用户原文。
它既可用于离线评测，也可在 API 层接入日志/指标系统；没有开启采集时不会改变业务行为。
"""

from __future__ import annotations

import contextvars
import re
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from functools import wraps
from typing import Any

_active_recorder: contextvars.ContextVar[TraceRecorder | None] = contextvars.ContextVar(
    "active_agent_trace", default=None
)
_active_span: contextvars.ContextVar[str | None] = contextvars.ContextVar("active_agent_span", default=None)
_active_registry_tool: contextvars.ContextVar[str | None] = contextvars.ContextVar("active_registry_tool", default=None)


def _safe_id(value: str | None, *, fallback: str | None = None) -> str:
    """限制外部关联 ID，避免控制字符进入响应或日志。"""
    candidate = str(value or "").strip()
    candidate = re.sub(r"[^\x20-\x7e\u4e00-\u9fff_-]", "", candidate)
    candidate = candidate[:128]
    return candidate or fallback or uuid.uuid4().hex


# 第三方 HTTP 异常原文常含完整请求 URL（高德 Web API 的 key 查询参数、
# MCP endpoint 的 ?key=、Unsplash 的 client_id、Authorization 头）；这些文本会经
# error=str(exc) 落入持久化 trace 文件。集中脱敏覆盖所有上报路径，而不是指望
# 每个调用方自己处理（docstring 承诺"不保存密钥"此前在错误路径上并不成立）。
_SECRET_PATTERNS = (
    re.compile(r"([?&](?:key|token|secret|api_key|apikey|access_key|client_id)=)[^&\s\"']+", re.IGNORECASE),
    re.compile(r"(Bearer\s+)[A-Za-z0-9._\-]+", re.IGNORECASE),
)


def redact(text: str) -> str:
    """脱敏（跨模块 API，D6/PR-8 复用）：错误文本/录制落盘前抹掉密钥类片段。

    调用方：trace 事件落盘（error 摘要）与 cassette 录制（**录制即脱敏**）。
    """
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub(r"\1***", text)
    return text


def _summary(value: Any) -> Any:
    """生成小而安全的结构摘要，避免把 Prompt/计划全文写入轨迹。"""
    if value is None or isinstance(value, (str, int, float, bool)):
        if isinstance(value, str):
            value = redact(value)
            if len(value) > 80:
                return value[:77] + "..."
        return value
    if isinstance(value, dict):
        return {"keys": sorted(str(k) for k in value)[:30], "size": len(value)}
    if isinstance(value, (list, tuple, set)):
        values = list(value)
        if len(values) <= 20 and all(isinstance(item, (str, int, float, bool)) for item in values):
            return values
        return {"type": type(value).__name__, "size": len(value)}
    return type(value).__name__


class TraceRecorder:
    def __init__(self, run_id: str | None = None, request_id: str | None = None, action_id: str | None = None):
        self.run_id = _safe_id(run_id)
        self.request_id = _safe_id(request_id)
        self.action_id = _safe_id(action_id) if action_id else None
        self.events: list[dict[str, Any]] = []

    def record(
        self,
        kind: str,
        name: str,
        *,
        status: str = "ok",
        duration_ms: float | None = None,
        metadata: dict[str, Any] | None = None,
        error: str | None = None,
        span_id: str | None = None,
        parent_span_id: str | None = None,
        tool_call_id: str | None = None,
        action_id: str | None = None,
    ) -> None:
        event_span_id = _safe_id(span_id)
        event: dict[str, Any] = {
            "kind": kind,
            "name": name,
            "status": status,
            "run_id": self.run_id,
            "request_id": self.request_id,
            "span_id": event_span_id,
        }
        if parent_span_id:
            event["parent_span_id"] = parent_span_id
        if tool_call_id:
            event["tool_call_id"] = _safe_id(tool_call_id)
        effective_action_id = action_id or self.action_id
        if effective_action_id:
            event["action_id"] = _safe_id(effective_action_id)
        if duration_ms is not None:
            event["duration_ms"] = round(duration_ms, 2)
        if metadata:
            event["metadata"] = {str(k): _summary(v) for k, v in metadata.items()}
        if error:
            event["error"] = redact(error)[:200]
        self.events.append(event)

    def to_dict(self) -> dict[str, Any]:
        payload = {"run_id": self.run_id, "request_id": self.request_id, "events": list(self.events)}
        if self.action_id:
            payload["action_id"] = self.action_id
        return payload


@contextmanager
def trace_run(
    run_id: str | None = None, request_id: str | None = None, action_id: str | None = None
) -> Iterator[TraceRecorder]:
    recorder = TraceRecorder(run_id, request_id, action_id)
    token = _active_recorder.set(recorder)
    span_token = _active_span.set(None)
    try:
        yield recorder
    finally:
        _active_span.reset(span_token)
        _active_recorder.reset(token)


def record_event(
    kind: str,
    name: str,
    *,
    status: str = "ok",
    metadata: dict[str, Any] | None = None,
    error: str | None = None,
    tool_call_id: str | None = None,
    action_id: str | None = None,
    span_id: str | None = None,
    parent_span_id: str | None = None,
) -> None:
    # Registry 会在调用结束后写入一条规范化审计事件；忽略其 handler 内部
    # 的旧式 tool span，避免一次工具调用在指标中被计数两次。
    if kind == "tool" and _active_registry_tool.get():
        return
    recorder = _active_recorder.get()
    if recorder is not None:
        recorder.record(
            kind,
            name,
            status=status,
            metadata=metadata,
            error=error,
            span_id=span_id or uuid.uuid4().hex,
            parent_span_id=parent_span_id or _active_span.get(),
            tool_call_id=tool_call_id,
            action_id=action_id,
        )


def current_run_id() -> str | None:
    """当前 trace 上下文的 run_id；无上下文返回 None。

    供流式事件发布（event_publisher）把 Redis 进度事件与本次 Agent run
    关联：run_id 随事件 data（key=runId）下发，实现「事件 ↔ 行程 ↔ 轨迹」
    三向对账；没有 trace 上下文（如独立进程调用研究链路）时返回 None，
    发布层自动退化为不带 runId 的旧形态。
    """
    recorder = _active_recorder.get()
    return recorder.run_id if recorder is not None else None


@contextmanager
def registry_tool_call(tool_call_id: str) -> Iterator[None]:
    """标记 Registry 正在执行 handler，收口其内部旧式 tool 轨迹。"""
    token = _active_registry_tool.set(tool_call_id)
    try:
        yield
    finally:
        _active_registry_tool.reset(token)


@contextmanager
def trace_span(
    kind: str,
    name: str,
    *,
    metadata: dict[str, Any] | None = None,
    tool_call_id: str | None = None,
    action_id: str | None = None,
) -> Iterator[None]:
    started = time.perf_counter()
    recorder = _active_recorder.get()
    parent_span_id = _active_span.get()
    span_id = uuid.uuid4().hex
    span_token = _active_span.set(span_id)
    try:
        yield
    except Exception as exc:
        if recorder is not None and not (kind == "tool" and _active_registry_tool.get()):
            recorder.record(
                kind,
                name,
                status="error",
                duration_ms=(time.perf_counter() - started) * 1000,
                metadata=metadata,
                error=str(exc),
                span_id=span_id,
                parent_span_id=parent_span_id,
                tool_call_id=tool_call_id,
                action_id=action_id,
            )
        raise
    else:
        if recorder is not None and not (kind == "tool" and _active_registry_tool.get()):
            recorder.record(
                kind,
                name,
                duration_ms=(time.perf_counter() - started) * 1000,
                metadata=metadata,
                span_id=span_id,
                parent_span_id=parent_span_id,
                tool_call_id=tool_call_id,
                action_id=action_id,
            )
    finally:
        _active_span.reset(span_token)


def traced(kind: str, name: str) -> Callable:
    """给同步 LangGraph 节点或工具增加统一轨迹事件。"""

    def decorator(func: Callable) -> Callable:
        @wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            metadata = {"arg_count": len(args), "kwarg_names": sorted(kwargs)}
            with trace_span(kind, name, metadata=metadata):
                return func(*args, **kwargs)

        return wrapper

    return decorator
