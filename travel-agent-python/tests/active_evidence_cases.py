"""P4 合成冻结环境；observed_pool 不含 hidden_truth，取证工具才读取真相。"""

from copy import deepcopy
from datetime import date
from decimal import Decimal

from app.agent.generation.content.constraint_facts import prepare_candidates
from app.agent.generation.content.evidence_observations import FactObservation
from app.agent.generation.orchestration.constraint_planning import _problem_snapshots
from app.schemas.active_evidence import ActiveEvidenceOptions
from app.schemas.planning import ConstraintPlanningOptions
from app.schemas.trip import FactEvidence, GenerateRequest
from app.schemas.trip_requirements import BudgetPolicy, FixedArrangement, TripRequirements


def environment(case):
    options = ConstraintPlanningOptions(
        country_code="FR",
        time_zone="Europe/Paris",
        currency="EUR",
        active_evidence=ActiveEvidenceOptions(
            strategy="bottleneck", max_queries=2, budget_units=2, time_limit_seconds=4
        ),
    )
    request = GenerateRequest(
        city="Fixture City",
        days=1,
        persons=1,
        budget=100,
        start_date="2026-10-25",
        requirements_struct=TripRequirements(
            constraint_planning=options,
            max_attractions_per_day=2,
            budget_policy=BudgetPolicy(mode="hard_cap"),
            fixed_arrangements=[
                FixedArrangement(day_no=1, name="A", item_type="attraction", start_time="09:00", end_time="11:00"),
                FixedArrangement(day_no=1, name="B", item_type="attraction", start_time="11:30", end_time="13:30"),
            ],
        ),
    )
    # 所有臂看到完全相同的来源不确定快照；hidden_truth 不传给求解或 gap/probe。
    pool = {
        "candidates": [
            {
                "name": n,
                "id": f"entity-{n}",
                "source": "frozen-fixture",
                "country_code": "FR",
                "source_url": "https://example.com/frozen-p4",
                "cost": 10,
                "cost_currency": "EUR",
                "duration_min": 120,
                "open_time": "09:00-18:00",
            }
            for n in ("A", "B")
        ]
    }
    prepared = prepare_candidates(pool, options)
    _, problems = _problem_snapshots(request, prepared, date(2026, 10, 25))
    return request, prepared, problems, deepcopy(case["hidden_truth"])


def observation(gap, prepared, truth):
    ids = tuple(
        next(p.fact.source_entity_id for p in prepared if p.fact.candidate_id == key)
        for key in (gap.first_id, gap.second_id)
        if key is not None
    )
    fact = FactEvidence(
        source_url="https://example.com/frozen-p4",
        retrieved_at="2026-10-24T08:00:00Z",
        provider="hidden-truth-fixture",
        verification_status="partially_verified",
        value_kind="observed",
    )
    if gap.kind == "route":
        forward = gap.names == ("A", "B")
        route = {
            "from_poi_id": gap.first_id,
            "to_poi_id": gap.second_id,
            "duration_min": truth["forward_route"] if forward else 10,
            "source": "osrm",
            "time_dependent": False,
            "fetched_at": fact.retrieved_at,
            "source_url": fact.source_url,
        }
        return FactObservation(gap.kind, ids, route=route, fact=fact, tool="frozen_route")
    if gap.kind == "opening":
        closed = truth["closed"] == gap.names[0]
        hours = {
            "status": "known",
            "valid_from": "2026-10-25",
            "valid_to": "2026-10-25",
            "rules": [{"closed": True, "intervals": [], "source_text": "冻结闭馆"}]
            if closed
            else [{"intervals": [{"start": "09:00", "end": "18:00"}], "source_text": "冻结营业"}],
        }
        fact = FactEvidence.model_validate({**fact.model_dump(), "opening_hours": hours})
        return FactObservation(gap.kind, ids, fact=fact, tool="frozen_opening")
    return FactObservation(
        gap.kind,
        ids,
        value=truth["price_b"] if gap.names == ("B",) else 10,
        currency="EUR",
        fact=fact,
        tool="frozen_price",
    )


def solve(snapshot, seconds):
    from app.agent.generation.content.constraint_trip_solver import solve_trip

    return solve_trip(snapshot, budget=Decimal(100), time_limit_s=seconds)
