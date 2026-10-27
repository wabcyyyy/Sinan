"""One serial executor, pre-outcome scope/producer freeze and bounded official score batches."""

import argparse
import csv
import hashlib
import importlib.metadata
import json
import statistics
import time
from datetime import UTC, datetime
from decimal import Decimal

from scripts.chinatravel_original_client import OriginalBudget
from scripts.chinatravel_original_data import OUTPUT, ROOT, SOURCE, prepare_evaluator, save
from scripts.eval_chinatravel_original import one, score


def producer_hashes():
    paths = list((ROOT / "travel-agent-python/app").rglob("*.py"))
    paths += [
        ROOT / "travel-agent-python/scripts" / name
        for name in (
            "eval_chinatravel_original.py",
            "chinatravel_original_data.py",
            "chinatravel_original_client.py",
            "chinatravel_original_batch.py",
        )
    ]
    return {str(p.relative_to(ROOT)).replace("\\", "/"): hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}


def freeze_protocol():
    path = OUTPUT / "protocol.json"
    if path.exists():
        protocol = json.loads(path.read_text("utf8"))
        if protocol["producer_sha256"] != producer_hashes():
            raise ValueError("formal producer changed; do not silently retune confirmation")
        return protocol
    prepare_evaluator()
    manifest = json.loads((OUTPUT / "manifest.json").read_text("utf8"))
    samples = {"intake": [], "attribute": [], "generation": []}
    for response in (OUTPUT / "models").glob("*-response.json"):
        try:
            usage = json.loads(response.read_text("utf8"))["usage"]
        except (KeyError, ValueError):
            continue
        stage = "intake" if "intake" in response.name else "attribute" if "attribute" in response.name else "generation"
        samples[stage].append(Decimal(usage["prompt_tokens"] * 2 + usage["completion_tokens"] * 8) / 1_000_000)
    if any(not values for values in samples.values()):
        raise ValueError("all paid stages need actual small-batch usage before formal scope")
    ledger = OriginalBudget(SOURCE / "budget.json", approved=True)
    # Two NL stages plus A and worst-case one B repair. C uses the same intake and no model.
    mean_task = sum(statistics.mean(v) * (2 if k == "generation" else 1) for k, v in samples.items())
    max_stage_task = sum(max(v) * (2 if k == "generation" else 1) for k, v in samples.items())
    per_task = max(mean_task * Decimal("1.4"), max_stage_task)
    available = Decimal(30) - ledger.total() - Decimal("2.12992")  # Keep one full-context request headroom.
    count = min(300, max(0, int(available / per_task)))
    order = sorted(manifest["all"], key=lambda uid: hashlib.sha256(("formal-v1:" + uid).encode()).hexdigest())
    protocol = {
        "frozen_utc": datetime.now(UTC).isoformat(),
        "phase": "formal-v1",
        "intended_easy_denominator": 300,
        "selected": order[:count],
        "unrun": order[count:],
        "dev": [uid for uid in order[:count] if uid in manifest["dev"]],
        "confirm": [uid for uid in order[:count] if uid in manifest["confirm"]],
        "arms": {
            "A": "one complete generation",
            "B": "shared A then at most one ordinary full-plan feedback repair",
            "C": "same NL intake and public catalog; enhanced existing CP; zero model planning calls",
        },
        "budget": {
            "ledger": str(ledger.path.relative_to(ROOT)),
            "prior_peak_upper": str(ledger.total()),
            "per_task_forecast": str(per_task),
            "forecast_rule": (
                "max(mean-stage total times1.4, sum of each observed stage maximum); not a billing guarantee"
            ),
            "forecast_selected_cny": str(per_task * count),
            "reserve_headroom": "2.12992",
            "cap_cny": "30",
            "sample_stage_costs": {
                k: {"n": len(v), "mean": str(statistics.mean(v)), "max": str(max(v))} for k, v in samples.items()
            },
        },
        "rules": [
            "original NL only; Gold only in unchanged official evaluator",
            "all selected failures stay in denominator",
            "78s per arm, CP30s total/eight timetable pairs/2.5s each solve; no HTTP retry",
            "A/B/C shared source catalog16 attractions/12 restaurants/8 hotels; CP subset4/4-per-day/3",
            "serial UID hash order, no result filtering; developed65 and confirmation235 separate",
            "frozen WorldEnv, not live online booking or human review; continuous browser proof not claimed",
        ],
        "versions": {p: importlib.metadata.version(p) for p in ("ortools", "pandas", "geopy", "tqdm", "pydantic")},
        "manifest_sha256": hashlib.sha256((OUTPUT / "manifest.json").read_bytes()).hexdigest(),
        "producer_sha256": producer_hashes(),
    }
    save(path, protocol)
    return protocol


def run_batch(start, size):
    protocol = freeze_protocol()
    for uid in protocol["selected"][start : start + size]:
        for arm in "ABC":
            try:
                rows = one(uid, protocol["phase"], arm, deadline=time.monotonic() + 78)
            except Exception as exc:
                # Visible provider/orchestration failures stay in the fixed denominator; continue the free arm.
                failed = {
                    "uid": uid,
                    "arm": arm,
                    "phase": protocol["phase"],
                    "plan": {},
                    "issues": [f"orchestration: {type(exc).__name__}: {exc}"],
                    "elapsed_s": 0,
                    "gold_used_by_producer": False,
                    "human_review": "not performed",
                }
                save(OUTPUT / f"{protocol['phase']}/results/{uid}-{arm}.json", failed)
                save(OUTPUT / f"official/results/SinanOriginal-{protocol['phase']}-{arm}/{uid}.json", {})
                rows = [failed]
            print(
                json.dumps({"uid": uid, "arm": arm, "issues": len(rows[0]["issues"]) if rows else "unfinished"}),
                flush=True,
            )
        save(
            OUTPUT / "current.json",
            {
                "uid": uid,
                "next_index": protocol["selected"].index(uid) + 1,
                "utc": datetime.now(UTC).isoformat(),
                "peak_upper_cny": str(OriginalBudget(SOURCE / "budget.json", approved=True).total()),
            },
        )


def score_batch(start, size):
    protocol = freeze_protocol()
    ids = protocol["selected"][start : start + size]
    tag = f"batch{start:03}"
    split = score(ids, protocol["phase"], "ABC", tag=tag)
    rows = []
    for arm in "ABC":
        folder = OUTPUT / f"official/eval_res/splits_{split}/SinanOriginal-{protocol['phase']}-{arm}"
        tables = {}
        for name in ("schema", "commonsense", "logical_py"):
            with (folder / f"{name}.csv").open(encoding="utf-8-sig", newline="") as stream:
                tables[name] = {r["data_id"]: r for r in csv.DictReader(stream)}
        for uid in ids:
            schema = tables["schema"][uid]["schema"] == "1"
            common = all(Decimal(v) == 0 for k, v in tables["commonsense"][uid].items() if k != "data_id" and v)
            logical = all(Decimal(v) == 1 for k, v in tables["logical_py"][uid].items() if k != "data_id" and v)
            record = OUTPUT / f"{protocol['phase']}/results/{uid}-{arm}.json"
            result = json.loads(record.read_text("utf8")) if record.exists() else {"issues": ["not executed"]}
            rows.append(
                {
                    "uid": uid,
                    "arm": arm,
                    "cohort": "dev" if uid in protocol["dev"] else "confirm",
                    "schema": schema,
                    "common": common,
                    "logical": logical,
                    "all_pass": schema and common and logical,
                    "issues": result["issues"],
                    "official_csv_folder": str(folder.relative_to(OUTPUT)),
                }
            )
    save(OUTPUT / f"formal-v1/score-batches/{tag}.json", rows)
    return rows


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("freeze", "run", "score"))
    parser.add_argument("--start", type=int, default=0)
    parser.add_argument("--size", type=int, default=5)
    args = parser.parse_args()
    if args.action == "freeze":
        p = freeze_protocol()
        print(json.dumps({"selected": len(p["selected"]), "confirm": len(p["confirm"]), "budget": p["budget"]}))
    elif args.action == "run":
        run_batch(args.start, args.size)
    else:
        score_batch(args.start, args.size)
