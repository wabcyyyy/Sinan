import time

import pytest

from app.agent.runtime.tool_budget import begin_tool_budget, end_tool_budget
from app.agent.runtime.trace import trace_run
from app.agent.tools.registry import (
    ToolInvocationError,
    ToolRegistry,
    ToolSpec,
)
from app.common.config import settings


def _spec(
    name="demo",
    handler=lambda **params: params,
    max_calls=2,
    *,
    timeout_seconds: float = 1,
    retry_policy=None,
    idempotent=True,
):
    return ToolSpec(
        name=name,
        version="1.0",
        description="demo",
        parameters={
            "type": "object",
            "properties": {"city": {"type": "string"}},
            "required": ["city"],
            "additionalProperties": False,
        },
        read_only=True,
        risk_level="low",
        timeout_seconds=timeout_seconds,
        max_calls=max_calls,
        retry_policy=retry_policy or {"max_retries": 0},
        requires_confirmation=False,
        idempotent=idempotent,
        handler=handler,
    )


def test_registry_exposes_only_registered_schemas_and_validates_arguments():
    registry = ToolRegistry()
    registry.register(_spec())
    assert registry.function_schemas()[0]["function"]["name"] == "demo"
    assert registry.invoke("demo", {"city": "杭州"})["city"] == "杭州"
    with pytest.raises(ToolInvocationError, match="未注册"):
        registry.invoke("missing", {})
    with pytest.raises(ToolInvocationError, match="未知参数"):
        registry.invoke("demo", {"city": "杭州", "token": "secret"})


def test_registry_records_tool_call_id_and_enforces_request_budget(monkeypatch):
    monkeypatch.setattr(settings, "tool_max_calls", 1)
    registry = ToolRegistry()
    registry.register(_spec(max_calls=5))
    with trace_run("run", "request") as trace:
        token = begin_tool_budget()
        try:
            registry.invoke("demo", {"city": "杭州"}, action_id="day-1")
            with pytest.raises(ToolInvocationError, match="预算"):
                registry.invoke("demo", {"city": "杭州"})
        finally:
            end_tool_budget(token)
    event = trace.to_dict()["events"][0]
    assert event["request_id"] == "request"
    assert event["tool_call_id"]
    assert event["action_id"] == "day-1"
    assert event["metadata"]["version"] == "1.0"


# ---- PR-11 沙箱接线：timeout 强制 / retry_policy 生效 / MCP annotations ----


def test_tool_timeout_is_enforced():
    """timeout_seconds 不再是纸面元数据：超时的 handler 让调用方快速拿到错误。"""
    registry = ToolRegistry()

    def slow(**params):
        time.sleep(2.0)
        return params

    registry.register(_spec(name="slow", handler=slow, timeout_seconds=0.2))
    started = time.monotonic()
    with pytest.raises(ToolInvocationError, match="超时"):
        registry.invoke("slow", {"city": "杭州"})
    assert time.monotonic() - started < 1.5, "超时必须强制生效，调用方不等 handler 自然结束"


def test_retry_policy_retries_idempotent_handler_then_succeeds():
    registry = ToolRegistry()
    calls = []

    def flaky(**params):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("transient")
        return params

    registry.register(_spec(name="flaky", handler=flaky, retry_policy={"max_retries": 1}))
    assert registry.invoke("flaky", {"city": "杭州"})["city"] == "杭州"
    assert len(calls) == 2, "retry_policy 必须真实生效（此前只是登记元数据）"


def test_retry_skipped_for_non_idempotent_tool():
    """重试只上幂等工具：非幂等 handler 即使登记了重试也不重试（L1 风险栏纪律）。"""
    registry = ToolRegistry()
    calls = []

    def once(**params):
        calls.append(1)
        raise RuntimeError("no retry")

    registry.register(_spec(name="once", handler=once, idempotent=False, retry_policy={"max_retries": 3}))
    with pytest.raises(RuntimeError):
        registry.invoke("once", {"city": "杭州"})
    assert len(calls) == 1


def test_retry_clamped_to_sandbox_ceiling():
    """登记 99 次重试也会被沙箱安全顶（_MAX_HANDLER_RETRIES=2）钳制。"""
    from app.agent.tools.registry.core import _MAX_HANDLER_RETRIES

    registry = ToolRegistry()
    calls = []

    def always(**params):
        calls.append(1)
        raise RuntimeError("down")

    registry.register(_spec(name="always", handler=always, retry_policy={"max_retries": 99}))
    with pytest.raises(RuntimeError):
        registry.invoke("always", {"city": "杭州"})
    assert len(calls) == 1 + _MAX_HANDLER_RETRIES


def test_public_specs_expose_mcp_annotations():
    """public_specs 对齐 MCP tool annotations 语义（readOnlyHint/destructiveHint）。"""
    registry = ToolRegistry()
    registry.register(_spec())
    annotations = registry.public_specs()[0]["annotations"]
    assert annotations == {"readOnlyHint": True, "destructiveHint": False}
