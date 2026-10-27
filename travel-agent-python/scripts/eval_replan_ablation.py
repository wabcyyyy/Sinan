"""四臂归因：普通全局 / 扰动全局 / 渐进范围 / 渐进范围无 hint；零网络、零模型。"""

import argparse
import json
import statistics
import time
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from app.agent.generation.content.constraint_replan_check import item_positions, schedule_metrics
from app.agent.generation.content.constraint_replan_model import ReplanModel
from app.agent.generation.content.constraint_trip_solver import solve_trip
from app.agent.generation.orchestration import constraint_replanning as planner
from app.agent.generation.orchestration.replan_snapshot import create_snapshot, neighborhoods
from scripts.align_p4_scenario import local_only
from scripts.eval_replan_effects import ARM_SECONDS, BASES, environment, events, independent_errors
from scripts.run_p4_acceptance import save

ARMS = ("global_standard", "global_disturbance", "scoped", "scoped_no_hint")


def run_arm(spec, event, arm, *, include_audit=False):
    request, before, routes, completed, optional = environment(spec)
    started = time.monotonic()
    measured_started = time.perf_counter()
    problems, prepared, previous, protected, req, matrix, start = create_snapshot(
        request, before, event, completed, routes
    )
    deadline = started + ARM_SECONDS
    assert request.budget is not None
    budget = Decimal(str(request.budget))
    scopes, _ = neighborhoods(before, event, req, True)
    solves = []

    def measured(*args, **kwargs):
        result = solve_trip(*args, **kwargs)
        solves.append(
            {
                "status": result[0].solver_status,
                "timings": result[0].timings,
                "optimized_tiers": result[0].optimized_tiers,
                "total_tiers": result[0].total_tiers,
            }
        )
        return result

    if arm.startswith("scoped"):
        with patch.object(planner, "solve_trip", measured):
            attempts, chosen, _ = planner._find_scope(
                problems, previous, event, scopes, deadline, budget, optional, use_hints=arm != "scoped_no_hint"
            )
        result = chosen[2] if chosen else None
        movable = chosen[1] if chosen else set()
        scope = chosen[0] if chosen else None
        status = planner._numerical_status(result[0]) if result else attempts[-1].status
    else:
        movable, scope = {v.item_id for v in previous}, "trip"
        result = measured(
            problems,
            budget=budget,
            time_limit_s=max(0.001, deadline - time.monotonic()),
            repair=ReplanModel(
                previous,
                event,
                movable,
                cross_day=True,
                removable=set(optional),
                strategy="global" if arm == "global_standard" else "minimal",
            ),
        )
        status = planner._numerical_status(result[0])
        attempts = []
    proposal = (
        planner._proposal(request, before, result, prepared, matrix, start, req, completed)
        if result and status in {"FEASIBLE", "OPTIMAL"}
        else None
    )
    errors = independent_errors(request, before, proposal, event, protected, matrix, optional) if proposal else []
    metrics = schedule_metrics(before, proposal, matrix, protected)[0].model_dump() if proposal else None
    changed = schedule_metrics(before, proposal, matrix, protected)[1] if proposal else []
    local_ids = scopes[0][1]
    output = {
        "status": status,
        "feasible_checked": proposal is not None and not errors,
        "checker_errors": errors,
        "metrics": metrics,
        "seconds": time.perf_counter() - measured_started,
        "scope": scope,
        "movable_ids": sorted(movable),
        "changed_ids": changed,
        "outside_initial_neighborhood": len(set(changed) - local_ids),
        "outside_selected_scope": len(set(changed) - movable),
        "affected_days": len({d for key, (d, _, _) in item_positions(proposal or []).items() if key in changed}),
        "attempts": [a.model_dump() for a in attempts],
        "solves": solves,
    }
    if include_audit:
        output["audit_inputs"] = {
            "request": request.model_dump(mode="json"),
            "before": before,
            "event": event.model_dump(mode="json"),
            "protected": sorted(protected),
            "optional": sorted(optional),
            "matrix": [{"from": a, "to": b, "route": route} for (a, b), route in matrix.items()],
        }
        output["proposal"] = proposal
    return output


def evaluate(output, repeats=3):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    save(
        output / "design.json",
        {
            "bases": BASES,
            "repeats": repeats,
            "arms": ARMS,
            "arm_seconds": ARM_SECONDS,
            "selection": "minimal only; identical scoped solver, no alternate-plan generation in any arm",
            "scope": "author-visible frozen synthetic facts, repeats nested in each event; no model or HTTP",
        },
    )
    rows = []
    deadline = time.monotonic() + 65
    with local_only():
        for repeat in range(repeats):
            for spec in BASES:
                for index, event in enumerate(events(spec)):
                    if time.monotonic() >= deadline:
                        raise TimeoutError(f"消融执行预算耗尽，已完成 {len(rows)} 个事件；不算完整结果")
                    row = {"base_id": spec["id"], "split": spec["split"], "event": event.model_dump(), "repeat": repeat}
                    order = (index + repeat) % len(ARMS)
                    for arm in ARMS[order:] + ARMS[:order]:
                        try:
                            row[arm] = run_arm(spec, event, arm)
                        except Exception as exc:
                            row[arm] = {"status": "ERROR", "feasible_checked": False, "error": repr(exc)}
                    rows.append(row)
                    save(output / "partial.json", {"rows": rows})
    hard = [r for r in rows if r["split"] == "evaluation" and r["event"]["kind"] != "prefer"]
    paired = [r for r in hard if all(r[a]["feasible_checked"] for a in ARMS)]
    summary = {"evaluation_hard_observations": len(hard), "all_arms_feasible": len(paired), "by_arm": {}}
    for arm in ARMS:
        summary["by_arm"][arm] = {
            "feasible": sum(r[arm]["feasible_checked"] for r in hard),
            "statuses": {s: sum(r[arm]["status"] == s for r in hard) for s in {r[arm]["status"] for r in hard}},
            "paired_means": {
                key: statistics.mean(r[arm]["metrics"][key] for r in paired) if paired else None
                for key in ("changed_items", "start_shift_minutes", "cross_day_moves", "locked_violations")
            },
            "outside_initial_neighborhood": sum(r[arm]["outside_initial_neighborhood"] for r in paired),
            "outside_selected_scope": sum(r[arm]["outside_selected_scope"] for r in paired),
            "median_seconds": statistics.median(r[arm]["seconds"] for r in hard if "seconds" in r[arm]),
        }
    report = {"summary": summary, "rows": rows, "new_model_calls": 0, "new_http_requests": 0}
    save(output / "report.json", report)
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--repeats", type=int, choices=range(1, 4), default=3)
    args = parser.parse_args()
    print(json.dumps(evaluate(args.output, args.repeats)["summary"], ensure_ascii=False))
