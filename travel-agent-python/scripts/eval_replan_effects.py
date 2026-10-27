"""成组冻结重排对照：完整局部产品路径 vs 无扰动目标全局求解；全失败保留。"""

import json
import statistics
import sys
import time
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.agent.generation.content.constraint_replan_check import (
    item_positions,
    minute,
    schedule_metrics,
    validate_replan_schedule,
)
from app.agent.generation.content.constraint_replan_model import ReplanModel
from app.agent.generation.content.constraint_trip_solver import solve_trip
from app.agent.generation.orchestration.constraint_replanning import _proposal, run_constraint_replan
from app.agent.generation.orchestration.replan_snapshot import create_snapshot, effective_requirements
from app.agent.generation.rules.day_policy import day_policy_for
from app.schemas.replanning import ReplanChange
from app.schemas.trip_requirements import FixedArrangement, PlaceRelation, RequiredPlace
from scripts.align_p4_scenario import local_only
from scripts.run_p4_acceptance import save
from tests.replanning_cases import case

BASES = [
    {"id": "single-spaced", "split": "development", "count": 4, "days": 1},
    {"id": "two-day-locked", "split": "development", "count": 3, "days": 2},
    {"id": "precedence", "split": "evaluation", "count": 4, "days": 1},
    {"id": "window-opening", "split": "evaluation", "count": 3, "days": 1},
    {"id": "cross-day-required", "split": "evaluation", "count": 2, "days": 2},
    {"id": "optional-departure", "split": "evaluation", "count": 4, "days": 1},
]
ARM_SECONDS = 0.6


def environment(spec):
    request, plans, routes = case(count=spec["count"], days=spec["days"])
    protected = [1]
    optional = []
    for day in plans:
        for index, item in enumerate(day["items"]):
            start = 540 + index * 50
            item.update(
                start_time=f"{start // 60:02d}:{start % 60:02d}",
                end_time=f"{(start + 30) // 60:02d}:{(start + 30) % 60:02d}",
            )
            item["benchmark_opening_windows"] = [[540, 1080]]
            fact = {
                "provider": "synthetic-benchmark",
                "source_url": "https://example.com/replan-frozen",
                "retrieved_at": "2026-10-24T08:00:00Z",
                "verification_status": "partially_verified",
                "value_kind": "observed",
            }
            item["fact_evidence"] = {
                "cost": fact,
                "duration_min": fact,
                "open_time": {
                    **fact,
                    "opening_hours": {
                        "status": "known",
                        "valid_from": "2026-10-25",
                        "valid_to": "2026-10-27",
                        "rules": [{"intervals": [{"start": "09:00", "end": "18:00"}], "source_text": "合成冻结开放窗"}],
                    },
                },
            }
    req = request.requirements_struct
    assert req is not None
    if spec["id"] == "two-day-locked":
        locked = plans[1]["items"][0]
        req.fixed_arrangements = [
            FixedArrangement(
                day_no=2,
                name=locked["poi_name"],
                item_type="attraction",
                start_time=locked["start_time"],
                end_time=locked["end_time"],
            )
        ]
    elif spec["id"] == "precedence":
        req.place_relations = [
            PlaceRelation(constraint_id="order-2-4", kind="before", place_names=["P2", "P4"], day_no=1)
        ]
    elif spec["id"] == "window-opening":
        req.day_windows[0].finish_by = "12:00"
        point = plans[0]["items"][2]
        point["benchmark_opening_windows"] = [[640, 720]]
        point["fact_evidence"]["open_time"]["opening_hours"]["rules"][0]["intervals"] = [
            {"start": "10:40", "end": "12:00"}
        ]
    elif spec["id"] == "cross-day-required":
        protected = []
        req.day_windows[0].finish_by = "10:40"
        req.required_places = [RequiredPlace(constraint_id="required-p2", name="P2", day_no=1)]
    elif spec["id"] == "optional-departure":
        optional = [3, 4]
    return request, plans, routes, protected, optional


def events(spec):
    last = spec["count"]
    return [
        ReplanChange(kind="delay", item_id=2, delay_minutes=20),
        ReplanChange(kind="move", item_id=last, day_no=1, start_time="09:00"),
        ReplanChange(kind="appointment", item_id=2, day_no=1, start_time="09:20", end_time="10:20"),
        ReplanChange(kind="finish_by", day_no=1, end_time="10:40"),
        ReplanChange(kind="close", item_id=2),
        ReplanChange(kind="prefer", item_id=last),
    ]


def independent_errors(request, before, after, event, protected, matrix, optional):
    errors = validate_replan_schedule(before, after, event, protected, matrix, optional)
    req = effective_requirements(request, before, event)
    positions = item_positions(after)
    by_name = {p["poi_name"]: (day, index, p) for day, index, p in positions.values()}
    original = item_positions(before)
    for day in after:
        policy = day_policy_for(req, day["day_no"])
        active = 0
        for item in day["items"]:
            start, end = minute(item["start_time"]), minute(item["end_time"])
            if start < (policy.window_start_min or 0) or end > (policy.window_end_min or 1440):
                errors.append("超出需求日窗")
            if not any(a <= start < end <= b for a, b in original[item["id"]][2]["benchmark_opening_windows"]):
                errors.append("不在冻结营业窗")
            active += end - start
        if active < policy.min_active_minutes:
            errors.append("不满足原交付活动时长")
        if sum(p["item_type"] == "attraction" for p in day["items"]) > policy.max_attractions:
            errors.append("超出每日景点数")
    for fixed in req.fixed_arrangements:
        found = by_name.get(fixed.name)
        if (
            not found
            or found[0] != fixed.day_no
            or found[2]["start_time"][:5] != fixed.start_time
            or found[2]["end_time"][:5] != fixed.end_time
        ):
            errors.append("固定安排被破坏")
    for required in req.required_places:
        found = by_name.get(required.name)
        if not found or (required.day_no and found[0] != required.day_no):
            errors.append("必去要求被破坏")
    for relation in req.place_relations:
        matched = [by_name[n] for n in relation.place_names if n in by_name]
        if relation.kind == "before" and (
            len(matched) != 2
            or matched[0][:2] >= matched[1][:2]
            or (relation.day_no and any(m[0] != relation.day_no for m in matched))
        ):
            errors.append("先后关系被破坏")
    return errors


def run_arm(spec, event, arm):
    request, before, routes, completed, optional = environment(spec)
    started = time.monotonic()
    snapshot = create_snapshot(request, before, event, completed, routes)
    problems, prepared, previous, protected, req, matrix, start = snapshot
    proposal = None
    attempts = []
    if arm == "local":
        response, proposals = run_constraint_replan(
            request,
            before,
            event,
            completed,
            routes,
            optional_item_ids=optional,
            time_limit_s=max(0.001, ARM_SECONDS - (time.monotonic() - started)),
        )
        attempts = [a.model_dump(mode="json") for a in response.attempts]
        chosen = next(
            (o for o in response.options if o.key == "minimal"), response.options[0] if response.options else None
        )
        status = chosen.numerical_status if chosen else attempts[-1]["status"] if attempts else "UNKNOWN"
        if chosen:
            proposal = proposals[chosen.key]
    else:
        results = solve_trip(
            problems,
            budget=Decimal(str(request.budget)),
            time_limit_s=max(0.001, ARM_SECONDS - (time.monotonic() - started)),
            repair=ReplanModel(
                previous,
                event,
                {v.item_id for v in previous},
                strategy="global",
                cross_day=True,
                removable=set(optional),
            ),
        )
        status = results[0].solver_status
        if results[0].total_tiers and results[0].optimized_tiers:
            proposal = _proposal(request, before, results, prepared, matrix, start, req, completed)
    errors = independent_errors(request, before, proposal, event, protected, matrix, optional) if proposal else []
    metrics = schedule_metrics(before, proposal, matrix, protected)[0].model_dump(mode="json") if proposal else None
    return {
        "status": status,
        "feasible_checked": proposal is not None and not errors,
        "checker_errors": errors,
        "metrics": metrics,
        "seconds": round(time.monotonic() - started, 6),
        "attempts": attempts,
        "proposal": proposal,
    }


def summarize(rows):
    paired = [r for r in rows if r["local"]["feasible_checked"] and r["global"]["feasible_checked"]]
    metrics = {}
    for key in (
        "changed_items",
        "cross_day_moves",
        "start_shift_minutes",
        "mean_start_shift_minutes",
        "locked_violations",
        "removed_items",
    ):
        metrics[key] = {arm: sum(r[arm]["metrics"][key] for r in paired) for arm in ("local", "global")}
    return {
        "events": len(rows),
        "both_feasible": len(paired),
        "local_feasible": sum(r["local"]["feasible_checked"] for r in rows),
        "global_feasible": sum(r["global"]["feasible_checked"] for r in rows),
        "paired_sums": metrics,
        "changed_items_wins_ties_losses": [
            sum(r["local"]["metrics"]["changed_items"] < r["global"]["metrics"]["changed_items"] for r in paired),
            sum(r["local"]["metrics"]["changed_items"] == r["global"]["metrics"]["changed_items"] for r in paired),
            sum(r["local"]["metrics"]["changed_items"] > r["global"]["metrics"]["changed_items"] for r in paired),
        ],
        "seconds_median": {
            arm: statistics.median(r[arm]["seconds"] for r in rows) if rows else None for arm in ("local", "global")
        },
    }


def evaluate(output):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    save(
        output / "design.json",
        {
            "bases": BASES,
            "events_per_base": 6,
            "arm_seconds": ARM_SECONDS,
            "selection": "local minimal if retained else first product nondominated option; global standard objectives",
            "split": (
                "new structural evaluation combinations, fixed before execution; author-visible, not blind holdout"
            ),
            "scope": (
                "frozen synthetic facts, 1–2 days, no model/network; no statistical independence of derived events"
            ),
        },
    )
    rows = []
    deadline = time.monotonic() + 55
    with local_only():
        for base in BASES:
            for index, event in enumerate(events(base)):
                if time.monotonic() >= deadline:
                    save(output / "incomplete.json", {"rows_completed": len(rows)})
                    break
                row = {"base_id": base["id"], "split": base["split"], "event": event.model_dump(mode="json")}
                for arm in ("local", "global") if index % 2 == 0 else ("global", "local"):
                    try:
                        row[arm] = run_arm(base, event, arm)
                    except Exception as exc:
                        row[arm] = {
                            "status": "ERROR",
                            "error_type": type(exc).__name__,
                            "error": str(exc),
                            "feasible_checked": False,
                            "checker_errors": [],
                            "metrics": None,
                            "seconds": 0,
                        }
                rows.append(row)
    report = {
        "rows": rows,
        "summary": {
            split: summarize([r for r in rows if r["split"] == split]) for split in ("development", "evaluation")
        },
        "new_model_calls": 0,
        "new_http_requests": 0,
        "paid_cny": "0",
    }
    save(output / "report.json", report)
    return report


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    print(json.dumps(evaluate(args.output)["summary"], ensure_ascii=False))
