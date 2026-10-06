"""web_search JSON 解析层（2026-09-30 UI 评审遗留项）的回归测试。

背景：联网补池的解析失败此前只留 `Expecting value` 一行日志，原始响应不可见，
且"HTML 错误页/截断/夹 prose"长成一个样子。本文件钉三件事：
1. 既有成功语义不变（围栏剥离 + 括号截取，成功形态逐字兼容）；
2. 尾逗号轻微畸变可自愈；
3. 解析失败可观测（原始响应头尾进日志）且失败语义不变（抛错计入熔断，
   完全无结构 = None 负结果）。
"""

from __future__ import annotations

import logging

import pytest

from app.agent.data import web_search


@pytest.fixture(autouse=True)
def _reset_search_client_runtime_state():
    """清车道时间戳：BACKGROUND 车道 1.0s 间隔会把用例串成真实秒级等待。"""
    web_search._search_client.reset_runtime_state()


def test_parse_llm_json_success_shapes_unchanged():
    assert web_search._parse_llm_json('{"items": [{"name": "宽窄巷子"}]}') == {"items": [{"name": "宽窄巷子"}]}
    assert web_search._parse_llm_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert web_search._parse_llm_json('结果如下：{"a": 1} 以上。') == {"a": 1}
    assert web_search._parse_llm_json('[{"name": "x"}, {"name": "y"}]') == [{"name": "x"}, {"name": "y"}]
    # 对象与数组并存时沿用既有优先级：花括号截取先于方括号
    assert web_search._parse_llm_json('{"items": [1]} 和 [2]') == {"items": [1]}
    assert web_search._parse_llm_json('数组在前 [2]，对象在后 {"a": 1}') == [2]


def test_parse_llm_json_repairs_trailing_commas():
    assert web_search._parse_llm_json('{"items": [{"a": 1},],}') == {"items": [{"a": 1}]}
    assert web_search._parse_llm_json('[{"a": 1}, ]') == [{"a": 1}]


def test_parse_llm_json_no_structure_returns_none_and_logs(caplog):
    with caplog.at_level(logging.WARNING, logger="app.agent.data.web_search"):
        assert web_search._parse_llm_json("抱歉，我没有找到相关结果。") is None
        # 截断到没有任何闭合括号（max_tokens 掐断的典型形态）：与旧语义一致走 None
        # 负结果路径（120s 后可重试、不计熔断），但必须留下可观测日志
        assert web_search._parse_llm_json('{"items": [{"a": 1') is None
    assert "no JSON structure" in caplog.text
    assert "抱歉" in caplog.text, "无结构时也必须留下原始响应片段"
    assert '{"items"' in caplog.text, "截断形态也必须留下原始响应片段"


def test_parse_llm_json_unparseable_raises():
    # 有闭合括号但内容坏（引号/类型错误）：失败语义保留——抛错计入熔断计数
    with pytest.raises(ValueError, match="unparseable"):
        web_search._parse_llm_json('{"items": [{"a": 1}')


def test_web_search_json_failure_is_observable_and_returns_none(monkeypatch, caplog):
    """端到端：上游回错误页（有结构但坏）→ None + 头尾日志；熔断语义不变。"""
    monkeypatch.setattr(web_search, "web_search_enabled", lambda: True)
    monkeypatch.setattr(web_search, "search_channel_ready", lambda: True)  # 本用例验解析层，与通道能力无关

    class _FakeClient:
        def complete(self, *_args, **_kwargs):
            return '<html>{"status": 502, detail: gateway timeout}</html>'

    monkeypatch.setattr(web_search, "get_role_client", lambda _role: _FakeClient())
    with caplog.at_level(logging.WARNING, logger="app.agent.data.web_search"):
        assert web_search.web_search_json("成都有哪些酒店？", schema_hint="{}") is None
    assert "unparseable answer" in caplog.text
    assert "<html>" in caplog.text, "必须留下原始响应头，能定位是 HTML 错误页"
    assert "gateway timeout" in caplog.text, "必须留下原始响应尾，能定位错误详情"


def test_web_search_json_repairs_trailing_comma_end_to_end(monkeypatch):
    monkeypatch.setattr(web_search, "web_search_enabled", lambda: True)
    monkeypatch.setattr(web_search, "search_channel_ready", lambda: True)  # 本用例验解析层，与通道能力无关

    class _FakeClient:
        def complete(self, *_args, **_kwargs):
            return '{"items": [{"name": "成都首座万豪酒店", "estimated_cost": 600,},],}'

    monkeypatch.setattr(web_search, "get_role_client", lambda _role: _FakeClient())
    data = web_search.web_search_json("成都有哪些酒店？", schema_hint="{}")
    assert data == {"items": [{"name": "成都首座万豪酒店", "estimated_cost": 600}]}
