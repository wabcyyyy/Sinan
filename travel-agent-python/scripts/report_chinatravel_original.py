"""Read-only aggregation of frozen original-task artifacts; never produces plans or rewrites scores."""

import argparse
import csv
import json
import statistics
from collections import Counter
from decimal import Decimal

from scripts.chinatravel_original_client import OriginalBudget
from scripts.chinatravel_original_data import OUTPUT, SOURCE, save


def raw_scores():
    rows = {}
    for path in sorted((OUTPUT / "formal-v1/score-batches").glob("*.json")):
        for row in json.loads(path.read_text("utf8")):
            key = row["uid"], row["arm"]
            if key in rows and rows[key] != row:
                raise ValueError("conflicting score batches")
            rows[key] = row
    return rows


def finalize_submissions():
    """Export actual shared business outputs; failure candidates remain diagnostic artifacts only."""
    from app.schemas.travel_plan import TravelPlan
    from app.schemas.trip import DailyPlan
    from scripts.eval_chinatravel_original import score

    protocol = json.loads((OUTPUT / "protocol.json").read_text("utf8"))
    raw = raw_scores()
    expected = {(uid, arm) for uid in protocol["selected"] for arm in "ABC"}
    if set(raw) != expected:
        raise ValueError("finish all frozen production and official batches before finalizing")
    final_path = OUTPUT / "formal-v1/final-scores.json"
    if final_path.exists():
        return  # Reuse the completed correction; never rescore unchanged artifacts routinely.
    corrected = {key: dict(row) for key, row in raw.items()}
    changed = {arm: [] for arm in "ABC"}
    audit = []
    for (uid, arm), row in corrected.items():
        result = json.loads((OUTPUT / f"formal-v1/results/{uid}-{arm}.json").read_text("utf8"))
        plans = [DailyPlan.model_validate(day) for day in result.get("business_plans", [])]
        if plans:
            header = plans[0].travel_header
            if header is None or any(day.travel_header != header for day in plans):
                raise ValueError(f"shared business header missing/inconsistent: {uid}-{arm}")
            final = TravelPlan.model_validate(
                {
                    **header.model_dump(),
                    "itinerary": [{"day": day.day_no, "activities": day.travel_activities} for day in plans],
                }
            ).official()
            if final != result["plan"]:
                raise ValueError(f"shared final plan differs from submitted representation: {uid}-{arm}")
        else:
            final = {}
        target = OUTPUT / f"official/results/SinanOriginal-final-v1-{arm}/{uid}.json"
        save(target, final)
        row["final_submission"] = str(target.relative_to(OUTPUT))
        row["shared_final_available"] = bool(plans)
        row["business_accepted"] = bool(plans) and not result["issues"]
        if final != result["plan"]:
            changed[arm].append(uid)
            audit.append(
                {
                    "uid": uid,
                    "arm": arm,
                    "raw_all_pass": row["all_pass"],
                    "reason": "no shared business final; retain candidate only in original failure artifact",
                    "source_result": f"formal-v1/results/{uid}-{arm}.json",
                }
            )
    for arm, ids in changed.items():
        for start in range(0, len(ids), 20):
            batch = ids[start : start + 20]
            split = score(batch, "final-v1", arm, tag=f"boundary_{arm}_{start:03}")
            folder = OUTPUT / f"official/eval_res/splits_{split}/SinanOriginal-final-v1-{arm}"
            tables = {}
            for name in ("schema", "commonsense", "logical_py"):
                with (folder / f"{name}.csv").open(encoding="utf-8-sig", newline="") as stream:
                    tables[name] = {r["data_id"]: r for r in csv.DictReader(stream)}
            for uid in batch:
                row = corrected[uid, arm]
                row["raw_submission_scores"] = {k: row[k] for k in ("schema", "common", "logical", "all_pass")}
                row["schema"] = Decimal(tables["schema"][uid]["schema"]) == 1
                for key, table, expected_value in (("common", "commonsense", 0), ("logical", "logical_py", 1)):
                    row[key] = all(
                        Decimal(v) == expected_value for k, v in tables[table][uid].items() if k != "data_id" and v
                    )
                row["all_pass"] = row["schema"] and row["common"] and row["logical"]
                row["official_csv_folder"] = str(folder.relative_to(OUTPUT))
    save(
        OUTPUT / "formal-v1/submission-boundary-correction.json",
        {
            "rule": "export exact shared business final or empty on pipeline failure; no candidate substitution",
            "raw_originals_preserved": True,
            "changed": audit,
            "unchanged_score_rows_reused": len(corrected) - len(audit),
            "no_new_models_or_solver_runs": True,
        },
    )
    save(final_path, list(corrected.values()))


def report():
    protocol = json.loads((OUTPUT / "protocol.json").read_text("utf8"))
    raw = raw_scores()
    final_path = OUTPUT / "formal-v1/final-scores.json"
    scores = {(r["uid"], r["arm"]): r for r in json.loads(final_path.read_text("utf8"))} if final_path.exists() else raw
    results = {}
    for path in (OUTPUT / "formal-v1/results").glob("*.json"):
        row = json.loads(path.read_text("utf8"))
        results[row["uid"], row["arm"]] = row
    summary = {
        "scope": {},
        "resources": {},
        "pairs": {},
        "failures": [],
        "score_basis": "shared final outputs" if final_path.exists() else "raw submission diagnostics",
    }
    for cohort, ids in (("all", protocol["selected"]), ("dev", protocol["dev"]), ("confirm", protocol["confirm"])):
        summary["scope"][cohort] = {}
        for arm in "ABC":
            rows = [scores[uid, arm] for uid in ids if (uid, arm) in scores]
            summary["scope"][cohort][arm] = {
                "planned": len(ids),
                "scored": len(rows),
                **{key: sum(r[key] for r in rows) for key in ("schema", "common", "logical", "all_pass")},
                "runtime_clear": sum(not results.get((uid, arm), {"issues": ["unrun"]})["issues"] for uid in ids),
                "raw_diagnostic_all_pass": sum(raw[uid, arm]["all_pass"] for uid in ids if (uid, arm) in raw),
                "business_accepted_all_pass": sum(
                    scores[uid, arm]["all_pass"]
                    and not results[uid, arm]["issues"]
                    and bool(results[uid, arm].get("business_plans"))
                    for uid in ids
                    if (uid, arm) in scores and (uid, arm) in results
                ),
                "runtime_clear_but_official_fail": sum(
                    not results[uid, arm]["issues"] and not scores[uid, arm]["all_pass"]
                    for uid in ids
                    if (uid, arm) in scores and (uid, arm) in results
                ),
                "runtime_blocked_but_official_pass": sum(
                    bool(results[uid, arm]["issues"]) and scores[uid, arm]["all_pass"]
                    for uid in ids
                    if (uid, arm) in scores and (uid, arm) in results
                ),
                "unscored": [uid for uid in ids if (uid, arm) not in scores],
            }
        for other in "AB":
            pairs = [uid for uid in ids if (uid, "C") in scores and (uid, other) in scores]
            summary["pairs"][f"{cohort}:C-vs-{other}"] = dict(
                Counter(
                    "win"
                    if scores[uid, "C"]["all_pass"] and not scores[uid, other]["all_pass"]
                    else "loss"
                    if scores[uid, other]["all_pass"] and not scores[uid, "C"]["all_pass"]
                    else "tie"
                    for uid in pairs
                )
            )
    for arm in "ABC":
        rows = [r for (uid, a), r in results.items() if a == arm and uid in protocol["selected"]]
        elapsed = [r["elapsed_s"] for r in rows if "elapsed_s" in r]
        summary["resources"][arm] = {
            "records": len(rows),
            "recorded_producer_only_seconds_sum": sum(elapsed),
            "recorded_producer_only_seconds_median": statistics.median(elapsed) if elapsed else None,
            "planning_goto_calls_excluding_official_checker": sum(
                r.get("tool_usage", {}).get("planning_goto_calls_excluding_official_checker", 0) for r in rows
            ),
            "B_reuses_A_on_clean_first_output": sum(
                bool(arm == "B" and r.get("shared_A_first_output") and not r.get("new_calls")) for r in rows
            ),
        }
        if arm == "B":
            summary["resources"][arm]["logical_goto_including_shared_A_first"] = sum(
                results.get((r["uid"], "A"), {})
                .get("tool_usage", {})
                .get("planning_goto_calls_excluding_official_checker", 0)
                + (
                    r.get("tool_usage", {}).get("planning_goto_calls_excluding_official_checker", 0)
                    if r.get("new_calls")
                    else 0
                )
                for r in rows
            )
    for (uid, _arm), score in scores.items():
        if score["all_pass"]:
            continue
        folder = OUTPUT / score["official_csv_folder"]
        diagnostics = {}
        for name, passing in (("commonsense", Decimal(0)), ("logical_py", Decimal(1))):
            with (folder / f"{name}.csv").open(encoding="utf-8-sig", newline="") as stream:
                row = next(r for r in csv.DictReader(stream) if r["data_id"] == uid)
            diagnostics[name] = [k for k, v in row.items() if k != "data_id" and v and Decimal(v) != passing]
        summary["failures"].append({**score, "official_failed_columns": diagnostics})
    ledger = OriginalBudget(SOURCE / "budget.json", approved=True)
    fresh = [r for r in ledger.state["records"] if r["id"].startswith("formal-v1-")]
    summary["cost"] = {
        "whole_project_peak_upper_cny": str(ledger.total()),
        "cap_cny": "30",
        "supplier_invoice_checked": False,
        "formal_unique_issued_calls": len(fresh),
        "formal_peak_upper_cny": str(sum(Decimal(r.get("settled_peak_cny", r["reserved_cny"])) for r in fresh)),
        "whole_project_unknown_reserves": [
            {"id": r["id"], "cny": r["reserved_cny"]} for r in ledger.state["records"] if r["status"] != "usage_known"
        ],
        "formal_prompt_tokens": sum(r.get("usage", {}).get("prompt_tokens", 0) for r in fresh),
        "formal_completion_tokens": sum(r.get("usage", {}).get("completion_tokens", 0) for r in fresh),
    }
    for stage in ("shared_NL", "A_generation", "B_repair"):
        records = [
            r
            for r in fresh
            if ("A_generation" if "-A-" in r["id"] else "B_repair" if "-B-" in r["id"] else "shared_NL") == stage
        ]
        summary["cost"][stage] = {
            "issued_attempts_including_unknown": len(records),
            "prompt_tokens_known": sum(r.get("usage", {}).get("prompt_tokens", 0) for r in records),
            "completion_tokens_known": sum(r.get("usage", {}).get("completion_tokens", 0) for r in records),
            "peak_upper_cny": str(sum(Decimal(r.get("settled_peak_cny", r["reserved_cny"])) for r in records)),
        }
    summary["boundaries"] = [
        "Official frozen Chinese WorldEnv; not live online bookings, live prices, or real-user benefit",
        "A/B/C share parsed original NL; C planning has zero model calls but inherits paid NL intake",
        "Producer time excludes shared intake/research; copied clean B records reuse A time; no end-to-end speed claim",
        "Developed and confirmation IDs are separate; schema/common/logical/all-pass are separate",
        "Missing/unrun/unsupported/timeout stay in planned denominator; human review not performed; full CI pending",
    ]
    save(OUTPUT / "formal-v1/summary.json", summary)
    print(json.dumps({k: v for k, v in summary.items() if k in ("scope", "pairs", "cost")}, ensure_ascii=True))
    return summary


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--finalize-submissions", action="store_true")
    args = parser.parse_args()
    if args.finalize_submissions:
        finalize_submissions()
    report()
