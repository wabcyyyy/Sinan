"""Important data boundaries of the independent evidence audit; no model or benchmark reruns."""

import pytest

from scripts.audit_resume_evidence import (
    csv_pass,
    raw_metrics,
    replan_valid,
    require_universe,
    summarize_replan_rows,
    unique_index,
)


def test_duplicate_and_missing_denominators_are_rejected():
    with pytest.raises(ValueError, match="duplicate"):
        unique_index([{"uid": "a"}, {"uid": "a"}], "uid")
    with pytest.raises(ValueError, match="denominator"):
        require_universe(["a"], ["a", "b"])
    with pytest.raises(ValueError, match="denominator"):
        require_universe(["a", "a"], ["a", "b"])


def test_empty_score_and_failed_proposal_cannot_be_success():
    with pytest.raises(ValueError, match="empty score"):
        csv_pass({"data_id": "a", "logic_0": ""}, 1)
    assert not replan_valid({"status": "UNKNOWN", "proposal": [{}], "feasible_checked": True, "checker_errors": []})
    assert not replan_valid({"status": "OPTIMAL", "proposal": None, "feasible_checked": True, "checker_errors": []})


def test_raw_metrics_count_cross_day_separately_from_clock_shift_and_protect_removals():
    before = [
        {
            "day_no": 1,
            "items": [
                {"id": 1, "start_time": "09:00", "end_time": "10:00"},
                {"id": 2, "start_time": "10:00", "end_time": "11:00"},
            ],
        }
    ]
    after = [{"day_no": 2, "items": [{"id": 1, "start_time": "09:30", "end_time": "10:30"}]}]
    assert raw_metrics(before, after, [1, 2]) == {
        "changed_items": 2,
        "removed_items": 1,
        "cross_day_moves": 1,
        "start_shift_minutes": 30,
        "locked_violations": 2,
    }


def test_third_arm_failure_does_not_filter_the_two_global_comparison():
    rows = [
        {
            "id": str(i),
            "arms": {
                "global_standard": {"valid": True, "status": "FEASIBLE", "metrics": {"changed_items": 2 + 4 * i}},
                "global_disturbance": {"valid": True, "status": "OPTIMAL", "metrics": {"changed_items": 1}},
                "scoped": {
                    "valid": i == 0,
                    "status": "OPTIMAL" if i == 0 else "UNKNOWN",
                    "metrics": {"changed_items": 1} if i == 0 else None,
                },
            },
        }
        for i in range(2)
    ]
    result = summarize_replan_rows(rows)
    pair = result["pairwise"]["global_standard_vs_global_disturbance"]
    assert pair["denominator"] == 2
    assert pair["metrics"]["global_standard"]["means"]["changed_items"] == 4
    assert result["three_arm_intersection"]["denominator"] == 1
