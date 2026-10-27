"""归因消融使用真实内核；各臂的失败和未完成证明必须保留。"""

import json
import os
from pathlib import Path

from app.schemas.replanning import ReplanChange
from app.schemas.trip import GenerateRequest
from scripts.align_p4_scenario import local_only
from scripts.eval_replan_ablation import ARMS, evaluate, run_arm
from scripts.eval_replan_effects import BASES, environment, events, independent_errors


def test_single_arm_audit_retains_replayable_inputs_and_proposal():
    spec = BASES[0]
    event = events(spec)[0]
    with local_only():
        result = run_arm(spec, event, "global_disturbance", include_audit=True)
    # The persisted form must be sufficient to rerun the checker without a solver.
    stored = json.loads(json.dumps(result))
    source = stored["audit_inputs"]
    assert stored["feasible_checked"]
    assert source["before"] == environment(spec)[1]
    assert source["event"] == event.model_dump(mode="json")
    matrix = {(r["from"], r["to"]): r["route"] for r in source["matrix"]}
    errors = independent_errors(
        GenerateRequest.model_validate(source["request"]),
        source["before"],
        stored["proposal"],
        ReplanChange.model_validate(source["event"]),
        source["protected"],
        matrix,
        source["optional"],
    )
    assert errors == stored["checker_errors"] == []
    assert source["protected"] == [1]
    assert stored["metrics"]["locked_violations"] == 0


def test_four_arm_ablation_records_scope_hint_and_solver_stages(tmp_path):
    report = evaluate(Path(os.environ.get("SINAN_ABLATION_REPORT_DIR", str(tmp_path / "ablation"))), repeats=3)
    assert len(report["rows"]) == 108
    for row in report["rows"]:
        for arm in ARMS:
            result = row[arm]
            assert result["status"] != "ERROR", result
            assert not result["checker_errors"], result
            if result["feasible_checked"]:
                assert result["metrics"]["locked_violations"] == result["outside_selected_scope"] == 0
            stages = {t["stage"] for solve in result["solves"] for t in solve["timings"]}
            assert {"compile", "feasibility"} <= stages
    assert report["summary"]["evaluation_hard_observations"] == 60
