"""受限 Function Calling Loop。

模型只能调用 Tool Registry 暴露的只读工具；每轮限制、重复调用检测和总预算
由执行器负责。写库、删除行程、预订等动作不进入这个 Loop。
"""

from __future__ import annotations

import json
from typing import Any

from app.agent.runtime.run_limits import current_limits
from app.agent.runtime.trace import record_event
from app.agent.tools.registry import ToolInvocationError, registry


class FunctionCallingError(RuntimeError):
    pass


def run_tool_call_loop(
    client: Any, messages: list[dict], *, max_rounds: int = 3, model: str | None = None, max_tokens: int | None = None
) -> dict[str, Any]:
    """执行 OpenAI-compatible tools 协议并返回最终 assistant message。"""
    conversation = [dict(message) for message in messages]
    seen: set[str] = set()
    tool_events: list[dict[str, Any]] = []
    schemas = registry.function_schemas()
    for round_no in range(max_rounds):
        limits = current_limits()
        if limits:
            limits.check("llm")
        extra: dict[str, Any] = {"max_tokens": max_tokens} if max_tokens else {}
        response = client.chat_response(conversation, model=model, tools=schemas, tool_choice="auto", **extra)
        message = response.get("message") or {}
        calls = message.get("tool_calls") or []
        conversation.append(message)
        if not calls:
            record_event(
                "decision",
                "function_calling.complete",
                metadata={
                    "rounds": round_no + 1,
                    "tool_calls": len(tool_events),
                },
            )
            return {"message": message, "tool_calls": tool_events, "rounds": round_no + 1}
        if round_no + 1 >= max_rounds:
            raise FunctionCallingError("Function Calling 达到最大轮数")
        for call in calls:
            function = call.get("function") or {}
            name = str(function.get("name") or "")
            raw_arguments = function.get("arguments") or "{}"
            try:
                arguments = json.loads(raw_arguments) if isinstance(raw_arguments, str) else raw_arguments
            except json.JSONDecodeError as exc:
                raise FunctionCallingError(f"工具 {name} 参数不是合法 JSON") from exc
            signature = json.dumps({"name": name, "arguments": arguments}, ensure_ascii=False, sort_keys=True)
            if signature in seen:
                raise FunctionCallingError(f"检测到重复工具调用：{name}")
            seen.add(signature)
            try:
                result = registry.invoke(name, arguments)
                status = "ok"
            except ToolInvocationError as exc:
                result = {"error": str(exc)}
                status = "error"
            call_id = str(call.get("id") or "")
            tool_events.append({"name": name, "call_id": call_id, "status": status})
            conversation.append(
                {
                    "role": "tool",
                    "tool_call_id": call_id,
                    "content": json.dumps(result, ensure_ascii=False, default=str),
                }
            )
            record_event(
                "decision",
                "function_calling.tool_result",
                metadata={
                    "name": name,
                    "status": status,
                    "round": round_no + 1,
                },
            )
    raise FunctionCallingError("Function Calling 未返回最终答案")
