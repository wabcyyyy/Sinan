import time
from dataclasses import replace
from decimal import Decimal
from types import SimpleNamespace
from typing import cast

import pytest
from ortools.sat.python import cp_model

from app.agent.generation.content.constraint_compile import compile_day
from app.agent.generation.content.constraint_problem import DayPlanningProblem, PlanningCandidate
from app.agent.generation.content.constraint_solver import _conflict, solve_day
from app.agent.generation.rules.constraint_checks import check_constraints
from app.agent.generation.rules.day_policy import day_policy_for
from app.schemas.trip_requirements import PlaceRelation, RequiredPlace, TripRequirements, canonical_requirements_payload
from tests.constraint_planner_cases import frozen_problem
from tests.constraint_planner_oracle import exhaustive_day, objective, schedule_violations


@pytest.mark.parametrize("infeasible", [False, True])
def test_small_independent_oracle(infeasible):
    problem = frozen_problem(infeasible=infeasible)
    visits, score, examined = exhaustive_day(problem)
    result = solve_day(problem)
    assert examined == 326
    if infeasible:
        assert visits is None and result.status == "INFEASIBLE"
        assert result.conflict_ids and result.conflict_irreducible
    else:
        assert result.status == "OPTIMAL"
        assert not schedule_violations(problem, result.visits)
        assert tuple(result.objective) == score == objective(problem, result.visits)


@pytest.mark.parametrize("conflict", ["opening", "day_window", "order_cycle", "one_of_excluded"])
def test_independent_temporal_and_choice_conflicts(conflict):
    problem = frozen_problem()
    req = problem.requirements.model_copy(deep=True)
    candidates = list(problem.candidates)
    if conflict == "opening":
        candidates[0] = replace(candidates[0], opening_windows=((660, 720),))
    elif conflict == "day_window":
        req.day_windows[0].finish_by = "10:00"
    elif conflict == "order_cycle":
        req.place_relations.append(
            PlaceRelation(constraint_id="reverse", kind="before", place_names=["地点1", "地点0"], day_no=1)
        )
    else:
        req.excluded_places = ["地点2", "地点3"]
    problem = replace(problem, requirements=req, candidates=tuple(candidates))
    assert exhaustive_day(problem)[0] is None
    result = solve_day(problem)
    assert result.status == "INFEASIBLE" and result.conflict_ids and result.conflict_irreducible


def test_conflict_sources_and_deletion_proof():
    problem = frozen_problem(infeasible=True)
    req = problem.requirements.model_copy(deep=True)
    # 隔离带轮次的必去约束，避免固定预约本身已与零预算构成另一组合法冲突。
    req.fixed_arrangements = []
    req.place_relations = []
    problem = replace(problem, requirements=req)
    result = solve_day(problem)
    compiled = compile_day(problem)
    assert "budget:hard-cap" in result.conflict_ids
    core_indexes = [i for i, check in compiled.assumptions.items() if check.constraint_id in result.conflict_ids]
    assert any(
        "turn:turn-2" in check.evidence_refs or "turn:turn-3" in check.evidence_refs for check in result.report.checks
    )
    # 独立验证 core 的充分性与逐条删除可行；不能只信布尔标签。
    for removed in [None, *core_indexes]:
        compiled.model.clear_assumptions()
        compiled.model.add_assumptions(
            [compiled.model.get_bool_var_from_proto_index(i) for i in core_indexes if i != removed]
        )
        solver = cp_model.CpSolver()
        solver.parameters.num_search_workers = 1
        status = solver.solve(compiled.model)
        assert status == (cp_model.INFEASIBLE if removed is None else cp_model.OPTIMAL)


@pytest.mark.parametrize("seconds", [0, 2])
def test_non_assumption_core_rechecks_full_proven_set(seconds):
    compiled = compile_day(frozen_problem(infeasible=True))
    solver = cp_model.CpSolver()
    solver.parameters.num_search_workers = 1
    assert solver.solve(compiled.model) == cp_model.INFEASIBLE
    # P5 跨日 before 冲突中 9.14 返回 visit 变量；不能把它当成有出处的约束。
    invalid = next(i for i in range(len(compiled.model.proto.variables)) if i not in compiled.assumptions)
    broken_core = SimpleNamespace(sufficient_assumptions_for_infeasibility=lambda: [invalid])
    checks, irreducible = _conflict(compiled, cast(cp_model.CpSolver, broken_core), time.monotonic() + seconds, True)
    indexes = [i for i, check in compiled.assumptions.items() if check in checks]
    assert checks and irreducible == bool(seconds)
    # 独立重解返回集合；时间耗尽只能返回充分集合，不能声称不可约。
    for removed in [None, *indexes] if irreducible else [None]:
        compiled.model.clear_assumptions()
        compiled.model.add_assumptions(
            [compiled.model.get_bool_var_from_proto_index(i) for i in indexes if i != removed]
        )
        check = cp_model.CpSolver()
        check.parameters.num_search_workers = 1
        check.parameters.cp_model_presolve = False
        assert check.solve(compiled.model) == (cp_model.INFEASIBLE if removed is None else cp_model.OPTIMAL)


@pytest.mark.parametrize("missing", ["price", "opening", "route", "currency"])
def test_missing_facts_are_unknown_not_verified_or_free(missing):
    problem = frozen_problem()
    candidates = list(problem.candidates)
    if missing == "price":
        candidates[0] = replace(candidates[0], price_minor=None)
    elif missing == "opening":
        candidates[0] = replace(candidates[0], opening_windows=None)
    elif missing == "currency":
        candidates[0] = replace(candidates[0], currency="EUR")
    problem = replace(
        problem, candidates=tuple(candidates), route_matrix={} if missing == "route" else problem.route_matrix
    )
    result = solve_day(problem)
    assert result.status == "UNKNOWN" and result.solver_status == "OPTIMAL"
    assert result.visits and result.report.unknown_checks
    if missing in ("price", "currency"):
        assert result.total_cost_minor is None


def test_asymmetric_routes_determine_order():
    candidates = tuple(PlanningCandidate(str(i), f"P{i}", 60, 0, "EUR", ((540, 690),)) for i in range(2))
    problem = DayPlanningProblem(
        TripRequirements(
            required_places=[RequiredPlace(constraint_id=str(i), name=f"P{i}", day_no=1) for i in range(2)]
        ),
        candidates,
        {("0", "1"): {"duration_min": 50, "source": "fixture"}, ("1", "0"): {"duration_min": 10, "source": "fixture"}},
        "EUR",
    )
    result = solve_day(problem)
    assert result.status == "OPTIMAL"
    assert [v.candidate_id for v in result.visits] == ["1", "0"] and result.travel_min == 10


def test_hard_appointment_wins_over_high_priority_soft_order():
    problem = frozen_problem()
    req = problem.requirements.model_copy(deep=True)
    req.place_relations.append(
        PlaceRelation(
            constraint_id="opposite",
            kind="before",
            place_names=["地点1", "地点0"],
            day_no=1,
            strength="soft",
            priority=5,
        )
    )
    problem = replace(problem, requirements=req)
    result = solve_day(problem)
    assert result.status == "OPTIMAL" and result.objective[0] == 0
    assert not schedule_violations(problem, result.visits)
    assert tuple(result.objective) == exhaustive_day(problem)[1]


def test_timeout_is_unknown_without_conflict():
    result = solve_day(frozen_problem(), time_limit_s=0)
    assert result.status == result.solver_status == "UNKNOWN"
    assert not result.conflict_ids and not result.visits


def test_exhausted_during_compilation_is_unknown():
    result = solve_day(frozen_problem(), time_limit_s=0.000000001)
    assert result.status == result.solver_status == "UNKNOWN" and not result.conflict_ids


def test_legacy_rule_path_marks_relations_unknown_until_p1():
    requirements = frozen_problem().requirements
    report = check_constraints(None, requirements, 1, day_policy_for(requirements, 1))
    assert {check.constraint_id for check in report.unknown_checks} >= {"relation:order", "relation:choice"}


def test_soft_priorities_precede_utility():
    candidates = tuple(
        PlanningCandidate(str(i), f"P{i}", 60, 0, "EUR", ((540, 1020),), utility=i + 1) for i in range(4)
    )
    req = TripRequirements(
        max_attractions_per_day=1,
        place_relations=[
            PlaceRelation(
                constraint_id="high", kind="one_of", place_names=["P0", "P1"], day_no=1, strength="soft", priority=5
            ),
            PlaceRelation(
                constraint_id="low", kind="one_of", place_names=["P2", "P3"], day_no=1, strength="soft", priority=1
            ),
        ],
    )
    problem = DayPlanningProblem(req, candidates, {}, "EUR")
    result = solve_day(problem)
    assert result.status == "OPTIMAL" and [v.candidate_id for v in result.visits] == ["1"]
    assert tuple(result.objective) == exhaustive_day(problem)[1]


def test_missing_candidate_is_only_pool_infeasible():
    problem = frozen_problem()
    req = problem.requirements.model_copy(deep=True)
    req.required_places.append(RequiredPlace(constraint_id="missing", name="不在候选池", day_no=1))
    result = solve_day(replace(problem, requirements=req))
    assert result.status == "INFEASIBLE" and "required:missing" in result.conflict_ids
    assert "候选池" in result.reason


def test_global_requirement_and_unsupported_fields_not_silently_satisfied():
    problem = frozen_problem()
    req = problem.requirements.model_copy(deep=True)
    req.required_places[0].day_no = None
    req.pace = "relaxed"
    result = solve_day(replace(problem, requirements=req))
    assert result.status == "UNKNOWN"
    assert {check.constraint_id for check in result.report.unknown_checks} >= {"scope:must-1", "unsupported:pace"}


def test_fixed_cost_uses_existing_requirement_amount():
    problem = frozen_problem()
    req = problem.requirements.model_copy(deep=True)
    req.fixed_arrangements[0].cost = Decimal("70")
    assert solve_day(replace(problem, requirements=req)).status == "INFEASIBLE"


def test_schema_relation_fingerprint_and_validation():
    req = frozen_problem().requirements
    reordered = req.model_copy(deep=True)
    reordered.place_relations.reverse()
    assert canonical_requirements_payload(req) == canonical_requirements_payload(reordered)
    assert TripRequirements.model_validate(req.model_dump(by_alias=True)) == req


@pytest.mark.parametrize("field,value", [("duration_min", -1), ("price_minor", -1)])
def test_invalid_fact_rejected(field, value):
    problem = frozen_problem()
    with pytest.raises(ValueError):
        replace(problem, candidates=(replace(problem.candidates[0], **{field: value}), *problem.candidates[1:]))
