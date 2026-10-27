"""Explicit benchmark execution through the repository's bounded validator."""

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import eval_expansion_model
from scripts.eval_benchmark_expansion import run_kernel, run_poi, run_replan
from scripts.eval_expansion_model import ExpansionBudget


def benchmark_root():
    value = os.environ.get("SINAN_EXPANSION_ROOT")
    if not value:
        pytest.skip("expanded benchmark is explicitly invoked; not a routine full-suite rerun")
    return Path(value)


def test_replan_expansion():
    root = benchmark_root()
    start = int(os.environ.get("SINAN_EXPANSION_START", "0"))
    stop = int(os.environ.get("SINAN_EXPANSION_STOP", "15"))
    rows = run_replan(root, start, stop)
    assert len(rows) == stop - start
    assert all(not r["baseline_errors"] for r in rows)
    for row in rows:
        for arm in row["arms"].values():
            assert arm["status"] != "ERROR", arm
            assert not arm["checker_errors"], arm
            if arm["feasible_checked"]:
                assert arm["metrics"]["locked_violations"] == arm["outside_selected_scope"] == 0


def test_kernel_expansion():
    rows = run_kernel(benchmark_root())
    assert len(rows) == 54
    assert all(r["classification_correct"] for r in rows), [r for r in rows if not r["classification_correct"]]
    assert all(not r.get("checker_errors") for r in rows)
    assert all(r.get("oracle_matches_label", True) for r in rows)
    assert all(r["core_proof"]["independently_irreducible"] for r in rows if r["status"] == "INFEASIBLE")


def test_poi_expansion():
    result = run_poi(benchmark_root())
    assert result["cases"] == 48
    assert result["rules_dirty_removed"] == result["dirty_total"]
    assert result["rules_valid_removed"] == 0


def test_model_budget_prevents_duplicate_and_keeps_unknown_usage(tmp_path):
    ledger = ExpansionBudget(tmp_path / "budget.json")
    payload = {"model": "deepseek-flash", "thinking": {"type": "disabled"}, "max_tokens": 1536}
    ledger.reserve("case-0", payload)
    before = ledger.total()
    with pytest.raises(ValueError, match="already issued"):
        ledger.reserve("case-0", payload)
    assert ExpansionBudget(ledger.path).total() == before
    for i in range(1, 4):
        ledger.reserve(f"case-{i}", payload)
    with pytest.raises(ValueError, match="CNY10"):
        ledger.reserve("case-4", payload)
    ledger.settle("case-0", {"usage": {"prompt_tokens": 100, "completion_tokens": 100}})
    assert ledger.total() < before * 4


@pytest.mark.parametrize("start", [0, 1])
def test_model_batch_identity_uses_index_and_rejects_completed_overlap(tmp_path, monkeypatch, start):
    """A feasible/infeasible pair can share a descriptive label, but never a paid request identity."""
    design = {
        "cases": [{"id": "same-label", "facts": {}, "expected": "UNKNOWN"}] * 30,
        "system_prompt": "synthetic",
        "temperature": 0.3,
        "max_tokens": 1536,
    }
    (tmp_path / "model-design.json").write_text(json.dumps(design), encoding="utf8")
    (tmp_path / "model-00-01.json").write_text(json.dumps({"rows": [{}]}), encoding="utf8")
    issued = []

    class Client:
        def __init__(self, *args):
            self.identifier = None

        def complete(self, *_args, **_kwargs):
            issued.append(self.identifier)
            return "{}"

    monkeypatch.setattr(eval_expansion_model, "NightClient", Client)
    monkeypatch.setattr("app.common.model_registry.binding", lambda _: SimpleNamespace())
    monkeypatch.setattr(eval_expansion_model, "assess", lambda *_: {"correct": True})
    if start == 0:
        with pytest.raises(ValueError, match="already issued"):
            eval_expansion_model.execute(tmp_path, 0, 2, approved=True)
        assert issued == []
    else:
        eval_expansion_model.execute(tmp_path, 1, 2, approved=True)
        assert issued == ["case-01-same-label"]
