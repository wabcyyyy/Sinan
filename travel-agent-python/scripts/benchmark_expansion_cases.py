"""Frozen synthetic expansion; seeds are instances nested within declared scenario families."""

from dataclasses import asdict, replace
from decimal import Decimal
from random import Random

from app.agent.generation.content.constraint_problem import DayPlanningProblem, PlanningCandidate
from app.schemas.planning import ConstraintPlanningOptions
from app.schemas.replanning import ReplanChange
from app.schemas.trip import GenerateRequest
from app.schemas.trip_requirements import (
    BudgetPolicy,
    DayWindow,
    FixedArrangement,
    PlaceRelation,
    RequiredPlace,
    TripRequirements,
)

KINDS = ("delay", "move", "appointment", "finish_by", "close")
FEASIBLE = ("windows", "split_opening", "directed_route", "budget", "choice", "before")
CONFLICTS = ("opening", "day_window", "order_cycle", "excluded_choice", "budget", "route_booking")


def hhmm(value):
    return f"{value // 60:02d}:{value % 60:02d}"


def replan_specs():
    return [
        {
            "id": f"expanded-{i:02}",
            "family": f"layout-{i // 5:02}",
            "seed": 7000 + i,
            "days": 2 + (i // 5) % 2,
            "count": 3 + (i // 10) % 2,
            "kind": KINDS[i % 5],
        }
        for i in range(60)
    ]


def replan_environment(spec):
    rng = Random(spec["seed"])
    req = TripRequirements(
        constraint_planning=ConstraintPlanningOptions(country_code="FR", time_zone="Europe/Paris", currency="EUR"),
        day_windows=[DayWindow(day_no=d, not_before="08:00", finish_by="15:00") for d in range(1, spec["days"] + 1)],
        max_attractions_per_day=6,
        budget_policy=BudgetPolicy(mode="hard_cap"),
    )
    plans = []
    for d in range(1, spec["days"] + 1):
        items, start = [], 540 + (spec["seed"] % 3) * 10
        for j in range(spec["count"]):
            key = (d - 1) * spec["count"] + j + 1
            duration = rng.choice((30, 40, 50))
            evidence = {
                "provider": "synthetic-expansion",
                "source_url": "https://example.com/frozen",
                "retrieved_at": "2026-10-25T00:00:00Z",
                "verification_status": "partially_verified",
                "value_kind": "observed",
            }
            opening = [[480, 900]]
            if spec["seed"] % 4 == 0 and j == 0:
                opening = [[480, 720], [780, 900]]
            items.append(
                {
                    "id": key,
                    "poi_name": f"P{key}",
                    "item_type": "attraction",
                    "country_code": "FR",
                    "source": "synthetic-expansion",
                    "cost": rng.randint(5, 20),
                    "cost_currency": "EUR",
                    "start_time": hhmm(start),
                    "end_time": hhmm(start + duration),
                    "duration_min": duration,
                    "benchmark_opening_windows": opening,
                    "fact_evidence": {
                        "cost": evidence,
                        "duration_min": evidence,
                        "open_time": {
                            **evidence,
                            "opening_hours": {
                                "status": "known",
                                "valid_from": "2026-10-25",
                                "valid_to": "2026-10-27",
                                "rules": [
                                    {
                                        "intervals": [{"start": hhmm(a), "end": hhmm(b)} for a, b in opening],
                                        "source_text": "synthetic frozen hours",
                                    }
                                ],
                            },
                        },
                    },
                }
            )
            start += duration + rng.choice((15, 20, 25))
        plans.append({"day_no": d, "items": items, "note": "frozen expansion"})
    fixed = plans[1]["items"][0]
    req.fixed_arrangements = [
        FixedArrangement(
            day_no=2,
            name=fixed["poi_name"],
            item_type="attraction",
            start_time=fixed["start_time"],
            end_time=fixed["end_time"],
        )
    ]
    req.required_places = [RequiredPlace(constraint_id="trip-must", name=plans[-1]["items"][-1]["poi_name"])]
    if spec["seed"] % 2 == 0:
        req.place_relations = [
            PlaceRelation(
                constraint_id="day2-order",
                kind="before",
                day_no=2,
                place_names=[p["poi_name"] for p in plans[1]["items"][:2]],
            )
        ]
    request = GenerateRequest(
        city="Paris", days=spec["days"], persons=1, budget=500, start_date="2026-10-25", requirements_struct=req
    )
    all_items = [p for day in plans for p in day["items"]]
    routes = [
        {
            "fromItemId": a["id"],
            "toItemId": b["id"],
            "route": {"duration_min": rng.randint(3, 12), "source": "synthetic-directed"},
        }
        for a in all_items
        for b in all_items
        if a is not b
    ]
    optional = [plans[0]["items"][-1]["id"]] if spec["kind"] == "finish_by" else []
    return request, plans, routes, [1], optional


def replan_event(spec):
    _, plans, _, _, _ = replan_environment(spec)
    target = plans[0]["items"][1]
    level = spec["seed"] // 5 % 4
    if spec["kind"] == "delay":
        return ReplanChange(kind="delay", item_id=2, delay_minutes=(15, 30, 60, 90)[level])
    if spec["kind"] == "move":
        return ReplanChange(kind="move", item_id=2, day_no=2, start_time=hhmm(780 + 10 * level))
    if spec["kind"] == "appointment":
        start = 750 + level * 10
        return ReplanChange(
            kind="appointment",
            item_id=2,
            day_no=1,
            start_time=hhmm(start),
            end_time=hhmm(start + target["duration_min"]),
        )
    if spec["kind"] == "finish_by":
        return ReplanChange(kind="finish_by", day_no=1, end_time=hhmm(690 + level * 15))
    return ReplanChange(kind="close", item_id=2)


def kernel_specs():
    rows = []
    for days in (1, 2, 3):
        variants = range(2) if days == 1 else range(1)
        for variant in variants:
            for expected, kinds in (("FEASIBLE", FEASIBLE), ("INFEASIBLE", CONFLICTS)):
                for kind in kinds:
                    rows.append(
                        {
                            "id": f"kernel-{days}d-{kind}-{variant}",
                            "days": days,
                            "kind": kind,
                            "expected": expected,
                            "seed": 8100 + len(rows),
                        }
                    )
    for kind in ("price", "opening", "route"):
        for variant in range(2):
            rows.append(
                {
                    "id": f"kernel-unknown-{kind}-{variant}",
                    "days": 1,
                    "kind": f"unknown_{kind}",
                    "expected": "UNKNOWN",
                    "seed": 8100 + len(rows),
                }
            )
    return rows


def kernel_problem(spec):
    rng = Random(spec["seed"])
    size = 5 if spec["days"] == 1 else 6
    candidates = [
        PlanningCandidate(
            f"p{i}",
            f"地点{i}",
            30 if i == 0 else rng.choice((30, 40, 45)),
            1000,
            "CNY",
            ((480, 1080),),
            utility=rng.randint(1, 9),
        )
        for i in range(size)
    ]
    req = TripRequirements(
        day_windows=[DayWindow(day_no=d, not_before="09:00", finish_by="16:00") for d in range(1, spec["days"] + 1)],
        required_places=[RequiredPlace(constraint_id="must-p1", name="地点1", day_no=1)],
        fixed_arrangements=[
            FixedArrangement(day_no=1, name="地点0", item_type="attraction", start_time="10:00", end_time="10:30")
        ],
        place_relations=[
            PlaceRelation(constraint_id="order", kind="before", day_no=1, place_names=["地点0", "地点1"]),
            PlaceRelation(constraint_id="choice", kind="one_of", day_no=1, place_names=["地点2", "地点3"]),
        ],
        budget_policy=BudgetPolicy(mode="hard_cap"),
        max_attractions_per_day=5,
    )
    if spec["days"] > 1:
        req.required_places.append(RequiredPlace(constraint_id="trip-must", name="地点5"))
    routes = {
        (a.candidate_id, b.candidate_id): {"duration_min": rng.randint(4, 18), "source": "frozen-directed"}
        for a in candidates
        for b in candidates
        if a is not b
    }
    budget, kind = Decimal(60), spec["kind"]
    if kind == "split_opening":
        candidates[1] = replace(candidates[1], opening_windows=((660, 720), (780, 930)))
    elif kind == "directed_route":
        routes["p0", "p1"]["duration_min"] = 75
        routes["p1", "p0"]["duration_min"] = 3
    elif kind == "budget":
        budget = Decimal(0 if spec["expected"] == "INFEASIBLE" else 40)
    elif kind == "choice":
        req.excluded_places = ["地点3"]
    elif kind == "before":
        req.place_relations.append(
            PlaceRelation(
                constraint_id="soft",
                kind="before",
                day_no=1,
                place_names=["地点3", "地点0"],
                strength="soft",
                priority=5,
            )
        )
    elif kind == "opening":
        candidates[0] = replace(candidates[0], opening_windows=((720, 900),))
    elif kind == "day_window":
        req.day_windows[0].finish_by = "10:15"
    elif kind == "order_cycle":
        req.place_relations.append(
            PlaceRelation(constraint_id="reverse", kind="before", day_no=1, place_names=["地点1", "地点0"])
        )
    elif kind == "excluded_choice":
        req.excluded_places = ["地点2", "地点3"]
    elif kind == "route_booking":
        req.fixed_arrangements.append(
            FixedArrangement(
                day_no=1,
                name="地点1",
                item_type="attraction",
                start_time="10:30",
                end_time=hhmm(630 + candidates[1].duration_min),
            )
        )
    elif kind == "unknown_price":
        candidates[0] = replace(candidates[0], price_minor=None)
    elif kind == "unknown_opening":
        candidates[0] = replace(candidates[0], opening_windows=None)
    elif kind == "unknown_route":
        routes = {}
    problems = [
        DayPlanningProblem(req, tuple(candidates), routes, "CNY", day_no=d, budget=budget)
        for d in range(1, spec["days"] + 1)
    ]
    return problems, budget


def serialize_kernel(problems, budget):
    return {
        "requirements": problems[0].requirements.model_dump(mode="json", by_alias=True),
        "budget_minor": int(budget * 100),
        "currency": problems[0].currency,
        "candidates": [asdict(c) for c in problems[0].candidates],
        "days": len(problems),
        "day_no": 1,
        "directed_routes": [
            {"from": a, "to": b, "minutes": r["duration_min"]} for (a, b), r in problems[0].route_matrix.items()
        ],
    }
