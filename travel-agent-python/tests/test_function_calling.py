import json

import pytest

from app.agent.runtime.trace import trace_run
from app.agent.tools.function_calling import FunctionCallingError, run_tool_call_loop


class FakeClient:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.requests = []

    def chat_response(self, messages, **kwargs):
        self.requests.append((messages, kwargs))
        return next(self.responses)


def test_function_calling_executes_registered_read_tool_then_returns_answer(monkeypatch):
    monkeypatch.setattr(
        "app.agent.tools.registry.registry.invoke", lambda name, arguments: {"city": arguments["city"], "items": []}
    )
    client = FakeClient(
        [
            {
                "message": {
                    "role": "assistant",
                    "tool_calls": [
                        {
                            "id": "call-1",
                            "type": "function",
                            "function": {"name": "search_pois", "arguments": json.dumps({"city": "杭州"})},
                        }
                    ],
                }
            },
            {"message": {"role": "assistant", "content": "已完成检索"}},
        ]
    )
    with trace_run("fc-run") as trace:
        result = run_tool_call_loop(client, [{"role": "user", "content": "查杭州"}])
    assert result["message"]["content"] == "已完成检索"
    assert result["tool_calls"][0]["status"] == "ok"
    assert any(event["name"] == "function_calling.complete" for event in trace.to_dict()["events"])


def test_function_calling_rejects_same_tool_and_arguments_twice(monkeypatch):
    monkeypatch.setattr("app.agent.tools.registry.registry.invoke", lambda *_args, **_kwargs: {})
    call = {"id": "call", "type": "function", "function": {"name": "search_pois", "arguments": '{"city":"杭州"}'}}
    client = FakeClient(
        [
            {"message": {"role": "assistant", "tool_calls": [call]}},
            {"message": {"role": "assistant", "tool_calls": [call]}},
        ]
    )
    with pytest.raises(FunctionCallingError, match="重复工具调用"):
        run_tool_call_loop(client, [{"role": "user", "content": "查"}])
