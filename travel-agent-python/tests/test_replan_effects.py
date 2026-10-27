"""新效果批次只执行一次，并检查独立 checker 能拒绝原日窗破坏。"""

import copy
import os
from pathlib import Path

from app.agent.generation.orchestration.replan_snapshot import create_snapshot
from app.schemas.replanning import ReplanChange
from scripts.eval_replan_effects import BASES, environment, evaluate, independent_errors


def test_grouped_comparison_records_every_outcome_and_pairs_only_valid_plans(tmp_path):
    output = Path(os.environ.get("SINAN_EFFECT_REPORT_DIR", str(tmp_path / "effects")))
    report = evaluate(output)
    assert len(report["rows"]) == 36
    assert len({r["base_id"] for r in report["rows"]}) == 6
    for split, summary in report["summary"].items():
        paired = [
            r
            for r in report["rows"]
            if r["split"] == split and r["local"]["feasible_checked"] and r["global"]["feasible_checked"]
        ]
        assert summary["both_feasible"] == len(paired)
        assert sum(summary["changed_items_wins_ties_losses"]) == len(paired)
        assert all(not r[arm]["checker_errors"] for r in paired for arm in ("local", "global"))
        assert all(r[arm]["metrics"]["locked_violations"] == 0 for r in paired for arm in ("local", "global"))


def test_independent_checker_rejects_day_window_violation_without_compiler():
    request, before, routes, completed, optional = environment(BASES[0])
    event = ReplanChange(kind="prefer", item_id=4)
    matrix = create_snapshot(request, before, event, completed, routes)[5]
    assert independent_errors(request, before, before, event, {1}, matrix, optional) == []
    after = copy.deepcopy(before)
    after[0]["items"][-1].update(start_time="19:00", end_time="19:30")
    errors = independent_errors(request, before, after, event, {1}, matrix, optional)
    assert "超出需求日窗" in errors and "不在冻结营业窗" in errors
