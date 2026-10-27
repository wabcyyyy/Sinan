"""Frozen Human154 first pass: four isolated UID processes, one official scorer, one locked ledger."""

import argparse
import csv
import hashlib
import importlib.metadata
import json
import multiprocessing
import os
import shutil
import subprocess
import sys
import time
import zipfile
from collections import Counter
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import UTC, datetime
from functools import partial

from app.common.llm_client import _apply_provider_options
from scripts.chinatravel_original_client import OriginalBudget, OriginalClient
from scripts.chinatravel_original_data import CODE, ROOT, SOURCE, _source_uids, queries, save
from scripts.sinan_budget_lock import atomic_ledger_save, ledger_lock
from scripts.sinan_nl_budget import HUMAN_UNLIMITED

OUTPUT = ROOT / "docs/benchmarks/sinan-chinatravel-human-20261026"
WORK = OUTPUT / "official"
PHASE = "human-v1"
PROGRESS = ROOT / "docs/design/sinan-chinatravel-human-progress-20261026.md"
RUNNER = None


def human_queries():
    return queries("human")


def prepared():
    if not (WORK / "eval_exp.py").exists():
        raise ValueError("prepare and freeze Human assets before starting workers")
    return WORK


def prepare():
    rows = human_queries()
    if len(rows) != 154:
        raise ValueError("Human154 denominator changed")
    known = {"h20241029143447759844", "h20241029143450090032", "h20241029143451793119"}
    sources = {}
    registry = ROOT / "docs/benchmarks/sinan-chinatravel-original-20261026/manual-seen-registry.json"
    sources[str(registry.relative_to(ROOT))] = json.loads(registry.read_text("utf8"))["uids"]
    for path in (SOURCE / "design.json", SOURCE / "repair-20261026/design.json"):
        sources[str(path.relative_to(ROOT))] = sorted(_source_uids(json.loads(path.read_text("utf8"))))
    for values in sources.values():
        known.update(values)
    ids = {r["uid"] for r in rows}
    manifest = {
        "dataset": "official Human154, unchanged Chinese original tasks",
        "all": [r["uid"] for r in rows],
        "seen": sorted(ids & known),
        "unseen_recorded": sorted(ids - known),
        "seen_registry_sources": sources,
        "seen_user_task": "three UIDs named in approved Human154 task",
        "seen_is_not_human_review": True,
        "days": {str(k): v for k, v in Counter(r["days"] for r in rows).items()},
        "source_csv_sha256": hashlib.sha256((SOURCE / "upstream-data/human.csv").read_bytes()).hexdigest(),
        "source_versions": json.loads((SOURCE / "data-version.json").read_text("utf8")),
        "code_version": json.loads((SOURCE / "source-version.json").read_text("utf8")),
        "no_easy_overlap": not ids & {r["uid"] for r in queries()},
    }
    path = OUTPUT / "manifest.json"
    if path.exists() and json.loads(path.read_text("utf8")) != manifest:
        raise ValueError("Human manifest already frozen")
    save(path, manifest)
    if not WORK.exists():
        shutil.copytree(CODE, WORK, ignore=shutil.ignore_patterns(".git", "__pycache__", "database"))
    database = WORK / "chinatravel/environment/database"
    source_database = SOURCE / "upstream-data/sandbox/database"
    if not database.exists():
        if os.name == "nt":
            subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(database), str(source_database)], check=True, capture_output=True
            )
        else:
            database.symlink_to(source_database, target_is_directory=True)
    for relative in (
        "eval_exp.py",
        "chinatravel/evaluation/hard_constraint.py",
        "chinatravel/symbol_verification/commonsense_constraint.py",
        "chinatravel/evaluation/output_schema.json",
    ):
        if (WORK / relative).read_bytes() != (CODE / relative).read_bytes():
            raise ValueError("official source/checker changed")
    return manifest


def authorize():
    manifest = prepare()
    path = SOURCE / "budget.json"
    with ledger_lock(path):
        state = json.loads(path.read_text("utf8"))
        if state.get("active_authorization", {}).get("id") == HUMAN_UNLIMITED:
            if state["cap_cny"] is not None or state.get("cap_mode") != "unlimited":
                raise ValueError("unlimited authorization exists but actual cap is inconsistent")
            if state["active_authorization"]["uids"] != manifest["all"]:
                raise ValueError("unlimited scope changed")
            return state["active_authorization"]
        if state["total_peak_upper_cny"] != "27.960330":
            raise ValueError("unexpected shared ledger change before Human authorization")
        authority = {
            "id": HUMAN_UNLIMITED,
            "approved_at_utc": datetime.now(UTC).isoformat(),
            "human_statement": "我明白了，帮我测human类型吧，全量测，无预算上限。",
            "concurrency_statement": "能不能并发处理加快速度",
            "scope": "Human154 first A/B/C and the explicitly requested anonymous product week7 proof",
            "product_extension_statement": "项目应该至少支持1周内；先完成共享1—7天及真实NL/保存读回证明再测Human",
            "proof_prefixes": ["week7-proof-v1-anonymous7d"],
            "uids": manifest["all"],
            "previous_cap_cny": state["cap_cny"],
            "prior_peak_upper_cny": state["total_peak_upper_cny"],
            "prior_unknown_reserved_cny": "10.626432",
            "prior_records_count": len(state["records"]),
            "prior_records_sha256": hashlib.sha256(json.dumps(state["records"], sort_keys=True).encode()).hexdigest(),
            "task": "D:/wcy/求职/面试笔记/Sinan-Human原题全量评测任务单-2026-10-26.md",
        }
        state.update(cap_cny=None, cap_mode="unlimited", active_authorization=authority)
        state.setdefault("authorization_history", []).append(authority)
        state["pricing_checked_utc"] = datetime.now(UTC).isoformat()
        atomic_ledger_save(path, state)
    save(OUTPUT / "authorization.json", authority)
    return authority


def producer_hashes():
    paths = list((ROOT / "travel-agent-python/app").rglob("*.py"))
    paths += [
        ROOT / "travel-agent-python/scripts" / name
        for name in (
            "chinatravel_human.py",
            "chinatravel_original_client.py",
            "chinatravel_original_data.py",
            "eval_chinatravel_original.py",
            "sinan_nl_budget.py",
            "sinan_night_budget.py",
            "sinan_budget_lock.py",
            "p4_acceptance_budget.py",
        )
    ]
    return {str(p.relative_to(ROOT)).replace("\\", "/"): hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}


def freeze():
    manifest = prepare()
    proof_path = OUTPUT / "week7-proof.json"
    if not proof_path.exists() or not json.loads(proof_path.read_text("utf8")).get("passed"):
        raise ValueError("actual seven-day NL/API/worker/readback proof is required before Human freeze")
    authority = authorize()
    path = OUTPUT / "protocol.json"
    if path.exists():
        protocol = json.loads(path.read_text("utf8"))
        if protocol["producer_sha256"] != producer_hashes():
            raise ValueError("Human first-pass producer changed; no silent retuning")
        return protocol
    order = sorted(manifest["all"], key=lambda uid: hashlib.sha256((PHASE + ":" + uid).encode()).hexdigest())
    model_options = {}
    _apply_provider_options(model_options, "https://api.deepseek.com", False)
    protocol = {
        "frozen_utc": datetime.now(UTC).isoformat(),
        "phase": PHASE,
        "all": order,
        "denominator": 154,
        "seen": manifest["seen"],
        "unseen_recorded": manifest["unseen_recorded"],
        "days": manifest["days"],
        "concurrency": {
            "uid_workers": 4,
            "isolation": "spawned processes",
            "within_uid": "A then B then C",
            "ledger": "one original file; short OS lock, atomic commit; no lock across HTTP",
            "official_scoring": "one parent scorer; fixed disjoint groups of20",
        },
        "resource_limits": {
            "max_output_tokens": 8192,
            "intake_max_calls": 4,
            "A_calls": 1,
            "B_repair_calls": 1,
            "arm_deadline_s": 78,
            "HTTP_max_s": 60,
            "connect_timeout_s": 5,
            "CP_total_s": 30,
            "CP_each_s": 6,
            "CP_pairs": 8,
            "catalog": [40, 32, 8],
            "CP_pool": ["rotating <=6 attractions/day", "4 restaurants/day", 3],
            "daily_max_nodes": 20,
        },
        "authorization": authority["id"],
        "cap_cny": None,
        "week7_product_proof": str(proof_path.relative_to(ROOT)),
        "supplier": "existing DeepSeek Flash; project-configured thinking",
        "model_options": model_options,
        "pricing": {
            "input_peak_per_million": "2",
            "output_peak_per_million": "8",
            "url": "https://api-docs.deepseek.com/zh-cn/quick_start/pricing/",
        },
        "boundaries": [
            "NL only; Gold only scoring",
            "all154 including4-6days and failures",
            "shared1-7day upgrade precedes Human; seven-day proof required separately (Human has no7day tasks)",
            "no Human development/tuning before this first pass",
            "seen/remaining is UID registry, not template-family holdout",
            "existing canonical final object only; failure candidates separate",
            "no HTTP retries",
        ],
        "versions": {
            name: importlib.metadata.version(name) for name in ("ortools", "pandas", "geopy", "tqdm", "pydantic")
        },
        "producer_sha256": producer_hashes(),
    }
    save(path, protocol)
    with zipfile.ZipFile(OUTPUT / "frozen-producer-source.zip", "w", zipfile.ZIP_DEFLATED) as archive:
        for relative in protocol["producer_sha256"]:
            archive.write(ROOT / relative, relative)
    return protocol


def initialize_worker():
    global RUNNER
    from scripts import eval_chinatravel_original as runner

    runner.configure_suite(
        output=OUTPUT,
        query_loader=human_queries,
        evaluator=prepared,
        client_factory=partial(OriginalClient, output=OUTPUT),
    )
    RUNNER = runner


def failed_result(uid, arm, reason):
    path = OUTPUT / f"{PHASE}/results/{uid}-{arm}.json"
    if path.exists():
        return
    row = {
        "uid": uid,
        "arm": arm,
        "phase": PHASE,
        "plan": {},
        "business_plans": [],
        "issues": [reason],
        "gold_used_by_producer": False,
        "human_review": "not performed",
        "new_calls": 0,
    }
    save(path, row)
    save(WORK / f"results/SinanOriginal-{PHASE}-{arm}/{uid}.json", {})


def run_uid(uid):
    for arm in "ABC":
        if (OUTPUT / f"{PHASE}/results/{uid}-{arm}.json").exists():
            continue
        try:
            result = RUNNER.one(uid, PHASE, arm, deadline=time.monotonic() + 78)
            if not result:
                failed_result(uid, arm, "deadline exhausted before final output")
        except Exception as exc:
            failed_result(uid, arm, f"orchestration: {type(exc).__name__}: {exc}")
    return uid


def official_score(ids, tag, query_rows=None, methods=None):
    by_uid = {r["uid"]: r for r in (human_queries() if query_rows is None else query_rows)}
    split = f"sinan_human_{tag}"
    directory = WORK / f"chinatravel/data/{split}"
    for uid in ids:
        save(directory / f"{uid}.json", by_uid[uid])
    (WORK / f"chinatravel/evaluation/default_splits/{split}.txt").write_text("\n".join(ids) + "\n", encoding="utf8")
    score_rows = []
    methods = methods or {arm: f"SinanOriginal-{PHASE}-{arm}" for arm in "ABC"}
    for arm, method in methods.items():
        process = subprocess.run(
            [sys.executable, "eval_exp.py", "--splits", split, "--method", method, "--lang", "zh"],
            cwd=WORK,
            capture_output=True,
            timeout=70,
        )
        log = OUTPUT / f"{PHASE}/official-score-{tag}-{arm}.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_bytes(process.stdout + process.stderr)
        if process.returncode:
            raise RuntimeError(f"official scoring failed: {log}")
        folder = WORK / f"eval_res/splits_{split}/{method}"
        tables = {}
        for name in ("schema", "commonsense", "logical_py"):
            with (folder / f"{name}.csv").open(encoding="utf-8-sig", newline="") as stream:
                tables[name] = {r["data_id"]: r for r in csv.DictReader(stream)}
        for uid in ids:
            schema = tables["schema"][uid]["schema"] == "1"
            common = all(float(v) == 0 for k, v in tables["commonsense"][uid].items() if k != "data_id" and v)
            logical = all(float(v) == 1 for k, v in tables["logical_py"][uid].items() if k != "data_id" and v)
            score_rows.append(
                {
                    "uid": uid,
                    "arm": arm,
                    "schema": schema,
                    "common": common,
                    "logical": logical,
                    "all_pass": schema and common and logical,
                    "official_csv_folder": str(folder.relative_to(OUTPUT)),
                }
            )
    return score_rows


def wire():
    prepare()
    uid = "e20241028160248698752"
    source = ROOT / f"docs/benchmarks/sinan-chinatravel-original-20261026/dev-v2/results/{uid}-C.json"
    plan = json.loads(source.read_text("utf8"))["plan"]
    save(WORK / f"results/HumanWireValid/{uid}.json", plan)
    save(WORK / f"results/HumanWireMissing/{uid}.json", {})
    rows = official_score(
        [uid],
        "wiring",
        query_rows=[r for r in queries() if r["uid"] == uid],
        methods={"valid": "HumanWireValid", "missing": "HumanWireMissing"},
    )
    if not rows[0]["all_pass"] or any(rows[1][k] for k in ("schema", "common", "logical", "all_pass")):
        raise ValueError("official wiring valid/empty regression failed")
    save(OUTPUT / "wiring.json", {"probe_source": "existing Easy development output, no Human tuning", "rows": rows})


def snapshot(protocol, completed):
    from decimal import Decimal

    scores = []
    for path in sorted((OUTPUT / f"{PHASE}/score-batches").glob("*.json")):
        scores.extend(json.loads(path.read_text("utf8")))
    ledger = OriginalBudget(SOURCE / "budget.json", approved=True)
    book = ledger.state
    snap = {
        "as_of_utc": datetime.now(UTC).isoformat(),
        "planned": 154,
        "completed": len(completed),
        "scored": len(scores) // 3,
        "remaining": 154 - len(completed),
        "cap_cny": None,
        "peak_upper_cny": str(ledger.total()),
        "known_peak_cny": str(
            sum(Decimal(r.get("settled_peak_cny", "0")) for r in book["records"] if r["status"] == "usage_known")
        ),
        "unknown_reserved_cny": str(
            sum(Decimal(r["reserved_cny"]) for r in book["records"] if r["status"] != "usage_known")
        ),
        "in_flight": sum(r.get("request_state") == "in_flight" for r in book["records"]),
        "groups": {},
    }
    days = {r["uid"]: r["days"] for r in human_queries()}
    for group, ids in (
        ("all", set(protocol["all"])),
        ("seen", set(protocol["seen"])),
        ("unseen_recorded", set(protocol["unseen_recorded"])),
        *[(f"days_{day}", {uid for uid, value in days.items() if value == day}) for day in range(1, 7)],
    ):
        snap["groups"][group] = {}
        for arm in "ABC":
            rows = [r for r in scores if r["arm"] == arm and r["uid"] in ids]
            gate_pass = 0
            for row in rows:
                result = json.loads((OUTPUT / f"{PHASE}/results/{row['uid']}-{arm}.json").read_text("utf8"))
                gate_pass += bool(row["all_pass"] and result.get("business_plans") and not result["issues"])
            snap["groups"][group][arm] = {
                "planned": len(ids),
                "scored": len(rows),
                **{key: sum(r[key] for r in rows) for key in ("schema", "common", "logical", "all_pass")},
                "business_gate_all_pass": gate_pass,
            }
    save(OUTPUT / "rolling-snapshot.json", snap)
    return snap


def run():
    protocol = freeze()
    if not (OUTPUT / "wiring.json").exists():
        raise ValueError("run the free official wiring check before models")
    groups = [protocol["all"][start : start + 20] for start in range(0, 154, 20)]
    completed = {
        uid for uid in protocol["all"] if all((OUTPUT / f"{PHASE}/results/{uid}-{a}.json").exists() for a in "ABC")
    }

    def score_ready():
        for number, ids in enumerate(groups):
            target = OUTPUT / f"{PHASE}/score-batches/batch{number:02}.json"
            if target.exists() or not set(ids) <= completed:
                continue
            save(target, official_score(ids, f"batch{number:02}"))
            snap = snapshot(protocol, completed)
            line = (
                f"{datetime.now(UTC).isoformat()} completed={snap['completed']}/154 scored={snap['scored']}/154 "
                f"A/B/C all-pass="
                + "/".join(str(snap["groups"]["all"][a]["all_pass"]) for a in "ABC")
                + f" peak_upper={snap['peak_upper_cny']} cap=null in_flight={snap['in_flight']}"
            )
            with PROGRESS.open("a", encoding="utf8") as stream:
                stream.write("\n\n" + line + "\n")
            print(line, flush=True)

    score_ready()
    with ProcessPoolExecutor(
        max_workers=4, mp_context=multiprocessing.get_context("spawn"), initializer=initialize_worker
    ) as pool:
        futures = {pool.submit(run_uid, uid): uid for uid in protocol["all"] if uid not in completed}
        for future in as_completed(futures):
            uid = futures[future]
            try:
                future.result()
            except Exception as exc:
                for arm in "ABC":
                    failed_result(uid, arm, f"isolated worker failed: {type(exc).__name__}: {exc}")
            completed.add(uid)
            save(
                OUTPUT / "current.json",
                {"completed": len(completed), "last_uid": uid, "utc": datetime.now(UTC).isoformat()},
            )
            score_ready()
    with ledger_lock(SOURCE / "budget.json"):
        book = json.loads((SOURCE / "budget.json").read_text("utf8"))
        for row in book["records"]:
            if row["id"].startswith(PHASE + "-") and row.get("request_state") == "in_flight":
                row["request_state"] = "worker_ended_usage_unknown"
        atomic_ledger_save(SOURCE / "budget.json", book)
    snap = snapshot(protocol, completed)
    if snap["scored"] != 154:
        raise ValueError("Human official scoring incomplete; do not call the run finished")
    save(
        OUTPUT / "production-closed.json",
        {
            "closed_utc": datetime.now(UTC).isoformat(),
            "completed": len(completed),
            "scored": snap["scored"],
            "in_flight": snap["in_flight"],
            "producer_hashes_match": producer_hashes() == protocol["producer_sha256"],
            "peak_upper_cny": snap["peak_upper_cny"],
        },
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("prepare", "authorize", "wire", "freeze", "run"))
    args = parser.parse_args()
    if args.action == "prepare":
        m = prepare()
        print(json.dumps({"all": len(m["all"]), "seen": m["seen"], "days": m["days"]}))
    elif args.action == "authorize":
        print(json.dumps(authorize(), ensure_ascii=True))
    elif args.action == "wire":
        wire()
    elif args.action == "freeze":
        p = freeze()
        print(
            json.dumps(
                {
                    "frozen": len(p["all"]),
                    "seen": len(p["seen"]),
                    "cap_cny": p["cap_cny"],
                    "concurrency": p["concurrency"],
                }
            )
        )
    else:
        run()
