"""Authorized 30 synthetic single-shot calls; CNY10 cumulative cap, no retry or tool calls."""

import argparse
import json
import time
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from scripts.eval_planning_baselines import SYSTEM, assess
from scripts.p4_acceptance_budget import model_upper
from scripts.run_p4_acceptance import save
from scripts.sinan_night_budget import NightBudget, NightClient


class ExpansionBudget(NightBudget):
    """New explicit authorization; does not reuse/reset the expired night-run ledger."""

    def __init__(self, path):
        self.path = Path(path)
        self.state = (
            json.loads(self.path.read_text("utf8"))
            if self.path.exists()
            else {
                "cap_cny": "10",
                "carried_peak_upper_cny": "0",
                "records": [],
                "authorization": "current user: synthetic questions only, <=30 calls, no retry, <=CNY10",
                "pricing_url": "https://api-docs.deepseek.com/zh-cn/quick_start/pricing/",
                "supplier_invoice_checked": False,
            }
        )
        if self.state["cap_cny"] != "10":
            raise ValueError("budget cap changed")

    def reserve(self, identifier, payload):
        if len(self.state["records"]) >= 30 or any(r["id"] == identifier for r in self.state["records"]):
            raise ValueError("request count exhausted or request already issued; no retry")
        if payload.get("model") != "deepseek-flash" or payload.get("thinking") != {"type": "disabled"}:
            raise ValueError("unpriced model or thinking mode")
        if payload.get("max_tokens") != 1536 or any(k in payload for k in ("tools", "functions", "stream")):
            raise ValueError("unapproved output/tool configuration")
        upper = model_upper(1536)
        if self.total() + upper > Decimal(10):
            raise ValueError("CNY10 cumulative bound would be exceeded")
        self.state["records"].append(
            {
                "id": identifier,
                "reserved_cny": str(upper),
                "status": "issued_usage_unknown",
                "issued_at": datetime.now(UTC).isoformat(),
            }
        )
        save(self.path, {**self.state, "total_peak_upper_cny": str(self.total())})


def prepare_model(root):
    root = Path(root)
    target = root / "model-design.json"
    if target.exists():
        raise ValueError("model design frozen already")
    design = json.loads((root / "design.json").read_text("utf8"))
    cases = [
        {"id": r["spec"]["id"], "facts": r["facts"], "expected": r["spec"]["expected"]}
        for r in design["kernel"]
        if r["spec"]["days"] == 1
    ]
    assert len(cases) == 30
    save(
        target,
        {
            "cases": cases,
            "system_prompt": SYSTEM,
            "temperature": 0.3,
            "max_tokens": 1536,
            "max_calls": 30,
            "hard_cap_cny": "10",
            "repeats": 1,
        },
    )
    return {"cases": len(cases), "maximum_per_call_reservation_cny": str(model_upper(1536))}


def execute(root, start, stop, *, approved=False):
    from app.common.model_registry import binding

    if not approved:
        raise ValueError("explicit authorization required")
    root = Path(root)
    design = json.loads((root / "model-design.json").read_text("utf8"))
    if not 0 <= start < stop <= 30:
        raise ValueError("batch outside frozen 30 cases")
    output = root / f"model-{start:02}-{stop:02}.json"
    if output.exists():
        raise ValueError("batch already complete")
    ledger = ExpansionBudget(root / "model-budget.json")
    client = NightClient(ledger, root / "models", binding("main"))
    completed_indexes = set()
    for previous in root.glob("model-??-??.json"):
        offset = int(previous.name.split("-")[1])
        count = len(json.loads(previous.read_text("utf8"))["rows"])
        completed_indexes.update(range(offset, offset + count))
    rows, deadline = [], time.monotonic() + 65
    for index, case in enumerate(design["cases"][start:stop], start):
        if time.monotonic() >= deadline:
            raise TimeoutError("batch deadline reached; no more requests")
        # Legacy labels repeat for feasible/infeasible budget variants; index is the frozen identity.
        client.identifier = f"case-{index:02}-{case['id']}"
        if index in completed_indexes or any(r["id"] == client.identifier for r in ledger.state["records"]):
            raise ValueError("case was already issued; inspect recorded response, do not retry")
        try:
            raw = client.complete(
                json.dumps(case["facts"], ensure_ascii=False),
                system_prompt=design["system_prompt"],
                temperature=design["temperature"],
                max_tokens=design["max_tokens"],
            )
            row = {
                "id": case["id"],
                "case_index": index,
                "request_id": client.identifier,
                "expected": case["expected"],
                "assessment": assess(case, raw),
            }
        except Exception as exc:
            row = {
                "id": case["id"],
                "expected": case["expected"],
                "assessment": {"correct": False, "greedy_correct": False, "status": "ERROR", "error": repr(exc)},
            }
        rows.append(row)
        save(output.with_suffix(".partial.json"), {"rows": rows})
    save(output, {"rows": rows, "peak_upper_cny": str(ledger.total())})
    return {
        "calls": len(rows),
        "correct": sum(r["assessment"]["correct"] for r in rows),
        "peak_upper_cny": str(ledger.total()),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--prepare", action="store_true")
    parser.add_argument("--start", type=int, default=0)
    parser.add_argument("--stop", type=int, default=10)
    parser.add_argument("--approved", action="store_true")
    args = parser.parse_args()
    print(
        json.dumps(
            prepare_model(args.root)
            if args.prepare
            else execute(args.root, args.start, args.stop, approved=args.approved),
            ensure_ascii=False,
        )
    )
