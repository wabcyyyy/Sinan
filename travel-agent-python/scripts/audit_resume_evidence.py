"""Recompute resume evidence from official CSVs, individual final objects and raw replan proposals."""

import argparse
import csv
import json
from collections import Counter
from decimal import Decimal
from itertools import combinations
from pathlib import Path
from statistics import mean, median

ROOT = Path(__file__).resolve().parents[2]
HUMAN = ROOT / "docs/benchmarks/sinan-chinatravel-human-20261026"
TASK = ROOT / "docs/resume-optimization/20261026"
ARMS = ("global_standard", "global_disturbance", "scoped")


def read(path):
    return json.loads(path.read_text("utf8"))


def unique_index(rows, field):
    result = {}
    for row in rows:
        key = row[field]
        if key in result:
            raise ValueError(f"duplicate {field}: {key}")
        result[key] = row
    return result


def require_universe(actual, expected):
    if set(actual) != set(expected) or len(actual) != len(expected):
        raise ValueError("missing, extra or duplicated denominator members")


def table(path):
    with path.open(encoding="utf-8-sig", newline="") as stream:
        return unique_index(list(csv.DictReader(stream)), "data_id")


def csv_pass(row, expected):
    values = [Decimal(value) for name, value in row.items() if name != "data_id" and value]
    if not values:
        raise ValueError("empty score row cannot imply success")
    return all(value == expected for value in values)


def human_audit(root=HUMAN):
    protocol = read(root / "protocol.json")
    with (ROOT / "docs/benchmarks/sinan-nl-01-20261025/upstream-data/human.csv").open(
        encoding="utf-8-sig", newline=""
    ) as stream:
        original = unique_index(list(csv.DictReader(stream)), "uid")
    require_universe(protocol["all"], original)
    if protocol["denominator"] != 154 or len(original) != 154:
        raise ValueError("original Human154 denominator changed")
    seen, remaining = protocol["seen"], protocol["unseen_recorded"]
    if set(seen) & set(remaining) or len(seen) != 4 or len(remaining) != 150:
        raise ValueError("seen/remaining partition changed")
    require_universe(seen + remaining, original)
    indexed, cache = {}, {}
    for path in sorted((root / "human-v1/score-batches").glob("*.json")):
        for recorded in read(path):
            key = recorded["uid"], recorded["arm"]
            if key in indexed:
                raise ValueError("duplicate UID-arm score")
            folder = root / recorded["official_csv_folder"].replace("\\", "/")
            if folder not in cache:
                cache[folder] = {
                    name: table(folder / f"{name}.csv") for name in ("schema", "commonsense", "logical_py")
                }
                require_universe(cache[folder]["schema"], cache[folder]["commonsense"])
                require_universe(cache[folder]["schema"], cache[folder]["logical_py"])
            uid, arm = key
            actual = {
                name: csv_pass(cache[folder][source][uid], expected)
                for name, source, expected in (
                    ("schema", "schema", 1),
                    ("common", "commonsense", 0),
                    ("logical", "logical_py", 1),
                )
            }
            actual["all_pass"] = all(actual.values())
            if any(actual[name] != recorded[name] for name in actual):
                raise ValueError(f"recorded score differs from official CSV: {key}")
            result = read(root / f"human-v1/results/{uid}-{arm}.json")
            if (result["uid"], result["arm"]) != key:
                raise ValueError("result identity mismatch")
            submitted = read(root / f"official/results/SinanOriginal-human-v1-{arm}/{uid}.json")
            if result["plan"] != submitted or bool(submitted) != bool(result.get("business_plans")):
                raise ValueError("final object boundary differs from submission")
            if actual["all_pass"] and not submitted:
                raise ValueError("failed empty final cannot count as all-pass")
            actual["business_gate_all_pass"] = bool(actual["all_pass"] and submitted and not result["issues"])
            actual["shared_final"] = bool(submitted)
            indexed[key] = actual
    require_universe(indexed, [(uid, arm) for uid in original for arm in "ABC"])
    groups = {}
    pairs = {}
    for group, ids in (("all154", list(original)), ("seen4", seen), ("remaining150", remaining)):
        groups[group] = {
            arm: {
                "denominator": len(ids),
                **{name: sum(indexed[uid, arm][name] for uid in ids) for name in indexed[ids[0], arm]},
            }
            for arm in "ABC"
        }
        pairs[group] = dict(
            Counter(
                "both_pass"
                if indexed[uid, "B"]["all_pass"] and indexed[uid, "C"]["all_pass"]
                else "B_only"
                if indexed[uid, "B"]["all_pass"]
                else "C_only"
                if indexed[uid, "C"]["all_pass"]
                else "both_fail"
                for uid in ids
            )
        )
    return {
        "verification": "Verified: CSV aggregation and saved product-boundary intersection; not new scoring",
        "historical_model": protocol["supplier"],
        "groups": groups,
        "paired_B_C": pairs,
        "definition": (
            "official all-pass=schema AND all common columns0 AND all nonempty logical columns1; "
            "product intersection also requires shared final and no runtime issues"
        ),
        "boundary": "remaining150 excludes4 registered seen UIDs; not template-family or independent real-user holdout",
    }


def positions(plans):
    return unique_index(
        [
            {"id": item["id"], "day": day["day_no"], "start": item["start_time"][:5], "end": item["end_time"][:5]}
            for day in plans
            for item in day["items"]
        ],
        "id",
    )


def raw_metrics(before, after, protected):
    old, new = positions(before), positions(after)

    def minutes(value):
        return int(value[:2]) * 60 + int(value[3:5])

    changed = {key for key in old if key not in new or old[key] != new[key]}
    retained = old.keys() & new.keys()
    return {
        "changed_items": len(changed),
        "removed_items": len(old.keys() - new.keys()),
        "cross_day_moves": sum(old[k]["day"] != new[k]["day"] for k in retained),
        "start_shift_minutes": sum(abs(minutes(old[k]["start"]) - minutes(new[k]["start"])) for k in retained),
        "locked_violations": len(changed & set(protected)),
    }


def replan_valid(arm):
    return bool(
        arm.get("proposal")
        and arm.get("feasible_checked") is True
        and arm.get("status") in {"FEASIBLE", "OPTIMAL"}
        and arm.get("checker_errors") == []
    )


def summarize_replan_rows(rows):
    def metrics(selected, names):
        return (
            {
                name: {
                    "sums": {
                        k: sum(row["arms"][name]["metrics"][k] for row in selected)
                        for k in selected[0]["arms"][name]["metrics"]
                    },
                    "means": {
                        k: mean(row["arms"][name]["metrics"][k] for row in selected)
                        for k in selected[0]["arms"][name]["metrics"]
                    },
                }
                for name in names
            }
            if selected
            else {}
        )

    arms = {}
    for name in ARMS:
        delivered = [row for row in rows if row["arms"][name]["valid"]]
        timing = [row["arms"][name]["seconds"] for row in rows if row["arms"][name].get("seconds") is not None]
        arms[name] = {
            "valid": len(delivered),
            "denominator": len(rows),
            "statuses": dict(Counter(row["arms"][name]["status"] for row in rows)),
            "delivered_only": metrics(delivered, [name]).get(name, {}),
            "wall_seconds": {
                "median": median(timing),
                "max": max(timing),
                "above_search_budget0_6": sum(v > 0.6 for v in timing),
            }
            if timing
            else None,
        }
    comparisons = {}
    for names in combinations(ARMS, 2):
        paired = [row for row in rows if all(row["arms"][name]["valid"] for name in names)]
        comparisons["_vs_".join(names)] = {
            "denominator": len(paired),
            "ids": [r["id"] for r in paired],
            "metrics": metrics(paired, names),
        }
    intersection = [row for row in rows if all(row["arms"][name]["valid"] for name in ARMS)]
    return {
        "arms": arms,
        "pairwise": comparisons,
        "three_arm_intersection": {"denominator": len(intersection), "metrics": metrics(intersection, ARMS)},
    }


def previous_replan_audit():
    root = ROOT / "docs/benchmarks/sinan-night-20261026/replan"
    cases = unique_index([read(p) for p in sorted(root.glob("expanded-??.json"))], "id")
    require_universe(cases, [f"expanded-{i:02}" for i in range(60)])
    rows = []
    for case in cases.values():
        row = {"id": case["id"], "arms": {}}
        for name in ARMS:
            arm = case["arms"][name]
            valid = bool(
                arm.get("feasible_checked")
                and arm.get("status") in {"FEASIBLE", "OPTIMAL"}
                and arm.get("checker_errors") == []
                and arm.get("metrics") is not None
            )
            row["arms"][name] = {
                "valid": valid,
                "status": arm["status"],
                "metrics": {
                    k: arm["metrics"][k]
                    for k in (
                        "changed_items",
                        "removed_items",
                        "cross_day_moves",
                        "start_shift_minutes",
                        "locked_violations",
                    )
                }
                if arm.get("metrics")
                else None,
                "seconds": arm.get("seconds"),
            }
        rows.append(row)
    return {
        "verification": (
            "Partially Verified: independently aggregated individual recorded metrics/statuses; "
            "old files lack proposals so checker cannot be rerun"
        ),
        **summarize_replan_rows(rows),
    }


def replan_audit(root):
    from app.schemas.replanning import ReplanChange
    from app.schemas.trip import GenerateRequest
    from scripts.eval_replan_effects import independent_errors

    cases = unique_index([read(p) for p in sorted(root.glob("expanded-??.json"))], "id")
    require_universe(cases, [f"expanded-{i:02}" for i in range(60)])
    rows = []
    for case in cases.values():
        require_universe(case["arms"], ARMS)
        if case["same_total_seconds_per_arm"] != 0.6:
            raise ValueError("arm budget changed")
        row = {"id": case["id"], "arms": {}}
        for name, arm in case["arms"].items():
            raw = arm.get("audit_inputs")
            valid = replan_valid(arm)
            metrics, errors = None, []
            if arm.get("proposal"):
                if not raw:
                    raise ValueError("proposal without complete checker inputs")
                matrix = {(r["from"], r["to"]): r["route"] for r in raw["matrix"]}
                errors = independent_errors(
                    GenerateRequest.model_validate(raw["request"]),
                    raw["before"],
                    arm["proposal"],
                    ReplanChange.model_validate(raw["event"]),
                    raw["protected"],
                    matrix,
                    raw["optional"],
                )
                metrics = raw_metrics(raw["before"], arm["proposal"], raw["protected"])
                if errors != arm["checker_errors"] or any(arm["metrics"][k] != v for k, v in metrics.items()):
                    raise ValueError(f"raw proposal audit differs from recorded checker/metrics: {case['id']}/{name}")
            if arm.get("feasible_checked") and not valid:
                raise ValueError("failed/empty proposal counted as success")
            row["arms"][name] = {
                "valid": valid,
                "status": arm["status"],
                "metrics": metrics,
                "errors": errors,
                "seconds": arm.get("seconds"),
            }
        rows.append(row)
    return {
        "verification": "Verified from raw before/after; deterministic checker rerun; no new solver calls",
        "scope": (
            "single replay of60 already-seen synthetic events in12 generated layout families; "
            "same0.6s search deadline per arm, not hard wall; failures remain in denominator"
        ),
        "metric_boundary": (
            "start shift uses absolute clock-minute difference for retained items; "
            "cross-day moves counted separately; not elapsed journey displacement"
        ),
        **summarize_replan_rows(rows),
        "previous_recorded": previous_replan_audit(),
        "rows": rows,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--replan", type=Path, default=TASK / "replan-replay")
    parser.add_argument("--output", type=Path, default=TASK / "evidence-audit.json")
    args = parser.parse_args()
    result = {"human": human_audit(), "replan": replan_audit(args.replan)}
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf8")
    print(json.dumps({"human": result["human"]["groups"], "replan": result["replan"]["arms"]}, ensure_ascii=True))


if __name__ == "__main__":
    main()
