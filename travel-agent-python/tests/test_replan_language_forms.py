import pytest

from app.agent.editing.replan_intent import parse_replan_change

ROWS = [{"id": 1, "poi_name": "地点1", "day_no": 1}, {"id": 2, "poi_name": "地点2", "day_no": 1}]


@pytest.mark.parametrize(
    "message,kind,item",
    [
        ("我想把地点1的预约推迟20分钟。", "delay", 1),
        ("请把地点2关闭。", "close", 2),
        ("把地点2的活动关掉。", "close", 2),
        ("帮我预约地点2，第1天14:00到15:00。", "appointment", 2),
        ("请帮我预约第1天14:00到15:00的地点2，第1天15:00前结束。", "appointment", 2),
        ("把地点2的预约改到第1天14:00，第1天15:00前结束。", "appointment", 2),
    ],
)
def test_observed_development_forms_keep_exact_operation(message, kind, item):
    result = parse_replan_change(message, ROWS, item_id=item)
    assert result.change and result.change.kind == kind and result.change.item_id == item
    if kind == "appointment":
        assert result.change.start_time == "14:00" and result.change.end_time == "15:00" and result.change.day_no == 1
    if kind == "delay":
        assert result.change.delay_minutes == 20


@pytest.mark.parametrize(
    "message",
    [
        "请不要取消地点2",
        "关闭地点2并推迟地点1",
        "帮我预约地点2，第1天15:00到14:00",
        "预约第1天14:00到15:00的地点2，第1天16:00前结束",
        "把地点2的预约改到第1天14:00，第2天15:00前结束",
    ],
)
def test_negation_multiple_targets_or_conflicting_time_still_requires_confirmation(message):
    result = parse_replan_change(message, ROWS, item_id=2)
    assert result.change is None and result.questions
