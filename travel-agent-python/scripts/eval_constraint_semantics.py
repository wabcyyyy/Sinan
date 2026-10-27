"""带独立原话标注的抽取输出回放；含故意漏提取负对照，不测模型泛化。"""

import argparse
import json
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from app.agent.editing import clarify
from app.agent.generation.content.constraint_solver import solve_day
from app.schemas.trip import ClarifyRequest
from app.schemas.trip_requirements import IntakeState
from tests.constraint_planner_cases import frozen_problem
from tests.constraint_planner_oracle import schedule_violations


def evaluate(output):
    relation = {
        "constraint_id": "raw-order",
        "kind": "before",
        "place_names": ["地点0", "地点1"],
        "day_no": 1,
        "strength": "hard",
    }
    cases = [
        {"id": "before", "raw": "第1天必须先去地点0再去地点1", "relation": relation, "expected": "before"},
        {
            "id": "at_least_one",
            "raw": "第1天地点0和地点1至少去一个",
            "relation": {**relation, "kind": "one_of"},
            "expected": "one_of",
        },
        {"id": "conditional", "raw": "如果下雨则第1天不去地点0", "expected": "unresolved"},
        {"id": "omission_control", "raw": "第1天必须先去地点0再去地点1", "expected": "before", "omit": True},
    ]
    rows = []
    for case in cases:
        base = frozen_problem()
        assert base.budget is not None
        req = base.requirements.model_copy(deep=True)
        req.place_relations = []
        state = IntakeState(city="合成城市", days=1, persons=1, budget=float(base.budget), requirements=req)
        patches = (
            []
            if case.get("omit")
            else [
                {
                    "op": "add",
                    "target": "place_relation" if "relation" in case else "unresolved_request",
                    "source_text": case["raw"],
                    "relation": case.get("relation"),
                    "value": None if "relation" in case else case["raw"],
                }
            ]
        )
        with patch.object(clarify, "_ask", return_value=json.dumps({"patches": patches})):
            extracted = clarify.run_clarify(ClarifyRequest(message=case["raw"], state=state)).state.requirements
        result = solve_day(replace(base, requirements=extracted))
        # 判官根据预先固定的原话语义，而不是模型给出的 provenance/编译器 pass。
        semantics_kept = (
            bool(extracted.unresolved_requests)
            if case["expected"] == "unresolved"
            else any(
                r.kind == case["expected"]
                and r.place_names == ["地点0", "地点1"]
                and r.strength == "hard"
                and r.day_no == 1
                for r in extracted.place_relations
            )
        )
        errors = schedule_violations(replace(base, requirements=extracted), result.visits)
        rows.append(
            {
                "id": case["id"],
                "raw": case["raw"],
                "negative_control": bool(case.get("omit")),
                "semantics_kept": semantics_kept,
                "status": result.status,
                "checker_errors": errors,
                "false_semantic_success": not semantics_kept and result.status in {"OPTIMAL", "FEASIBLE"},
                "trace": [s.model_dump() for s in extracted.requirement_sources],
            }
        )
    report = {
        "mode": "frozen extraction replay, not real-model accuracy",
        "rows": rows,
        "annotated_supported_requirements": 2,
        "supported_extracted": sum(r["semantics_kept"] for r in rows[:2]),
        "unsupported_retained": rows[2]["semantics_kept"],
        "omission_detected": not rows[3]["semantics_kept"],
        "new_model_calls": 0,
        "new_http_requests": 0,
    }
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists():
        raise FileExistsError(output)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(evaluate(args.output), ensure_ascii=False))
