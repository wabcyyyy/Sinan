"""共享模型一次求解整趟，不用逐日贪心冒充全程约束求解。"""

import time

from ortools.sat.python import cp_model

from app.agent.generation.content.constraint_compile import CompiledDay
from app.agent.generation.content.constraint_problem import DayPlanningProblem, DayPlanningResult
from app.agent.generation.content.constraint_replan_model import replan_objectives
from app.agent.generation.content.constraint_solver import _conflict, _facts, _snapshot, _solver
from app.agent.generation.content.constraint_trip import compile_trip, trip_objectives
from app.schemas.constraints import ConstraintReport


def _capture(days, solver, results):
    for day, result in zip(days, results, strict=True):
        result.visits, result.objective = _snapshot(day.problem, day, solver)
        result.selected_routes = []
        for (i, j, k), variable in day.route_choices.items():
            if solver.value(variable):
                first, second = day.problem.candidates[i], day.problem.candidates[j]
                option = day.problem.route_matrix[first.candidate_id, second.candidate_id]["options"][k]
                result.selected_routes.append({"from_id": first.candidate_id, "to_id": second.candidate_id, **option})


def _optimize(days, solver, results, deadline, repair=None):
    model = days[0].model
    objectives = trip_objectives(days)
    if repair is not None:
        objectives = replan_objectives(days, repair, objectives)
    completed = 0
    _capture(days, solver, results)
    for tier, term in enumerate(objectives):
        if time.monotonic() >= deadline:
            break
        model.maximize(term)
        solver = _solver(deadline - time.monotonic())
        tier_started = time.perf_counter()
        status = solver.solve(model)
        results[0].timings.append(
            {
                "stage": "objective",
                "tier": tier + 1,
                "seconds": time.perf_counter() - tier_started,
                "status": solver.status_name(status),
            }
        )
        if status in (cp_model.MODEL_INVALID, cp_model.INFEASIBLE):
            raise ValueError("固定已证明目标后模型失效，不能伪装成正常超时")
        for result in results:
            result.solver_status = solver.status_name(status)
            result.objective_bound = (
                solver.best_objective_bound if status in (cp_model.FEASIBLE, cp_model.OPTIMAL) else None
            )
        if status in (cp_model.FEASIBLE, cp_model.OPTIMAL):
            _capture(days, solver, results)
        if status != cp_model.OPTIMAL:
            break
        completed += 1
        model.add(term == int(solver.value(term)))
    for day, result in zip(days, results, strict=True):
        result.status = "OPTIMAL" if completed == len(objectives) else "FEASIBLE"
        result.optimized_tiers, result.total_tiers = completed, len(objectives)
        result.reason = "同城整趟固定假设下可行；实地事实仍待核实"
        _facts(day.problem, day, result)
    # 全程任一天存在关键事实缺口时，不能将其他天的局部结果当作全程已确认。
    if any(result.status == "UNKNOWN" for result in results):
        for result in results:
            result.status = "UNKNOWN"
            result.reason = "整趟仍有关键事实待核实；当前仅为条件试排"


def solve_trip(
    problems: list[DayPlanningProblem],
    *,
    budget=None,
    time_limit_s: float = 5,
    frozen_days=None,
    repair=None,
    shrink_conflicts: bool = True,
) -> list[DayPlanningResult]:
    if not 0 < time_limit_s <= 60:
        raise ValueError("整趟求解时限须在 0–60 秒内")
    started = time.monotonic()
    measured_started = time.perf_counter()
    deadline = started + time_limit_s
    days = compile_trip(problems, budget, frozen_days, repair)
    timings = [{"stage": "compile", "seconds": time.perf_counter() - measured_started}]
    model = days[0].model
    error = model.validate()
    if error:
        raise ValueError(f"CP-SAT 模型无效：{error}")
    solver = _solver(deadline - time.monotonic())
    feasibility_started = time.perf_counter()
    status = solver.solve(model)
    timings.append(
        {
            "stage": "feasibility",
            "seconds": time.perf_counter() - feasibility_started,
            "status": solver.status_name(status),
        }
    )
    if status == cp_model.MODEL_INVALID:
        raise ValueError("CP-SAT 拒绝模型")
    results = [
        DayPlanningResult(
            status="UNKNOWN",
            solver_status=solver.status_name(status),
            reason="时限内没有可行解或无解证明",
            timings=timings,
        )
        for _ in days
    ]
    if status == cp_model.INFEASIBLE:
        aggregate = CompiledDay(problems[0], model=model)
        aggregate.assumptions = {key: value for day in days for key, value in day.assumptions.items()}
        checks, irreducible = _conflict(aggregate, solver, deadline, shrink_conflicts, timings)
        checks = [
            check.model_copy(
                update={
                    "constraint_id": f"day:{check.day_no}:{check.constraint_id}",
                    "status": "violation",
                    "reason": f"联合充分冲突集合成员：{check.reason}",
                }
            )
            for check in checks
        ]
        for result in results:
            result.status = "INFEASIBLE"
            result.conflict_ids = [check.constraint_id for check in checks]
            result.conflict_irreducible = irreducible
            result.report = ConstraintReport(checks=checks[:200])
            result.reason = "当前候选池及事实假设下整趟不可行；不代表整座城市无解"
    elif status in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        _optimize(days, solver, results, deadline, repair)
    for result in results:
        result.elapsed_s = round(time.perf_counter() - measured_started, 6)
    return results
