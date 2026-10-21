from collections.abc import Callable
from typing import cast

import pytest

from tests.agent_eval.eval_research_ablation import (
    SingleGenerationOnly,
    capture_calls,
    direct_research,
    no_second_generation,
    run_variant,
    strict_json,
    variant_order,
)


def test_single_generation_baseline_does_not_repair_invalid_json():
    from app.agent.core.json_utils import LlmJsonError

    with pytest.raises(LlmJsonError):
        strict_json("truncated", object())
    with pytest.raises(SingleGenerationOnly):
        no_second_generation()


def test_capture_calls_preserves_billing_and_cache_usage(monkeypatch):
    from contextlib import contextmanager

    from app.common import trial_billing

    settled = []

    class Charge:
        def settle(self, usage):
            settled.append(usage)

    @contextmanager
    def billable(*args, **kwargs):
        yield Charge()

    monkeypatch.setattr(trial_billing, "billable_call", billable)
    usage = {"prompt_tokens": 100, "completion_tokens": 30, "prompt_cache_hit_tokens": 40}
    with capture_calls() as calls:
        with trial_billing.billable_call("https://example.invalid", {"model": "deepseek-flash"}) as observer:
            assert observer is not None
            observer.settle(usage)
    assert settled == [usage]
    assert calls[0]["usage"] == usage


def test_direct_research_executes_search_without_model_planner(monkeypatch):
    from app.agent.research import factory, reasoning
    from app.agent.research.evidence import ResearchTask

    def unexpected(*args):
        pytest.fail("直接检索不能调用模型规划/评估")

    monkeypatch.setattr(reasoning, "plan_research", unexpected)
    monkeypatch.setattr(reasoning, "evaluate_research", unexpected)
    monkeypatch.setattr(factory, "_run_search", lambda state: {"items": [{"name": "西湖"}]})
    pack = direct_research(ResearchTask(domain="attraction", city="杭州"))
    assert pack.items == [{"name": "西湖"}]
    assert pack.rounds == 1
    assert "未进行模型充分性评估" in pack.gaps[0]


def test_rotating_order_covers_each_arm_in_each_position():
    assert [variant_order(i)[i] for i in range(3)] == ["direct_once", "current", "direct_repair"]
    assert set(variant_order(0)) == {"direct_once", "direct_repair", "current"}
    assert all(variant_order(index, ("current",)) == ("current",) for index in range(3))


def test_direct_repair_preserves_actual_repair_functions(monkeypatch):
    from contextlib import contextmanager
    from types import SimpleNamespace

    from app.agent.generation.content import trip_stream
    from app.agent.generation.orchestration import open_plans
    from app.agent.research import supervisor
    from app.services import itinerary_generation
    from tests.agent_eval import eval_research_ablation as ablation
    from tests.agent_eval import product_path

    parser, day = trip_stream.parse_llm_json_with_repair, open_plans.llm_open_day
    repaired = []
    monkeypatch.setattr(itinerary_generation, "_generate_day_with_trace", lambda: repaired.append(True))
    monkeypatch.setattr(ablation, "evaluate_quality", lambda *args: {})

    @contextmanager
    def product(*args):
        assert supervisor.run_research is direct_research
        assert trip_stream.parse_llm_json_with_repair is parser
        assert open_plans.llm_open_day is day
        cast(Callable[[], None], itinerary_generation._generate_day_with_trace)()
        yield (
            SimpleNamespace(model_dump=lambda **kwargs: {}),
            {
                "duration_ms": 1,
                "events": [{"name": "web_search.request", "metadata": {"billable": True, "estimated_cny": 0.005}}],
            },
            {},
        )

    monkeypatch.setattr(product_path, "run_product_path", product)
    result = run_variant({}, "direct_repair", "test")
    assert repaired == [True]
    assert result["day_repair_attempts"] == 1
    assert result["cost"]["estimated_cny"] is None
    assert result["cost"]["search_known_estimated_cny"] == 0.005
    assert result["cost"]["model_known_estimated_cny"] is None
