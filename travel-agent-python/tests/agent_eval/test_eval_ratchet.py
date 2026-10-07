"""指标棘轮单测（PR-0）：双向棘轮的红绿语义（scripts/eval_ratchet.py）。

钉住三条：
1. 回归（低于下限 / 越过上限）→ 红；
2. 显著变好 → 也红，逼 --update 认账收紧（基线只跟不松，倒退藏不进旧基线）；
3. --update 只重写界值、保留方向键与 note；未冻结的新指标一并冻结。
"""

from __future__ import annotations

import json
from pathlib import Path

from scripts import eval_ratchet

REPORT = "report/offline/report.json"


def _baseline(reports: dict | None = None) -> dict:
    return {
        "_meta": {"frozen_at": "2026-09-23"},
        "reports": reports
        if reports is not None
        else {REPORT: {"up_rate": {"min": 0.8}, "down_rate": {"max": 0.1}, "pinned": {"min": 3.0, "max": 3.0}}},
    }


def _current(metrics: dict | None = None) -> dict:
    return {REPORT: {"metrics": metrics if metrics is not None else {"up_rate": 0.8, "down_rate": 0.1, "pinned": 3.0}}}


def test_within_bounds_passes() -> None:
    assert eval_ratchet.check(_current(), _baseline()) == []


def test_below_floor_is_regression() -> None:
    problems = eval_ratchet.check(_current({"up_rate": 0.5, "down_rate": 0.1, "pinned": 3.0}), _baseline())
    assert any("up_rate" in problem and "回归" in problem for problem in problems)


def test_above_ceiling_is_regression() -> None:
    problems = eval_ratchet.check(_current({"up_rate": 0.8, "down_rate": 0.4, "pinned": 3.0}), _baseline())
    assert any("down_rate" in problem and "回归" in problem for problem in problems)


def test_improvement_on_floor_metric_forces_update() -> None:
    problems = eval_ratchet.check(_current({"up_rate": 0.9, "down_rate": 0.1, "pinned": 3.0}), _baseline())
    assert any("up_rate" in problem and "变好" in problem for problem in problems)


def test_improvement_on_ceiling_metric_forces_update() -> None:
    problems = eval_ratchet.check(_current({"up_rate": 0.8, "down_rate": 0.0, "pinned": 3.0}), _baseline())
    assert any("down_rate" in problem and "变好" in problem for problem in problems)


def test_pinned_metric_flags_drift_in_either_direction() -> None:
    for drifted in (2.9, 3.1):
        problems = eval_ratchet.check(_current({"up_rate": 0.8, "down_rate": 0.1, "pinned": drifted}), _baseline())
        assert any("pinned" in problem for problem in problems), drifted


def test_tolerance_absorbs_noise_only() -> None:
    baseline = _baseline({REPORT: {"up_rate": {"min": 0.8, "tolerance": 0.01}}})
    assert eval_ratchet.check(_current({"up_rate": 0.795}), baseline) == []
    assert eval_ratchet.check(_current({"up_rate": 0.75}), baseline)


def test_unfrozen_and_missing_metrics_are_flagged() -> None:
    problems = eval_ratchet.check(
        _current({"up_rate": 0.8, "down_rate": 0.1, "pinned": 3.0, "new_metric": 1.0}), _baseline()
    )
    assert any("new_metric" in problem and "未冻结" in problem for problem in problems)

    problems = eval_ratchet.check(_current({"up_rate": 0.8}), _baseline())
    assert any("down_rate" in problem and "消失" in problem for problem in problems)


def test_missing_report_and_non_numeric_values_are_flagged() -> None:
    assert eval_ratchet.check({REPORT: {"metrics": None}}, _baseline())
    problems = eval_ratchet.check(_current({"up_rate": "high", "down_rate": 0.1, "pinned": 3.0}), _baseline())
    assert any("up_rate" in problem and "非数值" in problem for problem in problems)


def test_update_freezes_bounds_keeps_notes_and_freezes_new_metrics() -> None:
    baseline = _baseline(
        {
            REPORT: {
                "up_rate": {"min": 0.5, "note": "越高越好"},
                "down_rate": {"max": 0.2},
                "pinned": {"min": 3.0, "max": 3.0},
            }
        }
    )
    current = _current({"up_rate": 0.9, "down_rate": 0.1, "pinned": 3.0, "new_metric": 7})
    rewritten = eval_ratchet.update(current, baseline)
    bounds = rewritten["reports"][REPORT]
    assert bounds["up_rate"] == {"min": 0.9, "note": "越高越好"}
    assert bounds["down_rate"] == {"max": 0.1}
    assert bounds["pinned"] == {"min": 3.0, "max": 3.0}
    assert bounds["new_metric"]["min"] == 7
    assert rewritten["_meta"] == baseline["_meta"]
    # 更新后的基线对同一份报告应当全绿（棘轮咬合在新值上）
    assert eval_ratchet.check(current, rewritten) == []


def test_main_checks_and_updates_via_cli(tmp_path: Path) -> None:
    report_dir = tmp_path / "report" / "offline"
    report_dir.mkdir(parents=True)
    (report_dir / "report.json").write_text(json.dumps({"metrics": {"up_rate": 0.9}}), encoding="utf-8")
    baseline_path = tmp_path / "metrics_baseline.json"
    baseline_path.write_text(json.dumps(_baseline({REPORT: {"up_rate": {"min": 0.5}}})), encoding="utf-8")
    argv = ["eval_ratchet", "--baseline", str(baseline_path)]
    # 变好未认账 → 红；--update 认账后 → 绿
    assert eval_ratchet.main(argv) == 1
    assert eval_ratchet.main([*argv, "--update"]) == 0
    assert eval_ratchet.main(argv) == 0
    frozen = json.loads(baseline_path.read_text(encoding="utf-8"))
    assert frozen["reports"][REPORT]["up_rate"]["min"] == 0.9


def test_main_fails_when_baseline_missing(tmp_path: Path) -> None:
    assert eval_ratchet.main(["eval_ratchet", "--baseline", str(tmp_path / "nope.json")]) == 1


def test_coordinate_improvement_cannot_fall_back_to_old_floor():
    baseline = json.loads(eval_ratchet.BASELINE.read_text(encoding="utf-8"))
    current = eval_ratchet.load_reports(baseline, eval_ratchet.BASELINE.parent)
    for metric in ("coord_valid_rate", "poi_grounded_rate"):
        current[REPORT]["metrics"][metric] = 0.6528
    problems = eval_ratchet.check(current, baseline)
    for metric in ("coord_valid_rate", "poi_grounded_rate"):
        assert any(metric in problem and "回归" in problem for problem in problems)
