"""DeepSeek 官方 API 适配与思考强度控制单测。

验收点：
1. 对齐 DeepSeek 官方规范：
   - model: deepseek-flash
   - thinking: {"type": "enabled"}
   - reasoning_effort: "low"
   - 不发送非标准的 enable_search 和 enable_thinking（防 400 Bad Request）
2. 保持 DashScope 百炼兼容：
   - enable_search / enable_thinking 正常发送
3. 流式解析隔离：
   - delta 中的 reasoning_content 不会泄露到 content 迭代器
"""

from __future__ import annotations

from unittest.mock import MagicMock

import httpx
import pytest

import app.agent  # noqa: F401 先完成 agent 门面导入
from app.common import llm_client
from app.common.config import settings
from app.common.llm_client import LLMClient, _apply_provider_options


def test_deepseek_official_payload_options(monkeypatch):
    monkeypatch.setattr(settings, "llm_reasoning_effort", "low")
    monkeypatch.setattr(settings, "llm_enable_thinking", True)

    payload = {"model": "deepseek-flash", "messages": []}
    _apply_provider_options(
        payload,
        base_url="https://api.deepseek.com",
        enable_search=False,
    )

    assert payload.get("reasoning_effort") == "low"
    assert payload.get("thinking") == {"type": "enabled"}
    # 非标参数不得混入官方请求
    assert "enable_search" not in payload
    assert "enable_thinking" not in payload


def test_deepseek_official_refuses_search_instead_of_dropping_it(monkeypatch):
    """要求联网时响亮报错——静默丢参数会让模型拿记忆冒充"联网检索结果"。

    （通道能力判据与联网入口的降级见 test_provider_capability.py。）
    """
    monkeypatch.setattr(settings, "llm_reasoning_effort", "low")

    payload = {"model": "deepseek-flash", "messages": []}
    with pytest.raises(ValueError, match="不支持联网搜索"):
        _apply_provider_options(
            payload,
            base_url="https://api.deepseek.com",
            enable_search=True,
        )


def test_deepseek_official_disabled_thinking(monkeypatch):
    monkeypatch.setattr(settings, "llm_reasoning_effort", None)
    monkeypatch.setattr(settings, "llm_enable_thinking", False)

    payload = {"model": "deepseek-flash", "messages": []}
    _apply_provider_options(
        payload,
        base_url="https://api.deepseek.com/v1",
        enable_search=False,
    )

    assert payload.get("thinking") == {"type": "disabled"}
    assert "reasoning_effort" not in payload


def test_blank_effort_is_treated_as_unset(monkeypatch):
    """`.env` 里 `LLM_REASONING_EFFORT=`（留空）是"不设"的直觉写法，但 pydantic-settings
    解析成 ""——空串若被当成有效强度发出去，DeepSeek 会 400。空串必须等同没配。"""
    monkeypatch.setattr(settings, "llm_reasoning_effort", "")
    monkeypatch.setattr(settings, "llm_enable_thinking", None)

    payload = {"messages": []}
    _apply_provider_options(payload, base_url="https://api.deepseek.com", enable_search=False)

    assert payload == {"messages": []}, "空串不许变成 reasoning_effort 发出去"


def test_explicit_thinking_off_wins_over_effort(monkeypatch):
    """显式关闭思考是硬指令：不能因为 reasoning_effort 有值就又把思考打开。"""
    monkeypatch.setattr(settings, "llm_reasoning_effort", "low")
    monkeypatch.setattr(settings, "llm_enable_thinking", False)

    payload = {"messages": []}
    _apply_provider_options(payload, base_url="https://api.deepseek.com", enable_search=False)

    assert payload["thinking"] == {"type": "disabled"}
    assert "reasoning_effort" not in payload, "关了思考还发强度没有意义"


def test_thinking_off_wins_on_dashscope_too(monkeypatch):
    monkeypatch.setattr(settings, "llm_reasoning_effort", "low")
    monkeypatch.setattr(settings, "llm_enable_thinking", False)

    payload = {"messages": []}
    _apply_provider_options(payload, base_url="https://dashscope.aliyuncs.com/compatible-mode/v1", enable_search=False)

    assert payload["enable_thinking"] is False
    assert "reasoning_effort" not in payload


def test_dashscope_payload_options(monkeypatch):
    monkeypatch.setattr(settings, "llm_reasoning_effort", "low")
    monkeypatch.setattr(settings, "llm_enable_thinking", True)

    payload = {"model": "qwen-plus", "messages": []}
    _apply_provider_options(
        payload,
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        enable_search=True,
    )

    assert payload.get("enable_search") is True
    assert payload.get("enable_thinking") is True
    assert payload.get("reasoning_effort") == "low"
    assert "thinking" not in payload


def test_chat_response_sends_deepseek_parameters(monkeypatch):
    monkeypatch.setattr(settings, "llm_reasoning_effort", "low")
    captured_payloads = []

    class _MockHttpClient:
        def post(self, url, json_payload=None, **kwargs):
            captured_payloads.append(json_payload or kwargs.get("json"))
            resp = MagicMock(spec=httpx.Response)
            resp.status_code = 200
            resp.json.return_value = {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": "旅程规划完成",
                            "reasoning_content": "先分析天数与偏好...",
                        }
                    }
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 20},
            }
            return resp

        def is_closed(self):
            return False

    monkeypatch.setattr(llm_client, "_get_http_client", lambda: _MockHttpClient())

    client = LLMClient(
        base_url="https://api.deepseek.com",
        api_key="test-key",
        model="deepseek-flash",
    )
    result = client.chat_response(
        messages=[{"role": "user", "content": "你好"}],
        enable_search=False,
    )

    assert len(captured_payloads) == 1
    sent = captured_payloads[0]
    assert sent["model"] == "deepseek-flash"
    assert sent["reasoning_effort"] == "low"
    assert sent["thinking"] == {"type": "enabled"}
    assert "enable_search" not in sent
    assert result["message"]["content"] == "旅程规划完成"


def test_stream_chat_deltas_filters_reasoning_content(monkeypatch):
    class _MockStreamContext:
        def __init__(self, lines):
            self.lines = lines

        def __enter__(self):
            resp = MagicMock()
            resp.iter_lines.return_value = self.lines
            return resp

        def __exit__(self, exc_type, exc_val, exc_tb):
            pass

    class _MockHttpClient:
        def stream(self, method, url, **kwargs):
            sse_lines = [
                'data: {"choices":[{"delta":{"role":"assistant","reasoning_content":"深度思考第1步"}}]}',
                'data: {"choices":[{"delta":{"reasoning_content":"深度思考第2步"}}]}',
                'data: {"choices":[{"delta":{"content":"正式"}}]}',
                'data: {"choices":[{"delta":{"content":"内容"}}]}',
                'data: {"choices":[],"usage":{"prompt_tokens":10,"completion_tokens":20}}',
                "data: [DONE]",
            ]
            return _MockStreamContext(sse_lines)

        def is_closed(self):
            return False

    monkeypatch.setattr(llm_client, "_get_http_client", lambda: _MockHttpClient())

    client = LLMClient(
        base_url="https://api.deepseek.com",
        api_key="test-key",
        model="deepseek-flash",
    )
    deltas = list(client.stream_chat_deltas([{"role": "user", "content": "你好"}]))
    assert deltas == ["正式", "内容"]
