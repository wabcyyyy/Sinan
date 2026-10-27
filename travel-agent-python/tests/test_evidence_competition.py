import os
from pathlib import Path

from scripts.eval_evidence_competition import evaluate


def test_competing_gaps_budget_and_full_factorial_outcomes_are_retained(tmp_path):
    result = evaluate(Path(os.environ.get("SINAN_COMPETITION_REPORT_DIR", str(tmp_path / "competition"))))
    assert len(result["rows"]) == 48
    assert {r["template"] for r in result["rows"]} == {"choice", "departure"}
    assert all(len(r["calls"]) <= 2 and r["report"]["spent_units"] <= 2 for r in result["rows"])
    assert all(not r["report"]["llm_calls"] for r in result["rows"])
    assert all(s["runs"] == (12 if s["strategy"] == "random" else 4) for s in result["summary"])
