from app.agent.generation.orchestration import day_workflow
from app.schemas.trip import DailyPlan, GenerateDayRequest, TripItem


def _plan() -> DailyPlan:
    # 需通过 reflect：时长充足、含餐饮、时间不重叠
    return DailyPlan(
        day_no=1,
        items=[
            TripItem(
                item_type="attraction",
                poi_name="测试景点",
                start_time="09:00",
                end_time="12:00",
                duration_min=180,
                latitude=30.0005,
                longitude=120.0005,
            ),
            TripItem(
                item_type="food",
                poi_name="测试餐厅",
                start_time="12:20",
                end_time="13:20",
                duration_min=60,
                latitude=30.0008,
                longitude=120.0008,
            ),
            TripItem(
                item_type="attraction",
                poi_name="测试景点2",
                start_time="13:50",
                end_time="16:50",
                duration_min=180,
                latitude=30.0011,
                longitude=120.0011,
            ),
        ],
    )


def test_day_graph_retries_after_reflection(monkeypatch):
    calls = []

    def fake_once(req, *, force_fallback=False):
        calls.append((req.feedback, force_fallback))
        if len(calls) == 1:
            return DailyPlan(
                day_no=1,
                items=[
                    TripItem(item_type="attraction", poi_name="A", start_time="09:00", end_time="12:00"),
                    TripItem(item_type="food", poi_name="B", start_time="11:00", end_time="12:30"),
                ],
            ), "llm"
        return _plan(), "llm"

    monkeypatch.setattr(day_workflow, "generate_day_once", fake_once)
    result = day_workflow.run_day_agent(GenerateDayRequest(city="杭州"))

    assert result.items[0].poi_name == "测试景点"
    assert len(calls) == 2
    assert "时间冲突" in calls[1][0]
    assert calls[1][1] is False


def test_day_graph_uses_deterministic_fallback_after_second_failure(monkeypatch):
    calls = []

    def fake_once(req, *, force_fallback=False):
        calls.append(force_fallback)
        bad = DailyPlan(
            day_no=1,
            items=[
                TripItem(item_type="attraction", poi_name="A", start_time="09:00", end_time="12:00"),
                TripItem(item_type="food", poi_name="B", start_time="11:00", end_time="12:30"),
            ],
        )
        return bad, "fallback" if force_fallback else "llm"

    monkeypatch.setattr(day_workflow, "generate_day_once", fake_once)
    result = day_workflow.run_day_agent(GenerateDayRequest(city="杭州"))

    assert result.day_no == 1
    assert calls == [False, False, True]


def test_day_graph_retries_when_route_gap_is_too_short(monkeypatch):
    calls = []

    def fake_once(req, *, force_fallback=False):
        calls.append(req.feedback)
        if len(calls) == 1:
            return DailyPlan(
                day_no=1,
                items=[
                    TripItem(
                        item_type="attraction",
                        poi_name="远点A",
                        start_time="09:00",
                        end_time="10:00",
                        latitude=30.0,
                        longitude=120.0,
                    ),
                    TripItem(
                        item_type="attraction",
                        poi_name="远点B",
                        start_time="10:30",
                        end_time="12:00",
                        latitude=30.2,
                        longitude=120.0,
                    ),
                ],
            ), "llm"
        return _plan(), "llm"

    monkeypatch.setattr(day_workflow, "generate_day_once", fake_once)
    result = day_workflow.run_day_agent(GenerateDayRequest(city="杭州"))

    assert result.items[0].poi_name == "测试景点"
    assert len(calls) == 2
    assert "路线时间不足" in calls[1]


def test_day_graph_resamples_empty_plan(monkeypatch):
    """空天 plan 必须进 issues 触发既有重试环（P2：json_object 降级档间歇空天）。

    旧预期为什么是错的：validate_plans 对无 items 只记 log，空 plan 一次过
    直达落库门禁（kept PENDING），MAX_DAY_ATTEMPTS 重试环全程不参与——
    夜审 trip 90/91 的 day1 空天即此形状。补 issue 后走 retry（带反馈）→
    兜底（无反馈）的既有机制完成重采样，不新造机制。
    """
    calls = []

    def fake_once(req, *, force_fallback=False):
        calls.append((req.feedback, force_fallback))
        if len(calls) == 1:
            return DailyPlan(day_no=1, items=[]), "llm"
        return _plan(), "llm"

    monkeypatch.setattr(day_workflow, "generate_day_once", fake_once)
    result = day_workflow.run_day_agent(GenerateDayRequest(city="杭州"))

    assert [item.poi_name for item in result.items] == ["测试景点", "测试餐厅", "测试景点2"]
    assert len(calls) == 2
    assert "没有生成任何条目" in calls[1][0]
    assert calls[1][1] is False
