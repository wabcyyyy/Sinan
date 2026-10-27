"""Budget allocation and real prior failures, without increasing per-arm time."""

import os
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.agent.generation.orchestration import constraint_replanning as planner
from scripts.eval_night_replan import execute


def test_expired_local_search_is_not_reported_as_global_infeasible():
    attempts, chosen, conflicts = planner._find_scope([], [], None, [("trip", {1, 2})], time.monotonic() - 1, None, [])
    assert chosen is None and attempts[0].status == "UNKNOWN" and attempts[0].seconds == 0
    assert "未决" in conflicts[0]


def test_scope_reserves_time_and_skips_intermediate_core_shrink(monkeypatch):
    calls = []

    def solve(*_, **kwargs):
        calls.append(kwargs)
        return [SimpleNamespace(status="INFEASIBLE", report=SimpleNamespace(checks=[]))]

    monkeypatch.setattr(planner, "solve_trip", solve)
    planner._find_scope(
        [], [], None, [("local", {1}), ("day", {1, 2}), ("trip", {1, 2, 3})], time.monotonic() + 0.6, None, []
    )
    assert len(calls) == 3
    assert calls[0]["time_limit_s"] <= 0.201
    assert [c["shrink_conflicts"] for c in calls] == [False, False, True]


def test_repaired_replan_batch():
    root = os.environ.get("SINAN_NIGHT_REPLAN_ROOT")
    if not root:
        pytest.skip("explicit experiment output required")
    indexes = [int(s) for s in os.environ["SINAN_NIGHT_REPLAN_INDEXES"].split(",")]
    rows = execute(Path(root), indexes)
    assert len(rows) == len(indexes)
    for row in rows:
        arm = row["arms"]["scoped"]
        assert not arm.get("checker_errors"), arm
        if arm["feasible_checked"]:
            assert arm["metrics"]["locked_violations"] == arm["outside_selected_scope"] == 0
