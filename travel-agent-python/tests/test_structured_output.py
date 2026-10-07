"""Structured output 收口单测（PR-5）：强约束 schema + 三层解析路径。

背景（spike 2026-09-24，tests/probe_structured_output.py 可重跑）：DashScope
OpenAI 兼容网关**接受并执行** `response_format: json_schema strict`（诱导违规的
day_no 超界值被约束回界内、多余键被拒）；json_object 基线不执行 schema。据此
（D3 强约束档）生成主链路出口换 json_schema，`parse_llm_json` 的栅栏/截取打捞
退役，坏输出走「严格解析 → 单次修复重试 → 兜底」三层（decide.py 已验证模式的推广）。

修订（夜审 R2-F1，2026-10-05）：主通道 2026-10-04 换到 DeepSeek 官方后，其对
json_schema 返回 400（同 key json_object 200，探针实证）——出口档位改为按
model_registry 能力位分支（守约网关仍 strict schema，DeepSeek 降级 json_object，
靠 prompt 内嵌形状 + 修复重试兜结构）。
"""

from __future__ import annotations

import json

import pytest
from jsonschema import Draft202012Validator

from app.agent.core.json_utils import LlmJsonError, parse_llm_json, parse_llm_json_or_none
from app.agent.generation.content.day_prompts import (
    open_day_output_schema,
    open_trip_output_schema,
)
from app.agent.generation.content.json_output import parse_llm_json_with_repair
from app.common.config import settings
from app.schemas.trip import GenerateDayRequest

DEEPSEEK = "https://api.deepseek.com"
DASHSCOPE = "https://dashscope.aliyuncs.com/compatible-mode/v1"


class _FakeClient:
    """修复重试替身：记录 repair 调用，按脚本返回修复产物。"""

    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.calls: list[dict] = []

    def chat(self, messages, **kwargs):
        self.calls.append(kwargs)
        return self.reply


class TestOutputSchemas:
    def test_schemas_are_valid_and_closed(self):
        for schema in (open_day_output_schema(1), open_day_output_schema(2), open_trip_output_schema()):
            Draft202012Validator.check_schema(schema)
            assert schema["additionalProperties"] is False

    def test_item_schema_bans_self_reported_fields(self):
        """D6=C 事前禁产：经纬度 / poi_id / 来源字段不在 schema 里，模型想输出也过不了闭合约束。"""
        item = open_day_output_schema(1)["properties"]["items"]["items"]
        for banned in ("latitude", "longitude", "poi_id", "source", "verification_status"):
            assert banned not in item["properties"], f"{banned} 重新出现在 LLM 输出契约里"

    def test_trip_theme_key_follows_prompt_branch(self):
        """与 prompt 同步分支：仅第 1 天放行 trip_theme，其余天闭合禁产。"""
        assert "trip_theme" in open_day_output_schema(1)["properties"]
        assert "trip_theme" not in open_day_output_schema(2)["properties"]

    def test_generation_exits_pass_capability_gated_response_format(self, monkeypatch):
        """生成主链路两个出口必须带结构化输出档，档位随网关能力位（契约钉子，R2-F1）。

        旧预期"两出口恒发 strict json_schema"为什么是错的：主通道 2026-10-04 换到
        DeepSeek 官方后，该网关对 json_schema 返回 400（2026-10-05 探针实证，同 key
        json_object 200）——恒发强约束档 = 生成主链路 100% 失败。钉子改为双向钉：
        无能力位网关（DeepSeek）必须降级 json_object，守约网关（DashScope）必须 strict schema。
        """
        seen: list[dict] = []

        class _Capture:
            def complete(self, *args, **kwargs):
                seen.append(kwargs)
                return json.dumps(
                    {
                        "trip_theme": "T",
                        "daily_plans": [{"day_no": 1, "items": [{"poi_name": "A", "item_type": "attraction"}]}],
                        "items": [{"poi_name": "A", "item_type": "attraction"}],
                        "suggestions": [],
                    }
                )

            def chat(self, *args, **kwargs):
                seen.append(kwargs)
                return json.dumps({"note": "n", "items": [], "practical_notes": ["a"]})

        from app.agent.generation.content import day_prompts
        from app.agent.generation.orchestration import day_stream

        monkeypatch.setattr(day_prompts, "get_llm_client", lambda: _Capture())
        monkeypatch.setattr(day_stream, "get_llm_client", lambda: _Capture())

        monkeypatch.setattr(settings, "llm_base_url", DEEPSEEK)
        for role in ("main", "fast", "vision", "judge", "search", "stt"):
            monkeypatch.setattr(settings, f"llm_role_{role}", "")
        day_prompts.llm_open_trip(GenerateDayRequest(city="杭州", days=2, day_no=1))
        day_stream.llm_open_day(GenerateDayRequest(city="杭州", days=2, day_no=1), set())
        assert seen, "两个出口都未触发 LLM 调用"
        for kwargs in seen:
            fmt = kwargs.get("response_format")
            assert fmt == {"type": "json_object"}, f"DeepSeek 出口未降级：{fmt}"

        seen.clear()
        monkeypatch.setattr(settings, "llm_provider_dashscope_base_url", DASHSCOPE)
        monkeypatch.setattr(settings, "llm_provider_dashscope_api_key", "dash-key")
        monkeypatch.setattr(settings, "llm_role_fast", "dashscope:qwen-plus")
        day_prompts.llm_open_trip(GenerateDayRequest(city="杭州", days=2, day_no=1))
        day_stream.llm_open_day(GenerateDayRequest(city="杭州", days=2, day_no=1), set())
        for kwargs in seen:
            fmt = kwargs.get("response_format") or {}
            assert fmt.get("type") == "json_schema", f"守约网关出口未带 json_schema：{fmt}"
            assert fmt["json_schema"]["strict"] is True


class TestThreeLayerParsing:
    def test_strict_parse_rejects_fenced_and_truncated(self):
        """第一层：栅栏/截断一律算坏 JSON（打捞分支已删除，不再救活半截真相）。"""
        with pytest.raises(LlmJsonError):
            parse_llm_json('```json\n{"a": 1}\n```')
        with pytest.raises(LlmJsonError):
            parse_llm_json('{"a": 1')  # 截断
        with pytest.raises(LlmJsonError):
            parse_llm_json("")  # 空输出
        assert parse_llm_json_or_none('{"a": 1}') == {"a": 1}
        assert parse_llm_json_or_none('前言 {"a": 1} 后语') is None  # 混入叙事也不再截取

    def test_repair_retry_recovers_malformed_json(self):
        """第二层：坏 JSON → 单次修复重试（decide.py:139-151 模式），修好即用。"""
        client = _FakeClient('{"day_no": 1, "note": "修复后"}')
        data = parse_llm_json_with_repair('{"day_no": 1, "note": ', client)
        assert data["note"] == "修复后"
        assert len(client.calls) == 1, "修复只试一次"
        assert client.calls[0]["temperature"] == 0

    def test_repair_failure_raises_for_fallback(self):
        """第三层触发点：修复也失败 → 抛 LlmJsonError，交调用方兜底（逐日兜底/待研究草案）。"""
        client = _FakeClient("还是坏的")
        with pytest.raises(LlmJsonError):
            parse_llm_json_with_repair('{"broken": ', client)
        assert len(client.calls) == 1

    def test_generation_falls_back_when_all_layers_fail(self, monkeypatch):
        """第三层本体：主链出口双层全失败 → open_plans 缺口天逐日兜底 → 也失败才待研究草案。"""
        from app.agent.generation.orchestration import open_plans
        from app.schemas.trip import GenerateRequest

        def boom_trip(req):
            raise LlmJsonError("双层失败")

        def boom_day(req, used):
            raise LlmJsonError("双层失败")

        monkeypatch.setattr(open_plans, "llm_open_trip", boom_trip)
        monkeypatch.setattr(open_plans, "llm_open_day", boom_day)
        increment, errors = open_plans.generate_open_plans(
            GenerateRequest(city="杭州", days=2), "", None, candidates=[], foods=[], weather=[]
        )
        assert increment is None, "全链失败不得冒充生成成功"
        assert errors, "真因（双层失败）必须传给调用方"  # 兜底产物 draft_state 由 graph_nodes 层产
