"""真实空正文/形状丢失故障回放：保留请求修复，空行程不能成为成功产物。"""

import json

import pytest

from app.agent.core.json_utils import LlmJsonError
from app.agent.generation.content import day_prompts
from app.agent.generation.content.json_output import validate_trip_output
from app.agent.generation.orchestration import day_stream, day_workflow
from app.schemas.trip import DailyPlan, GenerateDayRequest


def _day(day_no=1):
    return {"day_no": day_no, "items": [{"poi_name": "文殊院", "item_type": "attraction"}]}


class Client:
    def __init__(self, raw, repaired):
        self.raw, self.repaired = raw, json.dumps(repaired, ensure_ascii=False)
        self.first, self.repairs = [], []

    def complete(self, user, **kwargs):
        self.first.append((user, kwargs))
        return self.raw

    def chat(self, messages, **kwargs):
        self.repairs.append((messages, kwargs))
        return self.repaired


@pytest.mark.parametrize("raw", ["", "{}", '{"items":[]}', '{"items":[{"poi_name":"虚项"}]}', '{"items":'])
@pytest.mark.parametrize("entry", ["day", "trip"])
def test_recovery_retains_original_request_and_rejects_empty_shape(monkeypatch, raw, entry):
    reply = _day() if entry == "day" else {"daily_plans": [_day(), _day(2)]}
    client = Client(raw, reply)
    monkeypatch.setattr(day_stream, "get_llm_client", lambda: client)
    monkeypatch.setattr(day_prompts, "get_llm_client", lambda: client)
    req = GenerateDayRequest(
        city="成都", days=2, preferences=["熊猫", "美食"], hotel_tier="青旅", requirements="不要爬山"
    )
    result = day_stream.llm_open_day(req, set()) if entry == "day" else day_prompts.llm_open_trip(req)[0]
    assert result
    assert len(client.first) == len(client.repairs) == 1
    messages, repair_kwargs = client.repairs[0]
    first_user, first_kwargs = client.first[0]
    assert messages[:2] == [
        {"role": "system", "content": first_kwargs["system_prompt"]},
        {"role": "user", "content": first_user},
    ]
    assert all(value in messages[0]["content"] for value in ("熊猫", "美食", "青旅", "不要爬山"))
    assert "成都" in messages[1]["content"]
    assert first_kwargs["enable_thinking"] is repair_kwargs["enable_thinking"] is False


@pytest.mark.parametrize("repaired", [{}, {"items": []}, {"items": [{"poi_name": "酒店", "item_type": "hotel"}]}])
def test_second_unusable_output_fails_without_more_repairs(monkeypatch, repaired):
    client = Client("", repaired)
    monkeypatch.setattr(day_stream, "get_llm_client", lambda: client)
    with pytest.raises(LlmJsonError):
        day_stream.llm_open_day(GenerateDayRequest(city="成都"), set())
    assert len(client.repairs) == 1


@pytest.mark.parametrize("plans", [[], [_day(), _day()], [_day(3)], [{"day_no": True, "items": []}]])
def test_trip_output_must_contain_unique_in_range_usable_days(plans):
    with pytest.raises(LlmJsonError):
        validate_trip_output({"daily_plans": plans}, 2)
    validate_trip_output({"daily_plans": [_day()]}, 2)  # 缺口走既有逐日补齐。


def test_day_graph_exhaustion_is_failure_and_last_attempt_clears_feedback(monkeypatch):
    calls = []

    def empty(req, *, force_fallback=False):
        calls.append((req.feedback, force_fallback))
        return DailyPlan(day_no=1, items=[]), "open"

    monkeypatch.setattr(day_workflow, "generate_day_once", empty)
    with pytest.raises(ValueError, match="单日行程生成失败"):
        day_workflow.run_day_agent(GenerateDayRequest(city="成都", per_day_chain=True))
    assert len(calls) == 3
    assert calls[1][0]
    assert calls[2] == ("", True)
