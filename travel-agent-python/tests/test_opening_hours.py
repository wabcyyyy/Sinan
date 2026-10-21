from datetime import date

import pytest

from app.agent.generation.content.reflect import hard_day_issues, validate_plans
from app.agent.generation.rules.opening_hours import opening_checks


def plan(hours=None, remark=None, start="09:00", end="10:00", day=1):
    return {
        "day_no": day,
        "items": [
            {
                "item_type": "attraction",
                "poi_name": "博物馆",
                "open_time": hours,
                "remark": remark,
                "start_time": start,
                "end_time": end,
            }
        ],
    }


def test_previous_real_ablation_closed_monday_is_a_hard_blocker():
    raw = plan(remark="免费但需分时预约；周一闭馆，出发前确认当日开放。")
    assert any("闭馆日冲突" in issue for issue in hard_day_issues(raw, start_date="2026-11-02"))
    assert opening_checks(raw, "2026-11-02")[0]["status"] == "violation"
    assert opening_checks({**raw, "day_no": 2}, "2026-11-02")[0]["status"] == "unknown"


@pytest.mark.parametrize(
    "hours,start,end,status",
    [
        ("09:00-12:00；14:00-17:00", "14:00", "16:00", "pass"),
        ("09:00-12:00；14:00-17:00", "11:30", "14:30", "violation"),
        ("09:00-17:00", "16:30", "18:00", "violation"),
        ("18:00-02:00", "23:00", "01:00", "pass"),
        ("18:00-02:00", "00:30", "01:30", "pass"),
        ("18:00-02:00", "03:00", "04:00", "violation"),
        ("周一12:00-17:00；周二至周日09:00-17:00", "09:00", "10:00", "violation"),
        ("周一12:00-17:00；周二至周日09:00-17:00", "13:00", "14:00", "pass"),
        ("09:00-17:00，16:30停止入馆", "16:40", "16:55", "violation"),
        (None, "09:00", "10:00", "unknown"),
        ("尚未查询", "09:00", "10:00", "unknown"),
    ],
)
def test_open_windows_are_not_collapsed_or_assumed_known(hours, start, end, status):
    assert opening_checks(plan(hours, start=start, end=end), "2026-11-02")[0]["status"] == status


def test_real_replay_does_not_apply_monday_closure_to_tuesday():
    raw = plan(remark="周一闭馆、周二正常开放", day=2)
    assert opening_checks(raw, "2026-11-02")[0]["status"] != "violation"
    raw["items"][0]["remark"] = "周一、周三闭馆"
    assert opening_checks(raw, "2026-11-02")[0]["status"] == "unknown"
    assert opening_checks(raw, "2026-11-03")[0]["status"] == "violation"


def test_live_product_date_prefixed_weekday_starts_a_new_rule():
    raw = plan(remark="博物馆周一闭馆、11月3日周二开放", day=2)
    assert opening_checks(raw, "2026-11-02")[0]["status"] == "unknown"
    assert opening_checks({**raw, "day_no": 1}, "2026-11-02")[0]["status"] == "unknown"


def test_live_source_mentioning_closure_concerns_is_not_a_closure():
    from app.agent.generation.rules.opening_rules import parse_opening_rules

    raw = plan("周一至周日均正常开放，无需担心闭馆问题", day=3)
    assert opening_checks(raw, "2026-11-02")[0]["status"] == "unknown"
    assert parse_opening_rules(raw["items"][0]["open_time"]).status == "unknown"
    raw["items"][0]["open_time"] = "周一至周日09:00-17:00开放，无需担心闭馆问题"
    assert opening_checks(raw, "2026-11-02")[0]["status"] == "pass"


def test_persisted_travel_date_overrides_start_date_and_day_number():
    raw = {**plan(remark="每周一闭馆", day=2), "travel_date": date(2026, 11, 2)}
    assert opening_checks(raw, "2026-11-02")[0]["status"] == "violation"


def test_missing_date_and_conditional_closure_are_not_claimed_open():
    assert opening_checks(plan("周一闭馆；周二至周日09:00-17:00"))[0]["status"] == "unknown"
    check = opening_checks(plan("周一闭馆（法定节假日除外）"), "2026-11-02")[0]
    assert check["status"] == "unknown" and "例外" in check["reason"]
    assert opening_checks(plan("周一不闭馆；09:00-17:00"), "2026-11-02")[0]["status"] == "pass"
    assert opening_checks(plan("星期一至五09:00-17:00"), "2026-11-04")[0]["status"] == "pass"


def test_unknown_opening_is_visible_without_becoming_a_false_success_claim():
    _, notes = validate_plans([plan()])
    assert any("开放安排待确认" in note for note in notes)
    assert not any("营业时间覆盖" in note for note in notes)


def test_food_hours_also_block_and_hotel_stays_do_not_use_opening_gate():
    raw = plan("09:00-17:00", start="19:00", end="20:00")
    raw["items"][0]["item_type"] = "food"
    assert any("开放时间不符" in issue for issue in hard_day_issues(raw))
    raw["items"][0]["item_type"] = "hotel"
    assert opening_checks(raw) == []
