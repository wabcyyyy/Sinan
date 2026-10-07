"""原报告 #80 回放：概述中的地点和时段只由最终 items 定义。"""

from app.agent.generation.content.narrative import sync_schedule_summary
from app.agent.generation.orchestration.stream_branch import _stream_plan_model
from app.common.vo_json import iso_time, parse_time_safe
from app.schemas.trip import DailyPlan, TripItem


def _plan():
    return DailyPlan(
        day_no=3,
        theme="黄昏城墙、合江亭夜色",
        note="晚餐火锅后合江亭江边收尾",
        practical_notes=["出发前核实开放时间"],
        items=[
            TripItem(poi_name="永宁门城墙", item_type="attraction", start_time="13:10", end_time="14:50"),
            TripItem(poi_name="望平街", item_type="attraction", start_time="15:30", end_time="17:00"),
        ],
    )


def test_summary_uses_final_names_and_exact_times_without_unscheduled_promises():
    plan = _plan()
    sync_schedule_summary(plan)
    assert plan.theme == "永宁门城墙 → 望平街"
    assert plan.note == "13:10–14:50 永宁门城墙；15:30–17:00 望平街（价格为估算，请以现场或官方渠道为准）"
    assert plan.practical_notes == ["出发前核实开放时间"]
    first = plan.model_dump()
    sync_schedule_summary(plan)
    assert plan.model_dump() == first
    plan.items.pop()
    plan.items[0].poi_name = "西安城墙"
    sync_schedule_summary(plan)
    assert plan.theme == "西安城墙"
    assert "望平街" not in plan.note


def test_stream_summary_has_same_authoritative_schedule_and_handles_empty_plan():
    result = _stream_plan_model(_plan().model_dump())
    assert result.note is not None and result.theme is not None
    assert "合江亭" not in result.note and "黄昏" not in result.theme
    result.items = []
    sync_schedule_summary(result)
    assert result.theme is None
    assert result.note is not None
    assert "尚未生成可用行程" in result.note


def test_summary_time_matches_persisted_clock_and_does_not_promise_invalid_free_text():
    plan = DailyPlan(
        day_no=1,
        items=[
            TripItem(item_type="hotel", poi_name="住宿", start_time="21:30:00", end_time="次日07:00"),
            TripItem(item_type="attraction", poi_name="夜景", start_time="23:50", end_time="24:00"),
        ],
    )
    sync_schedule_summary(plan)
    assert plan.items[0].start_time == "21:30" and plan.items[0].end_time is None
    assert plan.items[1].end_time == "00:00"
    assert plan.note is not None
    assert "次日07:00" not in plan.note and "21:30 住宿" in plan.note
    for item in plan.items:
        assert item.start_time == iso_time(parse_time_safe(item.start_time))
        assert item.end_time == iso_time(parse_time_safe(item.end_time))
