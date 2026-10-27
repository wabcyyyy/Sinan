"""One repair round; ALL calls charge the original SINAN-NL-01 ledger, never a new allowance."""

import copy
import csv
import hashlib
import json
import time
from datetime import UTC, datetime

from scripts.eval_sinan_nl import ArmClient, run_arm, solve_state
from scripts.run_p4_acceptance import save
from scripts.sinan_nl_budget import NLBudget
from scripts.sinan_nl_cases import _case, public_case
from scripts.sinan_nl_check import evaluate, schedule_checks
from scripts.sinan_nl_freeze import FILES, REPO


def prepare(root):
    original = root.parent
    if not (original / "budget.json").exists() or (root / "design.json").exists():
        raise ValueError("original ledger missing or design already frozen")
    old = json.loads((original / "design.json").read_text("utf8"))["rows"]
    diagnostics = json.loads((original / "diagnostic-design.json").read_text("utf8"))["rows"]
    seen = {r["source_uid"] for r in old}
    easy = list(csv.DictReader((original / "upstream-data/easy.csv").open(encoding="utf8")))
    medium = list(csv.DictReader((original / "upstream-data/medium.csv").open(encoding="utf8")))
    groups = [
        easy[10:14],
        [r for r in medium if r["days"] == "2"][10:14],
        [r for r in medium if r["days"] == "3"][10:14],
    ]
    sources = [groups[j][i] for i in range(4) for j in range(3)]
    main = []
    for index, source in enumerate(sources):
        if source["uid"] in seen:
            raise ValueError("base question was already seen")
        row = _case(original, source, 30 + index)
        row.update(id=f"r26-main-{index:02d}", split="confirm")
        result = solve_state(public_case(row), row["gold"])
        check = schedule_checks(row, row["gold"], result["plans"])
        if result["status"] != "FEASIBLE" or check["errors"]:
            raise ValueError({"id": row["id"], "reference": result["status"], "errors": check["errors"]})
        row["expected"] = "FEASIBLE"
        row["reference_witness"] = result
        main.append(row)
    new_diag = []
    for index, old_index in enumerate((0, 1, 2, 4)):
        row = copy.deepcopy(diagnostics[old_index])
        old_names = [c["name"] for c in row["facts"]["candidates"]]
        names = [f"新匿名馆{index}{suffix}" for suffix in ("甲", "乙", "丙")]
        text = json.dumps(row, ensure_ascii=False)
        for old_name, new_name in zip(old_names, names, strict=True):
            text = text.replace(old_name, new_name)
        row = json.loads(text)
        for i, candidate in enumerate(row["facts"]["candidates"]):
            candidate["price"] = (20, 30, 0)[i]
            candidate["duration_min"] = (45, 60, 90)[i]
            if candidate["opening"] is not None:
                candidate["opening"] = [[570, 1020]]
        a, b, c = names
        budget = row["gold"]["budget"]
        clauses = (
            f"第1天必须先去{a}结束再去{b}；并且第1天必须先去{b}结束再去{a}，两条均为硬要求。{c}全程必去。",
            f"{a}全程必须去。",
            f"{a}全程必去，不得自行补全未知营业事实。",
            f"{c}一定去，{a}和{b}都不要安排。",
        )
        row["nl"] = (
            "1人从2026年11月8日在杭州游玩1天，住宿餐饮另行处理。"
            "每日从9:30开始游览，到17:00结束，至多3个景点，同一地点不重复。"
            f"门票预算硬上限{budget}元人民币，不含城际和市内交通及食宿。{clauses[index]}"
        )
        row["gold"]["start_date"] = "2026-11-08"
        row["gold"]["requirements"]["day_windows"] = [{"day_no": 1, "not_before": "09:30", "finish_by": "17:00"}]
        row.update(id=f"diag-r26-{index:02d}", split="diagnostic")
        row["scope"] = "new anonymous combination; diagnostic grammar family reused, not independent unseen template"
        new_diag.append(row)
    regressions = []
    selected = [
        r
        for r in old
        if r["split"] == "dev"
        or r["id"] in {"ct-poi-11", "ct-poi-17", "ct-poi-22", "ct-poi-23", "ct-poi-26", "ct-poi-29"}
    ]
    for row in selected + diagnostics:
        row = copy.deepcopy(row)
        row["base_id"] = row["id"]
        row["id"] = ("diag-r26-reg-" if row["id"].startswith("diag-") else "r26-reg-") + row["id"]
        row["expected"] = row.get("expected", "FEASIBLE")
        row["split"] = "regression"
        regressions.append(row)
    root.mkdir(parents=True, exist_ok=True)
    save(
        root / "design.json",
        {
            "main": main,
            "diagnostics": new_diag,
            "regressions": regressions,
            "ledger": str(original / "budget.json"),
            "prior_upper_cny": "3.437300",
            "model": "deepseek-flash",
            "max_calls_per_arm": 3,
            "scope": "12 unseen source UIDs, shared templates/city pools; 4 new diagnostic combinations",
        },
    )
    return {"new_main": len(main), "new_diagnostic": len(new_diag), "seen_regression": len(regressions)}


def fingerprint(root):
    paths = set(FILES) | {
        "travel-agent-python/app/agent/editing/clarify_contract.py",
        "travel-agent-python/scripts/eval_nl_repair.py",
    }
    return {name: hashlib.sha256((REPO / name).read_bytes()).hexdigest() for name in sorted(paths)} | {
        "design.json": hashlib.sha256((root / "design.json").read_bytes()).hexdigest()
    }


def freeze(root):
    if (root / "freeze.json").exists():
        raise FileExistsError("repair confirmation already frozen")
    save(root / "freeze.json", {"utc": datetime.now(UTC).isoformat(), "fingerprint": fingerprint(root)})


def execute(root, split, start, stop, *, approved=False):
    from app.common.model_registry import binding

    design = json.loads((root / "design.json").read_text("utf8"))
    if not (root.parent / "budget.json").exists():
        raise ValueError("must reuse original ledger")
    ledger = NLBudget(root.parent / "budget.json", approved=approved)
    cases = design[{"confirm": "main", "diagnostic": "diagnostics", "regression": "regressions"}[split]]
    if split != "regression" and json.loads((root / "freeze.json").read_text("utf8"))["fingerprint"] != fingerprint(
        root
    ):
        raise ValueError("repair confirmation runtime/input changed; do not retune")
    if not 0 <= start < stop <= len(cases) or stop - start > 2:
        raise ValueError("at most two cases per 90-second verification batch")
    output = root / "results"
    output.mkdir(exist_ok=True)
    started = time.monotonic()
    complete = []
    for row in cases[start:stop]:
        for arm in "C" if split == "regression" else "ABC":
            if time.monotonic() - started > 76:
                raise TimeoutError("stop this batch; inspect already saved results")
            target = output / f"{row['id']}-{arm}.json"
            if target.exists():
                continue
            client = ArmClient(ledger, root / "models", binding("main"), f"{row['id']}-{arm}")
            before = time.perf_counter()
            try:
                result = run_arm(public_case(row), arm, client)
            except Exception as exc:
                failure = {"status": "ERROR", "state": {}, "plans": [], "error": repr(exc)}
                result = {"first": failure, "final": failure, "calls": client.calls}
            save(
                target,
                {
                    "id": row["id"],
                    "split": split,
                    "arm": arm,
                    "result": result,
                    "first_evaluation": evaluate(row, result["first"], row["expected"]),
                    "evaluation": evaluate(row, result["final"], row["expected"]),
                    "elapsed_s": time.perf_counter() - before,
                },
            )
            complete.append(target.name)
    return {"completed": complete, "cumulative_upper_cny": str(ledger.total())}
