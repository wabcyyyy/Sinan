"""Local frozen benchmarks. Prepare once; bounded batches retain every outcome and input."""

import argparse
import json
import platform
import statistics
import time
from collections import Counter
from dataclasses import asdict, replace
from pathlib import Path
from unittest.mock import patch

import ortools
from ortools.sat.python import cp_model

from app.agent.generation.content.constraint_trip import compile_trip
from app.agent.generation.content.constraint_trip_solver import solve_trip
from app.agent.grounding import screenshot_entities
from scripts import eval_replan_ablation as ablation
from scripts.align_p4_scenario import local_only
from scripts.benchmark_expansion_cases import (
    kernel_problem,
    kernel_specs,
    replan_environment,
    replan_event,
    replan_specs,
    serialize_kernel,
)
from scripts.eval_replan_effects import independent_errors
from scripts.run_p4_acceptance import save
from tests.constraint_planner_oracle import exhaustive_day, schedule_violations

METRICS = ("changed_items", "start_shift_minutes", "cross_day_moves", "locked_violations", "removed_items")


def prepare(root):
    root = Path(root)
    root.mkdir(parents=True, exist_ok=False)
    replan = []
    for spec in replan_specs():
        request, before, routes, completed, optional = replan_environment(spec)
        replan.append(
            {
                "spec": spec,
                "event": replan_event(spec).model_dump(),
                "request": request.model_dump(mode="json"),
                "before": before,
                "routes": routes,
                "completed": completed,
                "optional": optional,
            }
        )
    kernel = [{"spec": spec, "facts": serialize_kernel(*kernel_problem(spec))} for spec in kernel_specs()]
    save(
        root / "design.json",
        {
            "version": 1,
            "scope": "author-visible synthetic frozen inputs; no product tuning after results",
            "sampling": "60 distinct base instances nested in 12 generated layout families, one hard event each",
            "replan": replan,
            "kernel": kernel,
            "poi": poi_cases(),
            "arms": ablation.ARMS,
            "arm_seconds": ablation.ARM_SECONDS,
            "repeats": 1,
            "inclusion": "retain all failures; primary means use all-four-feasible intersection; no resampling",
            "timing": "perf_counter end-to-end arm; snapshot, compile, solve, checker; no alternate options",
            "kernel_reference": "single-day exhaustive oracle; multi-day independent output checker",
            "model_plan": "30 single-day cases once; direct output and same-output greedy; <=30 calls, <=CNY10",
            "old_reference": "sinan-p0-20261025/ablation-perf-counter/report.json (unchanged)",
            "python": platform.python_version(),
            "ortools": ortools.__version__,
        },
    )
    return {"replan": len(replan), "kernel": len(kernel), "poi": len(poi_cases())}


def run_replan(root, start, stop):
    root = Path(root)
    design = json.loads((root / "design.json").read_text("utf8"))
    output = root / f"replan-{start:02}-{stop:02}.json"
    if output.exists():
        raise ValueError("batch exists; do not overwrite or repeat")
    rows, deadline = [], time.monotonic() + 65
    with local_only(), patch.object(ablation, "environment", replan_environment):
        for offset, case in enumerate(design["replan"][start:stop], start):
            spec, event = case["spec"], replan_event(case["spec"])
            request, before, routes, completed, optional = replan_environment(spec)
            assert request.model_dump(mode="json") == case["request"] and before == case["before"]
            matrix = {(f"item:{r['fromItemId']}", f"item:{r['toItemId']}"): r["route"] for r in routes}
            baseline_errors = independent_errors(
                request, before, before, event.model_copy(update={"kind": "prefer"}), completed, matrix, optional
            )
            row = {
                "id": spec["id"],
                "family": spec["family"],
                "kind": event.kind,
                "baseline_errors": baseline_errors,
                "arms": {},
            }
            order = offset % 4
            for arm in ablation.ARMS[order:] + ablation.ARMS[:order]:
                if time.monotonic() >= deadline:
                    save(output.with_suffix(".partial.json"), {"rows": rows, "incomplete": row})
                    raise TimeoutError("65-second batch budget; completed rows retained")
                try:
                    row["arms"][arm] = ablation.run_arm(spec, event, arm)
                except Exception as exc:
                    row["arms"][arm] = {"status": "ERROR", "feasible_checked": False, "error": repr(exc)}
            rows.append(row)
            save(output.with_suffix(".partial.json"), {"rows": rows})
    save(output, {"rows": rows, "model_calls": 0, "http_requests": 0})
    return rows


def trip_errors(problems, results, budget):
    errors, seen, names, total = [], [], set(), 0
    for problem, result in zip(problems, results, strict=True):
        # The shared budget and global required clauses are checked below, not repeated per day.
        req = problem.requirements.model_copy(deep=True)
        req.required_places = [p for p in req.required_places if p.day_no is not None]
        errors.extend(schedule_violations(replace(problem, requirements=req, budget=None), result.visits))
        by_id = {c.candidate_id: c for c in problem.candidates}
        for visit in result.visits:
            seen.append(visit.candidate_id)
            names.add(by_id[visit.candidate_id].name)
            total += by_id[visit.candidate_id].price_minor or 0
    if len(seen) != len(set(seen)):
        errors.append("cross-day duplicate")
    if any(p.day_no is None and p.name not in names for p in problems[0].requirements.required_places):
        errors.append("global required")
    if total > int(budget * 100):
        errors.append("shared budget")
    return errors


def check_core(problems, budget, result):
    days = compile_trip(problems, budget)
    model = days[0].model
    assumptions = {i: f"day:{check.day_no}:{check.constraint_id}" for d in days for i, check in d.assumptions.items()}
    core = [i for i, key in assumptions.items() if key in result.conflict_ids]
    started = time.perf_counter()

    def resolve(indexes):
        model.clear_assumptions()
        model.add_assumptions([model.get_bool_var_from_proto_index(i) for i in indexes])
        solver = cp_model.CpSolver()
        solver.parameters.num_search_workers = 1
        solver.parameters.cp_model_presolve = False
        solver.parameters.max_time_in_seconds = 0.5
        return solver.status_name(solver.solve(model))

    sufficient = resolve(core)
    deletions = [{"removed": assumptions[i], "status": resolve([j for j in core if j != i])} for i in core]
    # Separate core minimality from restoration with ALL other original hard clauses still enabled.
    recovery = [{"relaxed": assumptions[i], "status": resolve([j for j in assumptions if j != i])} for i in core]
    return {
        "core_size": len(core),
        "sufficient_status": sufficient,
        "deletions": deletions,
        "independently_irreducible": bool(core)
        and sufficient == "INFEASIBLE"
        and all(d["status"] in {"OPTIMAL", "FEASIBLE"} for d in deletions),
        "original_model_single_relaxations": recovery,
        "any_original_model_recovered": any(r["status"] in {"OPTIMAL", "FEASIBLE"} for r in recovery),
        "proof_seconds": time.perf_counter() - started,
    }


def run_kernel(root):
    root = Path(root)
    output = root / "kernel.json"
    if output.exists():
        raise ValueError("kernel results already exist")
    design = json.loads((root / "design.json").read_text("utf8"))
    rows, deadline = [], time.monotonic() + 65
    with local_only():
        for case in design["kernel"]:
            if time.monotonic() >= deadline:
                raise TimeoutError("kernel batch budget reached; partial retained")
            spec = case["spec"]
            problems, budget = kernel_problem(spec)
            assert json.loads(json.dumps(serialize_kernel(problems, budget))) == case["facts"]
            try:
                results = solve_trip(problems, budget=budget, time_limit_s=0.6)
                result = results[0]
                numerical = result.status if result.status not in {"FEASIBLE", "OPTIMAL"} else "FEASIBLE"
                row = {
                    **spec,
                    "status": result.status,
                    "classification_correct": numerical == spec["expected"],
                    "seconds": result.elapsed_s,
                    "conflict_ids": result.conflict_ids,
                    "shrink_completed": result.conflict_irreducible,
                    "visits": [[asdict(v) for v in r.visits] for r in results],
                    "checker_errors": trip_errors(problems, results, budget) if result.visits else [],
                }
                if spec["days"] == 1 and spec["expected"] != "UNKNOWN":
                    witness, score, examined = exhaustive_day(problems[0])
                    row["oracle"] = {"feasible": witness is not None, "objective": score, "examined": examined}
                    row["oracle_matches_label"] = (witness is not None) == (spec["expected"] == "FEASIBLE")
                if result.status == "INFEASIBLE":
                    row["core_proof"] = check_core(problems, budget, result)
            except Exception as exc:
                row = {**spec, "status": "ERROR", "classification_correct": False, "error": repr(exc)}
            rows.append(row)
            save(output.with_suffix(".partial.json"), {"rows": rows})
    save(output, {"rows": rows, "model_calls": 0, "http_requests": 0})
    return rows


def poi_cases():
    labels = [
        ("卢浮宫", "Musée du Louvre", "Paris", "FR"),
        ("清水寺", "Kiyomizu-dera", "Kyoto", "JP"),
        ("東京駅", "Tokyo Station", "Tokyo", "JP"),
        ("British Museum", "大英博物館", "London", "GB"),
        ("京都駅", "Kyoto Station", "Kyoto", "JP"),
        ("中央公園", "Central Park", "New York", "US"),
    ]
    rows = []
    for i in range(48):
        family, variant = i % 8, i // 8
        query, local, city, country = labels[variant]
        gold = {
            "place_id": f"osm:node:{1000 + i:05d}",
            "name": local,
            "aliases": [query],
            "display_name": f"{city}, center district",
            "country_code": country,
            "latitude": 35.1 + i / 100,
            "longitude": 120.1 + i / 100,
            "source_url": f"https://www.openstreetmap.org/node/{1000 + i:05d}",
            "type": "museum",
        }
        distractor = {
            **gold,
            "place_id": f"osm:node:{(500 if variant % 2 == 0 else 2000) + i:05d}",
            "source_url": f"https://www.openstreetmap.org/node/{(500 if variant % 2 == 0 else 2000) + i:05d}",
        }
        kind, dirty = "place", []
        if family == 0:  # cross-script alias, no canonical literal hit
            distractor.update(name=f"{query} Shop", aliases=[])
        elif family == 1:  # same name, different city; both candidates are otherwise valid
            gold["name"] = query
            distractor.update(name=query, display_name="Other City")
        elif family == 2:  # width/case/punctuation normalization
            query = "ＴＯＫＹＯ　ＳＴＡＴＩＯＮ" if variant % 2 else "ＢＲＩＴＩＳＨ－ＭＵＳＥＵＭ"
            gold.update(name="Tokyo Station" if variant % 2 else "British Museum", aliases=[])
            distractor.update(name="Station Museum Shop", aliases=[])
        elif family == 3:
            kind = "transport"
            gold.update(name=query, type=("station", "airport", "bus_station")[variant % 3])
            distractor.update(name=query, type="museum")
        elif family in (4, 5):
            gold["name"] = query
            distractor.update(name=query, latitude=0 if family == 4 else None, longitude=0 if family == 4 else None)
            dirty = [distractor["place_id"]]
        elif family == 6:
            gold["name"] = query
            distractor.update(
                name=query,
                country_code="ZZ" if variant % 2 else country,
                source_url=None if variant % 2 == 0 else distractor["source_url"],
            )
            dirty = [distractor["place_id"]]
        else:  # deliberately unresolved by the present city/type heuristic
            gold["name"] = query
            distractor.update(name=query, display_name=f"{city}, different branch")
        candidates = [distractor, gold] if variant % 2 == 0 else [gold, distractor]
        rows.append(
            {
                "id": f"poi-{i:02}",
                "family": (
                    "alias",
                    "other_city",
                    "nfkc",
                    "transport",
                    "zero_coords",
                    "missing_coords",
                    "country_source",
                    "same_city_branch",
                )[family],
                "query": query,
                "city": city,
                "country": country,
                "kind": kind,
                "gold": gold["place_id"],
                "dirty_ids": dirty,
                "candidates": candidates,
            }
        )
    return rows


def run_poi(root):
    root = Path(root)
    revised = root / "poi-design-v3.json"
    output = root / ("poi-v3.json" if revised.exists() else "poi.json")
    if output.exists():
        raise ValueError("POI results already exist")
    design = json.loads((revised if revised.exists() else root / "design.json").read_text("utf8"))
    rows = []
    with local_only():
        for case in design["poi"]:
            raw = case["candidates"]

            def literal_rank(candidates, query=case["query"]):
                # A fair name-only baseline: exact trimmed casefold, stable upstream tie order.
                return sorted(
                    candidates, key=lambda candidate: candidate["name"].strip().casefold() != query.strip().casefold()
                )

            baseline = literal_rank(raw)
            clean = screenshot_entities.screenshot_entity_choices(raw, case["country"])
            clean_ids = {c.entity_id for c in clean}
            common_filtered = literal_rank([r for r in raw if r["place_id"] in clean_ids])
            with patch.object(screenshot_entities, "geocode_place_rows", return_value=raw):
                ranked = screenshot_entities.resolve_screenshot_place(
                    case["query"], case["city"], case["country"], case["kind"]
                )
            rows.append(
                {
                    "id": case["id"],
                    "family": case["family"],
                    "gold": case["gold"],
                    "baseline_top1": baseline[0]["place_id"],
                    "filtered_literal_top1": common_filtered[0]["place_id"] if common_filtered else None,
                    "rules_top1": ranked[0].entity_id if ranked else None,
                    "ranking": [c.model_dump(mode="json") for c in ranked or []],
                    "dirty_total": len(case["dirty_ids"]),
                    "rules_dirty_removed": len(set(case["dirty_ids"]) - clean_ids),
                    "valid_total": len(raw) - len(case["dirty_ids"]),
                    "rules_valid_removed": len({r["place_id"] for r in raw} - set(case["dirty_ids"]) - clean_ids),
                }
            )
    summary = {
        "cases": len(rows),
        "families": {},
        "dirty_total": sum(r["dirty_total"] for r in rows),
        "rules_dirty_removed": sum(r["rules_dirty_removed"] for r in rows),
        "rules_valid_removed": sum(r["rules_valid_removed"] for r in rows),
        "baseline_dirty_removed": 0,
    }
    for key in ("baseline_top1", "filtered_literal_top1", "rules_top1"):
        summary[key] = {"hits": sum(r[key] == r["gold"] for r in rows), "n": len(rows)}
    for family in sorted({r["family"] for r in rows}):
        subset = [r for r in rows if r["family"] == family]
        summary["families"][family] = {
            k: sum(r[k] == r["gold"] for r in subset) for k in ("baseline_top1", "filtered_literal_top1", "rules_top1")
        }
        summary["families"][family]["n"] = len(subset)
    save(output, {"summary": summary, "rows": rows, "model_calls": 0, "http_requests": 0})
    return summary


def summarize(root):
    root = Path(root)
    design = json.loads((root / "design.json").read_text("utf8"))
    rows = [r for p in sorted(root.glob("replan-??-??.json")) for r in json.loads(p.read_text("utf8"))["rows"]]
    if len({r["id"] for r in rows}) != len(rows):
        raise ValueError("overlapping replan batches")
    paired = [r for r in rows if all(r["arms"][a]["feasible_checked"] for a in ablation.ARMS)]
    report = {
        "events": len(rows),
        "expected_events": len(design["replan"]),
        "all_four_feasible": len(paired),
        "families": len({r["family"] for r in rows}),
        "by_arm": {},
        "by_kind": {},
        "by_family": {},
    }
    for arm in ablation.ARMS:
        report["by_arm"][arm] = {
            "statuses": dict(Counter(r["arms"][arm]["status"] for r in rows)),
            "checker_failures": sum(bool(r["arms"][arm].get("checker_errors")) for r in rows),
            "paired_means": {k: statistics.mean(r["arms"][arm]["metrics"][k] for r in paired) for k in METRICS},
            "paired_sums": {k: sum(r["arms"][arm]["metrics"][k] for r in paired) for k in METRICS},
            "median_seconds_all": statistics.median(
                r["arms"][arm]["seconds"] for r in rows if "seconds" in r["arms"][arm]
            ),
            "median_seconds_paired": statistics.median(r["arms"][arm]["seconds"] for r in paired),
            "scopes": dict(Counter(r["arms"][arm].get("scope") for r in paired)),
        }
    for field in ("kind", "family"):
        for value in sorted({r[field] for r in rows}):
            subset = [r for r in paired if r[field] == value]
            report[f"by_{field}"][value] = {
                "events": sum(r[field] == value for r in rows),
                "paired": len(subset),
                "means": {
                    a: {
                        k: statistics.mean(r["arms"][a]["metrics"][k] for r in subset) if subset else None
                        for k in METRICS
                    }
                    for a in ablation.ARMS
                },
            }
    report["baseline_invalid"] = sum(bool(r["baseline_errors"]) for r in rows)
    save(root / "replan-report.json", {"summary": report, "rows": rows})
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--prepare", action="store_true")
    parser.add_argument("--summarize", action="store_true")
    parser.add_argument("--prepare-poi", action="store_true")
    args = parser.parse_args()
    if args.prepare_poi:
        target = args.root / "poi-design-v3.json"
        if target.exists():
            raise ValueError("revised POI design already frozen")
        save(target, {"poi": poi_cases(), "reason": "balance lexical entity-ID ties; original design/results retained"})
        print("48 POI cases frozen with fixed-width, label-balanced entity IDs")
    else:
        print(json.dumps(prepare(args.root) if args.prepare else summarize(args.root), ensure_ascii=False))
