"""Six development recipes and twelve held-out task instances; hidden facts only reach the tool/oracle."""

import random
from datetime import date
from decimal import Decimal

from app.agent.generation.content.constraint_facts import planning_candidate, prepare_candidates
from app.agent.generation.content.constraint_problem import DayPlanningProblem
from app.schemas.active_evidence import ActiveEvidenceOptions
from app.schemas.planning import ConstraintPlanningOptions
from app.schemas.trip import GenerateRequest
from app.schemas.trip_requirements import (
    BudgetPolicy,
    DayWindow,
    FixedArrangement,
    PlaceRelation,
    RequiredPlace,
    TripRequirements,
)


def hhmm(value):
    return f"{value // 60:02d}:{value % 60:02d}"


def specifications():
    cases = []
    for index in range(18):
        development = index < 6
        kind = index if development else (index - 6) // 2
        variant = 0 if development else (index - 6) % 2
        seed = 9300 + index
        rows = {
            name: {
                "name": f"场馆{seed}-{name}",
                "duration": 60,
                "price": 20,
                "opening": [[540, 960]],
                "utility": 5 if name == "A" else 4 if name == "B" else 1,
            }
            for name in "ABC"
        }
        required, relations, fixed, excluded = [], [], [], []
        unknown, missing_routes, routes = [], [], {}
        budget, units, cap, end = 100, 4, 2, 900

        def before(a="A", b="B", relations=relations, rows=rows):
            relations.append(
                {
                    "constraint_id": f"order-{len(relations)}",
                    "kind": "before",
                    "day_no": 1,
                    "place_names": [rows[a]["name"], rows[b]["name"]],
                    "strength": "hard",
                }
            )

        if development:
            if kind == 0:
                required = ["A"]
                cap = 1
                units = 2
                rows["A"]["price"] = 120
                unknown = [("A", "price"), ("B", "opening")]
            elif kind == 1:
                required = ["A"]
                cap = 1
                units = 3
                rows["A"]["opening"] = []
                unknown = [("A", "opening"), ("B", "price")]
            elif kind == 2:
                before()
                end = 690
                units = 2
                routes["A,B"] = 45
                missing_routes = ["A,B"]
                unknown = [("C", "price")]
            elif kind == 3:
                cap = 1
                units = 3
                relations.append(
                    {
                        "constraint_id": "choice",
                        "kind": "one_of",
                        "day_no": 1,
                        "place_names": [rows["A"]["name"], rows["B"]["name"]],
                    }
                )
                rows["A"]["price"] = 150
                rows["B"]["opening"] = []
                unknown = [("A", "price"), ("B", "opening")]
            elif kind == 4:
                before()
                before("B", "A")
                unknown = [("A", "price"), ("B", "price"), ("C", "opening")]
            else:
                required = ["A"]
                cap = 1
                units = 8
                unknown = [("B", "price"), ("B", "opening"), ("C", "price"), ("C", "opening")]
        else:
            if kind == 0:
                before()
                budget = 60
                units = 2
                rows["A"]["price"] = 20 if variant == 0 else 80
                unknown = [("A", "price"), ("C", "opening")]
            elif kind == 1:
                cap = 1
                units = 3
                excluded = [rows["C"]["name"]]
                relations.append(
                    {
                        "constraint_id": "choice",
                        "kind": "one_of",
                        "day_no": 1,
                        "place_names": [rows["A"]["name"], rows["B"]["name"]],
                    }
                )
                rows["A"]["opening"] = [[540, 960]] if variant == 0 else []
                rows["B"]["price"] = 30 + variant * 10
                unknown = [("A", "opening"), ("B", "price")]
            elif kind == 2:
                cap = 2
                units = 3
                fixed = [
                    {
                        "day_no": 1,
                        "name": rows[n]["name"],
                        "item_type": "attraction",
                        "start_time": start,
                        "end_time": finish,
                    }
                    for n, start, finish in (("A", "09:00", "10:00"), ("B", "10:30", "11:30"))
                ]
                routes["A,B"] = 20 if variant == 0 else 45
                missing_routes = ["A,B"]
                unknown = [("C", "price"), ("C", "opening")]
            elif kind == 3:
                before()
                units = 2
                end = 720
                rows["A"]["duration"] = 90 if variant == 0 else 150
                unknown = [("A", "duration"), ("C", "opening")]
            elif kind == 4:
                before()
                budget = 70
                units = 3
                relations.append(
                    {
                        "constraint_id": "choice",
                        "kind": "one_of",
                        "day_no": 1,
                        "place_names": [rows["B"]["name"], rows["C"]["name"]],
                    }
                )
                rows["A"]["price"] = 20 if variant == 0 else 90
                rows["B"]["opening"] = [[570, 720], [780, 960]]
                unknown = [("A", "price"), ("B", "opening")]
            else:
                before()
                units = 2
                end = 640
                rows["A"]["duration"] = rows["B"]["duration"] = 30 if variant == 0 else 60
                unknown = [("A", "duration"), ("B", "duration"), ("C", "opening")]
        order = list(rows)
        random.Random(seed).shuffle(order)
        candidates = [
            {**rows[name], "letter": name, "id": f"cp-{i}", "entity": f"world:{seed}:{name}"}
            for i, name in enumerate(order)
        ]
        matrix = [
            [a["id"], b["id"], routes.get(a["letter"] + "," + b["letter"], 10 + variant * 2)]
            for a in candidates
            for b in candidates
            if a is not b
        ]
        by_letter = {c["letter"]: c["id"] for c in candidates}
        cases.append(
            {
                "id": f"evidence-{index:02}",
                "split": "dev" if development else "confirm",
                "family": ("development-" if development else "confirmation-composite-") + str(kind),
                "seed": seed,
                "hidden_candidates": candidates,
                "truth_routes": matrix,
                "unknown": [[by_letter[n], field] for n, field in unknown],
                "unknown_routes": [
                    [by_letter[a], by_letter[b]] for a, b in (pair.split(",") for pair in missing_routes)
                ],
                "required": [rows[n]["name"] for n in required],
                "relations": relations,
                "fixed": fixed,
                "excluded": excluded,
                "budget": budget,
                "units": units,
                "cap": cap,
                "end": end,
            }
        )
    return cases


def evidence(opening=None):
    result: dict[str, object] = {
        "source_url": "https://example.com/frozen-tool-world",
        "retrieved_at": "2026-10-26T00:00:00+08:00",
        "provider": "frozen_tool_world",
        "verification_status": "partially_verified",
        "value_kind": "observed",
    }
    if opening is not None:
        result["opening_hours"] = {
            "status": "known",
            "rules": [
                {
                    "closed": not bool(opening),
                    "intervals": [{"start": hhmm(a), "end": hhmm(b)} for a, b in opening],
                    "source_text": "自制冻结世界，不是在线资料",
                }
            ],
        }
    return result


def environment(case, *, truth=False):
    req = TripRequirements(
        constraint_planning=ConstraintPlanningOptions(
            country_code="CN",
            time_zone="Asia/Shanghai",
            currency="CNY",
            active_evidence=ActiveEvidenceOptions(max_queries=6, budget_units=case["units"], time_limit_seconds=5),
        ),
        budget_policy=BudgetPolicy(mode="hard_cap"),
        max_attractions_per_day=case["cap"],
        day_windows=[DayWindow(day_no=1, not_before="09:00", finish_by=hhmm(case["end"]))],
        required_places=[
            RequiredPlace(constraint_id=f"required-{i}", name=name, day_no=1) for i, name in enumerate(case["required"])
        ],
        place_relations=[PlaceRelation.model_validate(r) for r in case["relations"]],
        fixed_arrangements=[FixedArrangement.model_validate(f) for f in case["fixed"]],
        excluded_places=case["excluded"],
    )
    request = GenerateRequest(
        city=f"匿名城市{case['seed']}",
        days=1,
        persons=1,
        start_date="2026-11-05",
        budget=case["budget"],
        requirements_struct=req,
    )
    rows = []
    missing = set() if truth else {tuple(row) for row in case["unknown"]}
    for item in case["hidden_candidates"]:
        facts = {
            field: evidence(item["opening"] if field == "open_time" else None)
            for field, kind in (("cost", "price"), ("open_time", "opening"), ("duration_min", "duration"))
            if (item["id"], kind) not in missing
        }
        rows.append(
            {
                "id": item["entity"],
                "name": item["name"],
                "source": "frozen_tool_world",
                "country_code": "CN",
                "source_url": "https://example.com/frozen-tool-world",
                "duration_min": item["duration"] if (item["id"], "duration") not in missing else None,
                "cost": item["price"] if (item["id"], "price") not in missing else None,
                "cost_currency": "CNY",
                "fact_evidence": facts,
                "utility": item["utility"],
                "categories": [],
            }
        )
    options = req.constraint_planning
    assert options is not None
    prepared = prepare_candidates({"candidates": rows}, options)
    candidates = tuple(planning_candidate(p, options, date(2026, 11, 5), 1) for p in prepared)
    absent = set() if truth else {tuple(edge) for edge in case["unknown_routes"]}
    matrix = {
        (a, b): {
            "duration_min": minutes,
            "source": "osrm",
            "source_url": "https://example.com/frozen-tool-world",
            "measurement_scope": "frozen_tool_world",
        }
        for a, b, minutes in case["truth_routes"]
        if (a, b) not in absent
    }
    problem = DayPlanningProblem(req, candidates, matrix, "CNY", budget=Decimal(case["budget"]))
    return request, prepared, [problem]
