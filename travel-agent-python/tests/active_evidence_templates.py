"""与双固定预约模板不同的约束结构；真值仅在工具/独立判官中使用。"""

import json
from dataclasses import replace
from datetime import date
from decimal import Decimal
from pathlib import Path

from app.agent.generation.content.constraint_facts import prepare_candidates
from app.agent.generation.content.constraint_trip_solver import solve_trip
from app.agent.generation.content.evidence_gaps import decision_signature
from app.agent.generation.content.evidence_observations import FactObservation
from app.agent.generation.orchestration.active_evidence import drive_evidence
from app.agent.generation.orchestration.constraint_planning import _problem_snapshots
from app.schemas.active_evidence import ActiveEvidenceOptions
from app.schemas.planning import ConstraintPlanningOptions
from app.schemas.trip import FactEvidence, GenerateRequest
from app.schemas.trip_requirements import BudgetPolicy, DayWindow, FixedArrangement, PlaceRelation, TripRequirements


def cases():
    return json.loads(
        (Path(__file__).parent / "fixtures/constraint_planning/p4_constraint_templates.json").read_text(
            encoding="utf-8"
        )
    )["cases"]


def evidence(*, opening=False):
    value: dict = {
        "provider": "synthetic-template",
        "source_url": "https://example.com/p4-constraint-template",
        "retrieved_at": "2026-10-24T08:00:00Z",
        "verification_status": "partially_verified",
        "value_kind": "observed",
    }
    if opening:
        value["opening_hours"] = {
            "status": "known",
            "valid_from": "2026-10-25",
            "valid_to": "2026-10-25",
            "rules": [{"intervals": [{"start": "09:00", "end": "18:00"}], "source_text": "合成开放资料"}],
        }
    return value


def environment(case):
    # 此函数只消费 observed 配置，不读取 hidden_truth/expected_*。
    departure = case["kind"] == "departure"
    options = ConstraintPlanningOptions(
        country_code=case["country"],
        time_zone=case["zone"],
        currency=case["currency"],
        active_evidence=ActiveEvidenceOptions(
            strategy="bottleneck", max_queries=2, budget_units=2, time_limit_seconds=4
        ),
    )
    requirements = TripRequirements(
        constraint_planning=options,
        max_attractions_per_day=3 if departure else 1,
        budget_policy=BudgetPolicy(mode="hard_cap"),
        day_windows=[
            DayWindow(
                day_no=1,
                kind="departure" if departure else "arrival",
                not_before="09:00",
                finish_by="15:00" if departure else "13:00",
            )
        ],
        place_relations=[
            PlaceRelation(
                constraint_id="new-template", kind="before" if departure else "one_of", place_names=["A", "B"], day_no=1
            )
        ],
        fixed_arrangements=[
            FixedArrangement(day_no=1, name="C", item_type="attraction", start_time="14:00", end_time="15:00")
        ]
        if departure
        else [],
        excluded_places=["A"] if case["kind"] == "excluded" else [],
    )
    request = GenerateRequest(
        city=case["city"], days=1, persons=1, budget=100, start_date="2026-10-25", requirements_struct=requirements
    )
    pool = []
    for name in ["A", "B", "C"] if departure else ["A", "B"]:
        facts = {"cost": evidence(), "duration_min": evidence(), "open_time": evidence(opening=True)}
        if not departure and name == "A":
            facts.pop("cost")
        pool.append(
            {
                "name": name,
                "id": f"template:{name}",
                "source": "synthetic-template",
                "country_code": case["country"],
                "duration_min": (60 if name == "C" else 120) if departure else 180,
                "cost": 20 if name == "A" else 40 if name == "B" else 10,
                "cost_currency": case["currency"],
                "fact_evidence": facts,
            }
        )
    prepared = prepare_candidates({"candidates": pool}, options)
    _, problems = _problem_snapshots(request, prepared, date(2026, 10, 25))
    candidates = tuple(replace(c, utility=10 if c.name == "A" else 1) for c in problems[0].candidates)
    matrix = {
        (a.candidate_id, b.candidate_id): {"duration_min": 0, "source": "osrm"}
        for a in candidates
        for b in candidates
        if a != b
    }
    if departure:
        ids = {c.name: c.candidate_id for c in candidates}
        matrix[ids["B"], ids["C"]] = {"duration_min": 0, "source": "coordinate-estimate"}
    problems[0] = replace(problems[0], candidates=candidates, route_matrix=matrix)
    return request, prepared, problems


def independent_check(case, results):
    """直接检查所选点/时刻/价格与整数总时长，不读取 CP 编译器。"""
    visits = results[0].visits
    if case["kind"] == "departure":
        feasible = 120 + 120 + case["hidden_truth"]["route_minutes"] + 60 <= 360
        if not feasible:
            return decision_signature(results)[0] == "INFEASIBLE"
        return False  # 当前固定的 departure 真值明确不可行，不能用空计划冒充通过。
    prices = {"A": case["hidden_truth"]["price_a"], "B": 40}
    allowed = [n for n in ("A", "B") if prices[n] <= 100 and not (case["kind"] == "excluded" and n == "A")]
    # 独立枚举：每天最多一项，one_of 至少一项；选可负担/未排除的最高收益。
    expected = max(allowed, key=lambda n: 10 if n == "A" else 1)
    names = ["A" if v.candidate_id == "cp-0" else "B" for v in visits]
    return names == [expected] and all(
        v.start_min >= 540 and v.end_min <= 780 and v.end_min - v.start_min == 180 for v in visits
    )


def evaluate():
    rows = []
    for case in cases():
        for strategy, seeds in (("fixed", [0]), ("random", [0, 1, 2]), ("confidence", [0]), ("bottleneck", [0])):
            for seed in seeds:
                request, prepared, problems = environment(case)
                assert request.requirements_struct is not None
                assert request.requirements_struct.constraint_planning is not None
                assert request.requirements_struct.constraint_planning.active_evidence is not None
                truth = case["hidden_truth"]

                def observe(gap, current, candidates, req, options, *, truth=truth):
                    ids = tuple(
                        next(p.fact.source_entity_id for p in candidates if p.fact.candidate_id == key)
                        for key in (gap.first_id, gap.second_id)
                        if key is not None
                    )
                    fact = FactEvidence.model_validate(evidence())
                    if gap.kind == "route":
                        route = {
                            "from_poi_id": gap.first_id,
                            "to_poi_id": gap.second_id,
                            "source": "osrm",
                            "duration_min": truth["route_minutes"],
                            "time_dependent": False,
                        }
                        return FactObservation("route", ids, route=route, fact=fact, tool="frozen_template_route")
                    return FactObservation(
                        "price",
                        ids,
                        value=truth["price_a"],
                        currency=req.requirements_struct.constraint_planning.currency,
                        fact=fact,
                        tool="frozen_template_price",
                    )

                def solve(current, seconds):
                    return solve_trip(current, budget=Decimal(100), time_limit_s=seconds)

                results, report = drive_evidence(
                    problems,
                    prepared,
                    request,
                    options=request.requirements_struct.constraint_planning.active_evidence,
                    solve=solve,
                    observe=observe,
                    strategy=strategy,
                    seed=seed,
                    budget=Decimal(100),
                )
                rows.append(
                    {
                        "case": case["id"],
                        "strategy": strategy,
                        "seed": seed,
                        "passed": independent_check(case, results),
                        "decision": decision_signature(results),
                        "report": report.model_dump(mode="json"),
                    }
                )
    return {
        "method": "three new synthetic constraint structures; same limits; independent rule oracle; zero network/model",
        "rows": rows,
    }
