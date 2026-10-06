"""统一生成图冒烟：day / trip 两种 mode 均可从同一张图进入。"""

from app.agent.generation.orchestration.trip_graph import (
    empty_day_state,
    empty_trip_state,
    run_day,
    unified_agent_graph,
)
from app.agent.research.agent_state import MODE_DAY, MODE_TRIP
from app.schemas.trip import GenerateDayRequest, GenerateRequest


def test_unified_graph_exists():
    assert unified_agent_graph is not None


def test_day_state_mode(monkeypatch):
    from app.agent.generation.orchestration import day_workflow

    def fake_once(req, *, force_fallback=False):
        from app.schemas.trip import DailyPlan, TripItem

        return DailyPlan(
            day_no=1,
            items=[
                TripItem(
                    item_type="attraction",
                    poi_name="A",
                    start_time="09:00",
                    end_time="12:00",
                    duration_min=180,
                    latitude=30.0,
                    longitude=120.0,
                ),
                TripItem(
                    item_type="food",
                    poi_name="B",
                    start_time="12:20",
                    end_time="13:20",
                    duration_min=60,
                    latitude=30.01,
                    longitude=120.01,
                ),
                TripItem(
                    item_type="attraction",
                    poi_name="C",
                    start_time="13:50",
                    end_time="16:50",
                    duration_min=180,
                    latitude=30.02,
                    longitude=120.02,
                ),
            ],
        ), "llm"

    monkeypatch.setattr(day_workflow, "generate_day_once", fake_once)
    plan = run_day(GenerateDayRequest(city="杭州"))
    assert plan.items[0].poi_name == "A"


def test_empty_states_carry_mode():
    assert empty_day_state(GenerateDayRequest(city="杭州"))["mode"] == MODE_DAY
    assert empty_trip_state(GenerateRequest(city="杭州", days=1))["mode"] == MODE_TRIP


# ---------- BIZ-2：单日链重试预算与 days 解耦 ----------


def _day_state(req: GenerateDayRequest, attempts: int):
    from app.agent.research.agent_state import UnifiedAgentState

    return UnifiedAgentState(mode=MODE_DAY, day_request=req, validation_issues=["第 1 天无行程项"], attempts=attempts)


def test_per_day_chain_keeps_full_retry_budget_with_days():
    """单日链（per_day_chain=True）带 days>1 也拿满重试预算——传 days 只为修
    预算分档，不再被「days>1 ⇒ 只试 1 次」绑架。"""
    from app.agent.generation.orchestration.trip_graph import day_route_after_reflect
    from app.agent.generation.rules.generation_core import MAX_DAY_ATTEMPTS

    req = GenerateDayRequest(city="杭州", days=7, per_day_chain=True)
    for attempt in range(MAX_DAY_ATTEMPTS - 1):
        assert day_route_after_reflect(_day_state(req, attempt)) == "retry"
    assert day_route_after_reflect(_day_state(req, MAX_DAY_ATTEMPTS)) == "fallback"


def test_whole_trip_day_request_still_single_attempt():
    """整段链的逐日兜底（days>1、无 per_day_chain 标记）维持 1 次尝试的原口径。"""
    from app.agent.generation.orchestration.trip_graph import day_route_after_reflect

    req = GenerateDayRequest(city="杭州", days=7)
    assert day_route_after_reflect(_day_state(req, 0)) == "retry"
    assert day_route_after_reflect(_day_state(req, 1)) == "fallback"


def test_budget_tier_divides_by_real_days():
    """BIZ-2 的另一半：预算分档按真实天数摊——¥7000/7 天是日均 ¥1000，
    不再被当成单日 ¥7000 直升奢华档。"""
    from app.agent.generation.rules.budget import budget_tier

    _label_per_day, _g, ppd = budget_tier(7000, 1, 7)
    assert ppd == 1000.0
    label_1d, _, _ = budget_tier(7000, 1, 1)
    label_7d, _, _ = budget_tier(7000, 1, 7)
    assert label_1d == "奢华档" and label_7d != "奢华档"
