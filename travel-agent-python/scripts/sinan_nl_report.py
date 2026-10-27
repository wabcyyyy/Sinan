"""Summarize immutable raw outputs with a named evaluation version; never call a model."""

import json
from collections import Counter
from decimal import Decimal
from pathlib import Path

from scripts.run_p4_acceptance import save
from scripts.sinan_nl_check import evaluate


def summarize(root):
    root = Path(root)
    design = json.loads((root / "design.json").read_text("utf8"))
    diagnostic = json.loads((root / "diagnostic-design.json").read_text("utf8"))
    cases = {row["id"]: row for row in design["rows"] + diagnostic["rows"]}
    expected = {row["id"]: row["expected"] for row in json.loads((root / "reference.json").read_text("utf8"))["rows"]}
    expected.update({row["id"]: row["expected"] for row in diagnostic["rows"]})
    rows = []
    for directory in ("results", "dev-calibrated", "dev-calibrated2"):
        for path in sorted((root / directory).glob("*.json")):
            raw = json.loads(path.read_text("utf8"))
            row = cases[raw["id"]]
            first = evaluate(row, raw["result"]["first"], expected[row["id"]])
            final = evaluate(row, raw["result"]["final"], expected[row["id"]])
            rows.append(
                {
                    "id": row["id"],
                    "split": raw["split"],
                    "phase": directory,
                    "family": row["family"],
                    "arm": raw["arm"],
                    "first": first,
                    "final": final,
                    "logical_calls": raw["result"]["calls"],
                    "elapsed_s": raw["elapsed_s"],
                    "raw_path": str(path),
                }
            )
    groups = {}
    for split, phase in (
        ("dev", "results"),
        ("dev", "dev-calibrated"),
        ("dev", "dev-calibrated2"),
        ("confirm", "results"),
        ("diagnostic", "results"),
    ):
        group = {}
        for arm in "ABC":
            selected = [row for row in rows if row["split"] == split and row["phase"] == phase and row["arm"] == arm]
            final = [row["final"] for row in selected]
            tp = sum(e["semantics"]["tp"] for e in final)
            predicted = sum(e["semantics"]["predicted"] for e in final)
            gold = sum(e["semantics"]["gold"] for e in final)
            checks = [c for e in final for c in e["schedule"]["checks"]]
            group[arm] = {
                "n": len(selected),
                "first_passed": sum(row["first"]["passed"] for row in selected),
                "final_passed": sum(e["passed"] for e in final),
                "hard_complete": sum(e["semantics"]["hard_complete"] for e in final),
                "hard_preserved_and_passed": sum(e["hard_preserved_and_passed"] for e in final),
                "false_feasible": sum(e["false_feasible"] for e in final),
                "constraint_tp": tp,
                "constraint_predicted": predicted,
                "constraint_gold": gold,
                "precision": tp / predicted if predicted else 0,
                "recall": tp / gold if gold else 0,
                "hard_soft_errors": sum(e["semantics"]["hard_soft_errors"] for e in final),
                "individual_checks_passed": sum(c["passed"] for c in checks),
                "individual_checks_total": len(checks),
                "statuses": dict(Counter(e["status"] for e in final)),
                "logical_calls": sum(row["logical_calls"] for row in selected),
                "elapsed_total_s": sum(row["elapsed_s"] for row in selected),
                "failures": [
                    {
                        "id": row["id"],
                        "errors": row["final"]["schedule"]["errors"],
                        "missing": row["final"]["semantics"]["missing"],
                        "extra": row["final"]["semantics"]["extra"],
                        "status": row["final"]["status"],
                    }
                    for row in selected
                    if not row["final"]["hard_preserved_and_passed"]
                ],
            }
        groups[f"{split}/{phase}"] = group
    ledger = json.loads((root / "budget.json").read_text("utf8"))
    records = ledger["records"]
    fee = {
        "cap_cny": ledger["cap_cny"],
        "known_peak_upper_cny": str(sum((Decimal(r.get("settled_peak_cny", "0")) for r in records), Decimal(0))),
        "unknown_reserved_cny": str(
            sum((Decimal(r["reserved_cny"]) for r in records if r["status"] != "usage_known"), Decimal(0))
        ),
        "total_peak_upper_cny": ledger["total_peak_upper_cny"],
        "records": len(records),
        "usage_known": sum(r["status"] == "usage_known" for r in records),
        "prompt_tokens": sum(r.get("usage", {}).get("prompt_tokens", 0) for r in records),
        "completion_tokens": sum(r.get("usage", {}).get("completion_tokens", 0) for r in records),
        "supplier_invoice_checked": False,
    }
    report = {
        "evaluation_version": "3-single-day-must-scope-equivalence",
        "groups": groups,
        "fees": fee,
        "rows": rows,
        "note": "No new model calls; raw first evaluations, including the numeric mismatch, remain immutable.",
    }
    target = root / "summary.json"
    save(target, report)
    return {"groups": groups, "fees": fee}


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(summarize(args.root), ensure_ascii=True))
