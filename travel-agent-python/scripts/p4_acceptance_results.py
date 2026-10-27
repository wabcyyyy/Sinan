"""消费一次真实验收结果；冻结策略对照与在线来源严格分开。"""

from dataclasses import asdict, replace
from datetime import date
from decimal import Decimal

from app.agent.generation.content.constraint_facts import prepare_candidates
from app.agent.generation.content.constraint_trip_solver import solve_trip
from app.agent.generation.content.evidence_gaps import decision_signature
from app.agent.generation.content.evidence_observations import FactObservation
from app.agent.generation.orchestration.active_evidence import drive_evidence
from app.agent.generation.orchestration.constraint_planning import _problem_snapshots
from app.agent.generation.orchestration.evidence_tools import query_evidence
from app.agent.generation.rules.day_policy import day_policy_for
from app.schemas.active_evidence import ActiveEvidenceOptions
from app.schemas.planning import ConstraintPlanningOptions
from app.schemas.trip import FactEvidence, GenerateRequest
from app.schemas.trip_requirements import DayWindow, FixedArrangement, TripRequirements


def synthetic_fact(*, opening=False):
    value: dict = {
        "provider": "synthetic-acceptance",
        "source_url": "https://example.com/p4-synthetic",
        "retrieved_at": "2026-10-24T08:00:00Z",
        "verification_status": "partially_verified",
        "value_kind": "observed",
    }
    if opening:
        value["opening_hours"] = {
            "status": "known",
            "valid_from": "2026-11-02",
            "valid_to": "2026-11-02",
            "rules": [{"intervals": [{"start": "09:00", "end": "18:00"}], "source_text": "合成营业资料"}],
        }
    return value


def extracted_checks(state):
    req = state.requirements
    return {
        "one_of_preserved": any(
            r.kind == "one_of" and r.place_names == ["地点A", "地点B"] and r.day_no == 1 for r in req.place_relations
        ),
        "hard_budget": bool(req.budget_policy and req.budget_policy.mode == "hard_cap" and state.budget == 100),
        "max_one": req.max_attractions_per_day == 1,
        "excluded_c": "地点C" in req.excluded_places,
        "window": any(w.day_no == 1 and w.not_before == "09:00" and w.finish_by == "13:00" for w in req.day_windows),
        "context": bool(
            req.constraint_planning
            and req.constraint_planning.currency == "EUR"
            and req.constraint_planning.time_zone == "Europe/Lisbon"
            and state.start_date == "2026-11-02"
            and state.days == 1
            and state.persons == 1
            and state.stay_nights == 0
        ),
        "no_unresolved": not req.unresolved_requests,
    }


def frozen_reference(state, duration_min):
    """两点 one_of/max1 场景的独立枚举，不读取 CP 编译器或求解结果。"""
    policy = day_policy_for(state.requirements, 1)
    input_valid = all(extracted_checks(state).values())
    feasible = []
    for name, price, utility in (("地点A", 120, 10), ("地点B", 40, 1)):
        if (
            input_valid
            and name not in state.requirements.excluded_places
            and price <= state.budget
            and policy.min_active_minutes <= duration_min <= 780 - 540
        ):
            feasible.append({"name": name, "price_eur": price, "utility": utility})
    best = max(feasible, key=lambda row: row["utility"]) if feasible else None
    return {
        "input_valid": input_valid,
        "day_kind": policy.kind,
        "min_active_minutes": policy.min_active_minutes,
        "duration_min": duration_min,
        "feasible_choices": feasible,
        "optimal_names": [best["name"]] if best else [],
        "expected_status": ("FEASIBLE" if best else "INFEASIBLE") if input_valid else None,
        "scope": "two frozen attractions, max1, one_of, EUR100, 09–13, hidden prices A120/B40",
    }


def policy_comparison(state, research, *, duration_min=180):
    checks = extracted_checks(state)
    reference = frozen_reference(state, duration_min)
    rows = []
    for strategy in ("fixed", "random", "confidence", "bottleneck"):
        req = state.requirements.model_copy(deep=True)
        req.constraint_planning.active_evidence = ActiveEvidenceOptions(
            max_queries=2, budget_units=2, time_limit_seconds=4, allow_source_search=False
        )
        request = GenerateRequest(
            city=state.city,
            days=1,
            persons=1,
            budget=state.budget,
            start_date=state.start_date,
            requirements_struct=req,
        )
        pool = []
        for name, cost in (("地点A", 20), ("地点B", 40)):
            facts = {
                "cost": synthetic_fact(),
                "duration_min": synthetic_fact(),
                "open_time": synthetic_fact(opening=True),
            }
            if name == "地点A":
                facts.pop("cost")
            pool.append(
                {
                    "name": name,
                    "id": f"synthetic:{name}",
                    "source": "synthetic-acceptance",
                    "country_code": "PT",
                    "duration_min": duration_min,
                    "cost": cost,
                    "cost_currency": "EUR",
                    "fact_evidence": facts,
                }
            )
        prepared = prepare_candidates({"candidates": pool}, req.constraint_planning)
        _, problems = _problem_snapshots(request, prepared, date(2026, 11, 2))
        candidates = tuple(replace(c, utility=10 if c.name == "地点A" else 1) for c in problems[0].candidates)
        matrix = {
            (a.candidate_id, b.candidate_id): {"duration_min": 0, "source": "osrm"}
            for a in candidates
            for b in candidates
            if a != b
        }
        problems[0] = replace(problems[0], candidates=candidates, route_matrix=matrix)

        def observe(gap, current, candidates, request, options):
            target = next(p for p in candidates if p.fact.candidate_id == gap.first_id)
            if gap.kind != "price" or target.item.poi_name != "地点A":
                return FactObservation(gap.kind, (target.fact.source_entity_id,), question="冻结环境未提供该字段真值")
            return FactObservation(
                "price",
                (target.fact.source_entity_id,),
                value=120,
                currency="EUR",
                fact=FactEvidence.model_validate(synthetic_fact()),
                tool="frozen_price",
            )

        results, report = drive_evidence(
            problems,
            prepared,
            request,
            options=req.constraint_planning.active_evidence,
            solve=lambda current, seconds: solve_trip(current, budget=Decimal(100), time_limit_s=seconds),
            observe=observe,
            budget=Decimal(100),
            strategy=strategy,
            seed=0,
        )
        names = {c.candidate_id: c.name for c in candidates}
        visits = results[0].visits
        # 独立判官只检查真实输入、所选点、隐藏价格和时刻；不复用 CP 编译断言。
        conditional_passed = (
            all(checks.values())
            and reference["expected_status"] == "FEASIBLE"
            and results[0].status in {"OPTIMAL", "FEASIBLE"}
            and [names[v.candidate_id] for v in visits] == reference["optimal_names"]
            and sum(v.end_min - v.start_min for v in visits) >= reference["min_active_minutes"]
            and all(v.start_min >= 540 and v.end_min <= 780 and v.end_min - v.start_min == duration_min for v in visits)
        )
        rows.append(
            {
                "strategy": strategy,
                "passed": conditional_passed and research.get("sufficient") is True,
                "conditional_planning_passed": conditional_passed,
                "reference_agrees": conditional_passed
                or (reference["expected_status"] == "INFEASIBLE" and results[0].status == "INFEASIBLE"),
                "selected": [names[v.candidate_id] for v in visits],
                "decision": decision_signature(results),
                "results": [asdict(r) for r in results],
                "report": report.model_dump(mode="json"),
            }
        )
    return {
        "extracted_checks": checks,
        "research_gate": research.get("sufficient"),
        "reference": reference,
        "rows": rows,
        "method": "same two real outputs reused; frozen tools; not independent model samples",
    }


def online():
    options = ConstraintPlanningOptions(
        country_code="CN",
        time_zone="Asia/Shanghai",
        currency="CNY",
        active_evidence=ActiveEvidenceOptions(
            max_queries=1, budget_units=2, time_limit_seconds=15, allow_source_search=True
        ),
    )
    req = TripRequirements(
        constraint_planning=options,
        day_windows=[DayWindow(day_no=1, kind="arrival", not_before="14:00", finish_by="16:00")],
        fixed_arrangements=[
            FixedArrangement(
                day_no=1, name="中国丝绸博物馆", item_type="attraction", start_time="14:00", end_time="15:30"
            )
        ],
    )
    request = GenerateRequest(city="杭州", days=1, persons=1, start_date="2026-11-02", requirements_struct=req)
    pool = [
        {
            "name": "中国丝绸博物馆",
            "id": "official-name:chinasilkmuseum.com:hangzhou",
            "source": "manual-official-name",
            "country_code": "CN",
            "duration_min": 90,
        }
    ]
    prepared = prepare_candidates({"candidates": pool}, options)
    _, problems = _problem_snapshots(request, prepared, date(2026, 11, 2))
    observations = []

    def observe(*args):
        observation = query_evidence(*args)
        observations.append(asdict(observation))
        return observation

    results, report = drive_evidence(
        problems,
        prepared,
        request,
        options=options.active_evidence,
        solve=lambda current, seconds: solve_trip(current, time_limit_s=seconds),
        observe=observe,
    )
    return {
        "scenario": "Hangzhou museum, synthetic requested visit, real opening search",
        "decision": decision_signature(results),
        "results": [asdict(r) for r in results],
        "report": report.model_dump(mode="json"),
        "facts": [p.fact.model_dump(mode="json") for p in prepared],
        "observations": observations,
    }
