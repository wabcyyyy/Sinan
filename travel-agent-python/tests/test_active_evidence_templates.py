"""新约束模板的冻结执行与直接判官；复用既有 P4 策略，不按结果调权。"""

import json

from tests.active_evidence_templates import evaluate


def test_new_constraint_structures_against_independent_rule_oracle(tmp_path):
    report = evaluate()
    (tmp_path / "p4-independent-templates.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    assert len(report["rows"]) == 18
    assert all(r["passed"] for r in report["rows"]), [
        (r["case"], r["strategy"], r["decision"]) for r in report["rows"] if not r["passed"]
    ]
    assert all(r["report"]["spent_units"] <= 2 and len(r["report"]["queries"]) <= 2 for r in report["rows"])
    assert all(r["report"]["llm_calls"] == 0 and r["report"]["retrieval_calls"] == 0 for r in report["rows"])
    excluded = next(
        r for r in report["rows"] if r["case"] == "excluded-choice-no-query" and r["strategy"] == "bottleneck"
    )
    assert excluded["report"]["queries"] == [] and excluded["report"]["stop_reason"] == "decision_stable"
