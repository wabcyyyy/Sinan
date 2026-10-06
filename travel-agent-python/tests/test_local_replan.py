from copy import deepcopy

from app.agent.editing.local_replan import run_local_replan
from app.schemas.trip import LocalReplanRequest


def _item(name, kind="attraction", start="09:00", end="10:00"):
    return {
        "item_type": kind,
        "poi_id": name,
        "poi_name": name,
        "latitude": 30.0,
        "longitude": 120.0,
        "start_time": start,
        "end_time": end,
        "duration_min": 60,
    }


def test_local_replan_only_changes_affected_day_and_keeps_locked_item():
    plans = [
        {"day_no": 1, "items": [_item("Day1")]},
        {"day_no": 2, "items": [_item("Locked"), _item("Old", start="11:00", end="12:00")]},
    ]
    before_day1 = deepcopy(plans[0])
    result = run_local_replan(
        LocalReplanRequest(
            city="杭州",
            affected_day_nos=[2],
            locked_names=["Locked"],
            candidate_names=["Day1"],
            plans=plans,
            action_id="day-2-replan",
        )
    )
    assert result["status"] in {"success", "degraded"}
    assert result["affected_day_nos"] == [2]
    assert result["plans"][0] == before_day1
    assert result["plans"][1]["items"][0]["poi_name"] == "Locked"
    assert result["replaced_items"][0]["from"] == "Old"


def test_local_replan_returns_explicit_failure_for_unknown_candidate():
    result = run_local_replan(
        LocalReplanRequest(
            city="杭州",
            affected_day_nos=[1],
            candidate_names=["不存在"],
            plans=[{"day_no": 1, "items": [_item("A")]}],
        )
    )
    assert result["status"] == "failed"
    assert "候选点位不存在" in result["violations"][0]


def test_local_replan_failure_does_not_drop_locked_item():
    locked = _item(
        "Locked",
        start="20:00",
        end="22:00",
    )
    locked["duration_min"] = 120
    locked["open_time"] = "20:00-20:30"
    result = run_local_replan(
        LocalReplanRequest(
            city="杭州",
            affected_day_nos=[1],
            locked_names=["Locked"],
            plans=[
                {
                    "day_no": 1,
                    "items": [
                        locked,
                    ],
                }
            ],
        )
    )
    assert result["status"] == "failed"
    assert any(item["poi_name"] == "Locked" for item in result["plans"][0]["items"])
