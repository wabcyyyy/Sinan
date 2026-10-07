"""按次关闭思考贯通全部客户端入口，生成偏好贯通业务与流式接缝。"""

import json

import httpx
import pytest

from app.agent.generation.orchestration import open_plans, stream_branch
from app.common import llm_client
from app.common.config import settings
from app.schemas.trip import GenerateDayRequest, GenerateRequest


@pytest.mark.parametrize(
    "method", ["chat_response", "chat", "complete", "stream_chat_deltas", "stream_chat", "stream_complete"]
)
def test_per_request_thinking_override_reaches_provider_without_changing_global(monkeypatch, method):
    monkeypatch.setattr(settings, "llm_enable_thinking", True)
    monkeypatch.setattr(settings, "llm_reasoning_effort", "low")
    sent = []

    def handle(request):
        payload = json.loads(request.content)
        sent.append(payload)
        if payload.get("stream"):
            return httpx.Response(200, text='data: {"choices":[{"delta":{"content":"{}"}}]}\n\ndata: [DONE]\n\n')
        return httpx.Response(200, json={"choices": [{"message": {"content": "{}"}}]})

    with httpx.Client(transport=httpx.MockTransport(handle)) as http:
        monkeypatch.setattr(llm_client, "_get_http_client", lambda: http)
        client = llm_client.LLMClient(base_url="https://api.deepseek.com", api_key="test", model="deepseek-flash")
        args = "JSON" if "complete" in method else [{"role": "user", "content": "JSON"}]
        result = getattr(client, method)(args, enable_thinking=False)
        if method == "stream_chat_deltas":
            assert list(result) == ["{}"]
        assert sent[0]["thinking"] == {"type": "disabled"}
        assert "reasoning_effort" not in sent[0]
        client.chat(args if isinstance(args, list) else [{"role": "user", "content": args}])
        assert sent[-1]["thinking"] == {"type": "enabled"}
        assert settings.llm_enable_thinking is True


def test_open_plans_passes_preferences_to_whole_and_missing_day_generation(monkeypatch):
    captured = []

    def trip(req):
        captured.append(req)
        return [], []  # 整段缺口必须进入逐日路径。

    def day(req, used):
        captured.append(req)
        return {"items": []}

    monkeypatch.setattr(open_plans, "llm_open_trip", trip)
    monkeypatch.setattr(open_plans, "llm_open_day", day)
    open_plans.generate_open_plans(GenerateRequest(city="成都", days=2, preferences=["熊猫"]), "", [], [], [], [])
    assert len(captured) == 3
    assert all(req.preferences == ["熊猫"] for req in captured)


def test_stream_request_retains_preferences(monkeypatch):
    captured = []
    monkeypatch.setattr(stream_branch, "get_stream_writer", lambda: lambda event: None)
    monkeypatch.setattr(stream_branch, "build_suggestions", lambda *args, **kwargs: [])
    monkeypatch.setattr(stream_branch, "fill_suggestion_gaps", lambda *args, **kwargs: [])

    def generate(req, *args, **kwargs):
        captured.append(req)
        return {}, []

    monkeypatch.setattr(open_plans, "generate_open_plans", generate)
    from app.agent.research.agent_state import UnifiedAgentState

    stream_branch.stream_generate(
        UnifiedAgentState(day_request=GenerateDayRequest(city="成都", preferences=["熊猫"])), {}
    )
    assert captured[0].preferences == ["熊猫"]
