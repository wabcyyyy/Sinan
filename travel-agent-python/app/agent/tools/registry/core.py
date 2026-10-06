"""受控工具目录：机制层（G-2.6 按域拆包，登记见 catalog.py）。

工具只能通过显式注册后执行，目录同时提供 Function Calling schema、参数校验、
调用预算和脱敏审计事件。写入型工具默认不注册，避免模型或不可信工具结果
绕过业务服务直接改变行程状态。全部工具面（含 research 三域检索）统一从
本注册表派发，禁止 getattr 字符串派发散落各处。

handler 约定：经 `from app.agent.tools import impl as tools` 后**调用期**读模块属性
（`tools.search_attractions(...)`），保证 mock.patch.object(tools, ...) 零
修改生效；不得在注册期绑定函数对象。

本模块只含机制（ToolSpec / 预算 / 校验 / ToolRegistry）；具体登记了哪些工具
见 `catalog.py`（导入它即完成登记）。
"""

from __future__ import annotations

import contextvars
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeoutError
from dataclasses import dataclass, field
from typing import Any

from app.agent.runtime.run_limits import current_limits
from app.agent.runtime.tool_budget import tool_budget_scope
from app.agent.runtime.trace import record_event, registry_tool_call, trace_span
from app.common.config import settings

#: retry_policy 的安全顶（PR-11）：登记值超过它按它钳制。重试放大外呼量，
#: 沙箱侧必须有一道不依赖登记自觉的上限。
_MAX_HANDLER_RETRIES = 2


class ToolInvocationError(ValueError):
    """工具不存在、参数不合法、未确认或超过预算。"""


@dataclass(frozen=True)
class RunContext:
    """工具门控的评估上下文。

    G-1.4 只建立机制（ToolSpec.when + 本上下文），消费方（addon/feature-flag
    体系）在 G-3.1 接线；当前所有 spec 的 when 均为 None，行为零变化。
    """

    name: str
    params: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ToolSpec:
    name: str
    version: str
    description: str
    parameters: dict[str, Any]
    read_only: bool
    risk_level: str
    timeout_seconds: float
    max_calls: int
    retry_policy: dict[str, Any]
    requires_confirmation: bool
    idempotent: bool
    handler: Callable[..., Any]
    # 运行时门控钩子：返回 False 时该工具按「未注册」处理（隐藏而非报错）。
    # None 表示无条件可用。评估入参见 RunContext。
    when: Callable[[RunContext], bool] | None = None

    def function_schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


def _type_matches(value: Any, expected: str) -> bool:
    return {
        "string": isinstance(value, str),
        "integer": isinstance(value, int) and not isinstance(value, bool),
        "number": isinstance(value, (int, float)) and not isinstance(value, bool),
        "boolean": isinstance(value, bool),
        "array": isinstance(value, list),
        "object": isinstance(value, dict),
    }.get(expected, True)


def _effective_retries(spec: ToolSpec) -> int:
    """retry_policy → 实际重试次数：只上**幂等**工具（重试只读 GET 的纪律），
    并按沙箱安全顶钳制。"""
    if not spec.idempotent:
        return 0
    declared = int(spec.retry_policy.get("max_retries") or 0)
    return max(0, min(declared, _MAX_HANDLER_RETRIES))


def _run_with_timeout(spec: ToolSpec, params: dict[str, Any], context: contextvars.Context) -> Any:
    """在专用单线程池里执行 handler，把 spec.timeout_seconds 从纸面元数据变成
    强制约束（PR-11：此前 registry 只读 max_calls）。

    Python 线程无法强杀：超时后调用方立刻拿到 ToolInvocationError，池线程仍在
    后台跑到自然结束（解释器退出前会被 join）——语义是"调用方不再等"，不是
    "计算被终止"。contextvars 随 copy_context 带入 worker：trace recorder、
    span 与预算上下文都随行，handler 内的 record_event 仍挂到当前 run。工具
    调用低频（每次 run ≤ tool_max_calls），每次建池的开销可忽略，换来与被超时
    handler 的完全隔离。
    """
    pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix=f"tool-{spec.name}")
    try:
        future = pool.submit(context.run, lambda: spec.handler(**params))
        try:
            return future.result(timeout=max(spec.timeout_seconds, 0.05))
        except FuturesTimeoutError as exc:
            # 放弃等待（不 shutdown(wait=True)——那会把超时重新变成阻塞）。
            pool.shutdown(wait=False)
            raise ToolInvocationError(f"工具 {spec.name} 超时（>{spec.timeout_seconds}s）") from exc
    finally:
        pool.shutdown(wait=False)


def _validate_parameters(spec: ToolSpec, params: dict[str, Any]) -> None:
    if not isinstance(params, dict):
        raise ToolInvocationError(f"工具 {spec.name} 参数必须是对象")
    schema = spec.parameters or {}
    required = schema.get("required", [])
    missing = [name for name in required if name not in params]
    if missing:
        raise ToolInvocationError(f"工具 {spec.name} 缺少参数：{', '.join(missing)}")
    properties = schema.get("properties", {})
    if schema.get("additionalProperties") is False:
        unknown = sorted(set(params) - set(properties))
        if unknown:
            raise ToolInvocationError(f"工具 {spec.name} 存在未知参数：{', '.join(unknown)}")
    for name, value in params.items():
        definition = properties.get(name) or {}
        if value is not None and definition.get("type") and not _type_matches(value, definition["type"]):
            raise ToolInvocationError(f"工具 {spec.name} 参数 {name} 类型不正确")


class ToolRegistry:
    def __init__(self) -> None:
        self._specs: dict[str, ToolSpec] = {}

    def register(self, spec: ToolSpec) -> ToolSpec:
        if spec.name in self._specs:
            raise ValueError(f"工具已注册：{spec.name}")
        self._specs[spec.name] = spec
        return spec

    def get(self, name: str) -> ToolSpec:
        try:
            return self._specs[name]
        except KeyError as exc:
            raise ToolInvocationError(f"未注册工具：{name}") from exc

    def list_specs(self) -> list[ToolSpec]:
        """当前可用的工具（when 门控关闭的不列——工具面随能力收起，G-3.1）。"""
        return [spec for spec in self._specs.values() if self._visible(spec)]

    @staticmethod
    def _visible(spec: ToolSpec) -> bool:
        return spec.when is None or spec.when(RunContext(name=spec.name))

    def function_schemas(self) -> list[dict[str, Any]]:
        """Function Calling schema：同样只列 when 门控放行的工具（LLM 不见被关能力）。"""
        return [spec.function_schema() for spec in self.list_specs()]

    def public_specs(self) -> list[dict[str, Any]]:
        """供内部审计/调试使用，不暴露 Python handler。"""
        return [
            {
                "name": spec.name,
                "version": spec.version,
                "description": spec.description,
                "parameters": spec.parameters,
                "read_only": spec.read_only,
                "risk_level": spec.risk_level,
                "timeout_seconds": spec.timeout_seconds,
                "max_calls": spec.max_calls,
                "retry_policy": spec.retry_policy,
                "requires_confirmation": spec.requires_confirmation,
                "idempotent": spec.idempotent,
                # MCP tool annotations 语义对齐（PR-11）：给客户端/审计面的能力
                # 提示，与 handler 强制执行的门禁相互独立。
                "annotations": {"readOnlyHint": spec.read_only, "destructiveHint": not spec.read_only},
            }
            for spec in self.list_specs()
        ]

    def invoke(
        self, name: str, params: dict[str, Any] | None = None, *, confirmed: bool = False, action_id: str | None = None
    ) -> Any:
        spec = self.get(name)
        params = params or {}
        # 门控钩子（G-1.4 机制，G-3.1 接消费方）：关闭的工具与未注册工具同话术，
        # 不暴露「存在但被禁用」；先于参数校验，被禁用即不存在。
        if spec.when is not None and not spec.when(RunContext(name=name, params=dict(params))):
            raise ToolInvocationError(f"未注册工具：{name}")
        _validate_parameters(spec, params)
        if spec.requires_confirmation and not confirmed:
            raise ToolInvocationError(f"工具 {name} 需要用户确认")

        tool_call_id = uuid.uuid4().hex
        with tool_budget_scope() as state:
            limits = current_limits()
            if limits:
                limits.check("tool")
            total = int(state["total"])
            per_tool = int(state["by_tool"].get(name, 0))
            if total >= settings.tool_max_calls or per_tool >= spec.max_calls:
                record_event(
                    "tool",
                    name,
                    status="error",
                    tool_call_id=tool_call_id,
                    action_id=action_id,
                    metadata={
                        "version": spec.version,
                        "budget_exhausted": True,
                        "total_calls": total,
                        "tool_calls": per_tool,
                    },
                    error="tool_call_budget_exhausted",
                )
                raise ToolInvocationError(f"工具调用预算已耗尽：{name}")
            state["total"] = total + 1
            state["by_tool"][name] = per_tool + 1
            metadata = {
                "version": spec.version,
                "risk_level": spec.risk_level,
                "read_only": spec.read_only,
                "idempotent": spec.idempotent,
                "timeout_seconds": spec.timeout_seconds,
                "parameters": params,
            }
            with (
                trace_span("tool", name, metadata=metadata, tool_call_id=tool_call_id, action_id=action_id),
                registry_tool_call(tool_call_id),
            ):
                return self._dispatch(spec, params, tool_call_id, action_id)

    def _dispatch(self, spec: ToolSpec, params: dict[str, Any], tool_call_id: str, action_id: str | None) -> Any:
        """handler 执行面（PR-11 沙箱接线）：timeout_seconds 强制 + retry_policy 生效。

        重试只给幂等工具（见 `_effective_retries`）；每次重试记审计事件，调用方
        （LLM/图节点）看到的仍是最后一次的异常——重试是沙箱内部的韧性，不是
        新的一次工具调用（不重复扣预算，受 `_MAX_HANDLER_RETRIES` 钳制）。
        """
        retries = _effective_retries(spec)
        context = contextvars.copy_context()
        for attempt in range(retries + 1):
            try:
                return _run_with_timeout(spec, params, context)
            except ToolInvocationError:
                raise
            except Exception as exc:
                if attempt >= retries:
                    raise
                record_event(
                    "tool",
                    spec.name,
                    status="error",
                    tool_call_id=tool_call_id,
                    action_id=action_id,
                    metadata={"retry_attempt": attempt + 1, "max_retries": retries},
                    error=str(exc),
                )
        raise AssertionError("unreachable: retry loop must return or raise")


#: 进程内唯一注册表（登记见 catalog.py；导入包即完成登记）
registry = ToolRegistry()
