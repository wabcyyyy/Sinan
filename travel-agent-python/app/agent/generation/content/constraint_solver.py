"""有总时限的单日 CP-SAT；硬约束可行后逐级优化，冲突只作候选池内解释。"""

from __future__ import annotations

import logging
import time
from itertools import pairwise

from ortools.sat.python import cp_model

from app.agent.generation.content.constraint_compile import CompiledDay, compile_day
from app.agent.generation.content.constraint_problem import DayPlanningProblem, DayPlanningResult, ScheduledVisit
from app.schemas.constraints import ConstraintCheck, ConstraintReport

logger = logging.getLogger(__name__)


def _solver(remaining: float) -> cp_model.CpSolver:
    solver = cp_model.CpSolver()
    # assumptions 与并行搜索不兼容；与锁定版本的官方文档一致。
    solver.parameters.num_search_workers = 1
    solver.parameters.max_time_in_seconds = max(0, remaining)
    solver.parameters.random_seed = 0
    return solver


def _conflict(compiled: CompiledDay, solver: cp_model.CpSolver, deadline: float, shrink: bool, timings=None):
    core = list(solver.sufficient_assumptions_for_infeasibility())
    if any(index not in compiled.assumptions for index in core):
        # 9.14 的跨日重排复现了 presolve 返回普通活动变量而非 assumption 的情况。
        # 原模型已证无解，全部原 assumptions 仍是充分集合；重新删减，不能误译变量为用户约束。
        logger.warning("CP-SAT conflict core contains non-assumption literals: %s; checking full assumption set", core)
        core = list(compiled.assumptions)
    irreducible = False
    if shrink:
        # 禁用其余 assumptions：仅验证该充分集合本身；不得把“没检验”叫最小。
        for literal in list(core):
            if time.monotonic() >= deadline:
                break
            trial = [index for index in core if index != literal]
            compiled.model.clear_assumptions()
            compiled.model.add_assumptions([compiled.model.get_bool_var_from_proto_index(i) for i in trial])
            check = _solver(deadline - time.monotonic())
            # 避免同一 presolve 映射问题影响冲突集合的逐条删除证明。
            check.parameters.cp_model_presolve = False
            started = time.perf_counter()
            status = check.solve(compiled.model)
            if timings is not None:
                timings.append(
                    {
                        "stage": "conflict_shrink",
                        "seconds": time.perf_counter() - started,
                        "status": check.status_name(status),
                    }
                )
            if status == cp_model.INFEASIBLE:
                core = trial
            elif status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
                break
        else:
            irreducible = True
    return [compiled.assumptions[index] for index in core], irreducible


def _snapshot(problem: DayPlanningProblem, compiled: CompiledDay, solver: cp_model.CpSolver):
    visits = [
        ScheduledVisit(
            c.candidate_id, solver.value(compiled.starts[i]), solver.value(compiled.starts[i]) + c.duration_min
        )
        for i, c in enumerate(problem.candidates)
        if solver.value(compiled.selected[i])
    ]
    visits.sort(key=lambda visit: visit.start_min)
    return visits, [int(solver.value(term)) for term in compiled.objectives]


def _facts(problem: DayPlanningProblem, compiled: CompiledDay, result: DayPlanningResult) -> None:
    chosen = {visit.candidate_id for visit in result.visits}
    indexes = {c.candidate_id: i for i, c in enumerate(problem.candidates)}
    gaps = list(compiled.gaps)

    def gap(key: str, reason: str, refs: list[str]) -> None:
        gaps.append(
            ConstraintCheck(
                constraint_id=key,
                kind="planning_evidence",
                status="unknown",
                day_no=problem.day_no,
                blocking=True,
                reason=reason,
                item_refs=refs,
            )
        )

    costs = []
    for candidate in problem.candidates:
        if candidate.candidate_id not in chosen:
            continue
        price = compiled.prices[indexes[candidate.candidate_id]]
        if price is None:
            gap(f"price:{candidate.candidate_id}", "票价未知或币种不一致；仅作零下界试解，不是免费", [candidate.name])
        else:
            costs.append(price)
        if candidate.opening_windows is None:
            gap(f"opening:{candidate.candidate_id}", "营业窗未知；当前安排待取证", [candidate.name])
    used_route_ids = set()
    for first, second in pairwise(result.visits):
        used_route_ids.add(f"route:{first.candidate_id}:{second.candidate_id}")
        route = problem.route_matrix.get((first.candidate_id, second.candidate_id))
        if route is None:
            gap(
                f"route:{first.candidate_id}:{second.candidate_id}",
                "有向路段未知；零分钟仅为乐观下界",
                [first.candidate_id, second.candidate_id],
            )
        else:
            selected_route = next(
                (
                    r
                    for r in result.selected_routes
                    if r["from_id"] == first.candidate_id and r["to_id"] == second.candidate_id
                ),
                route,
            )
            result.travel_min += selected_route["duration_min"]
    route_costs = sum(row["cost_minor"] for row in result.selected_routes)
    if problem.requirements.fact_source == "chinatravel" and not result.selected_routes:
        route_costs = sum(
            problem.route_matrix.get((a.candidate_id, b.candidate_id), {}).get("cost_minor", 0)
            for a, b in pairwise(result.visits)
        )
    result.known_cost_minor = sum(costs) + route_costs
    result.total_cost_minor = sum(costs) + route_costs if len(costs) == len(result.visits) else None
    checks = []
    for check in compiled.assumptions.values():
        if check.constraint_id.startswith("route:") and check.constraint_id not in used_route_ids:
            continue
        if check.constraint_id.startswith("opening:") and not any(
            ref in [c.name for c in problem.candidates if c.candidate_id in chosen] for ref in check.item_refs
        ):
            continue
        checks.append(check.model_copy(update={"status": "pass"}))
    # 同一个缺口可能由多个关系引用；按 ID 合并，不重复占报告容量。
    result.report = ConstraintReport(checks=list({check.constraint_id: check for check in [*checks, *gaps]}.values()))
    if gaps:
        result.status = "UNKNOWN"
        result.reason = "仅有乐观试解：仍有事实、实体或未编译需求缺口；不能宣称可交付"


def solve_day(
    problem: DayPlanningProblem, *, time_limit_s: float = 3, shrink_conflict: bool = True
) -> DayPlanningResult:
    """总预算包含编译、首次可行解、逐级优化和删除检验；不调用外部服务。

    status=UNKNOWN 可带待查证试排；solver_status 单独保留原生数值求解状态。
    OPTIMAL 仅指当前快照及候选池内全部目标层完成证明，不代表现实事实已核实。
    """
    if not 0 <= time_limit_s <= 60:
        raise ValueError("求解总时限须在 0–60 秒内")
    started = time.monotonic()
    measured_started = time.perf_counter()
    deadline = started + time_limit_s
    if time_limit_s == 0:
        return DayPlanningResult(status="UNKNOWN", solver_status="UNKNOWN", reason="求解预算为零，未开始搜索")
    compiled = compile_day(problem)
    error = compiled.model.validate()
    if error:
        raise ValueError(f"CP-SAT 模型无效：{error}")
    solver = _solver(deadline - time.monotonic())
    status = solver.solve(compiled.model)
    result = DayPlanningResult(
        status="UNKNOWN", solver_status=solver.status_name(status), total_tiers=len(compiled.objectives)
    )
    if status == cp_model.MODEL_INVALID:
        raise ValueError("CP-SAT 拒绝模型，不能当作需求无解")
    if status == cp_model.INFEASIBLE:
        checks, result.conflict_irreducible = _conflict(compiled, solver, deadline, shrink_conflict)
        result.status = "INFEASIBLE"
        result.conflict_ids = [check.constraint_id for check in checks]
        result.report = ConstraintReport(
            checks=[
                check.model_copy(
                    update={"status": "violation", "reason": f"充分冲突集合成员（并非单条独立违例）：{check.reason}"}
                )
                for check in checks[:200]
            ]
        )
        result.reason = "仅在当前候选池和固定事实假设下不可行；冲突成员须联合解释，不代表整座城市无解"
    elif status in (cp_model.FEASIBLE, cp_model.OPTIMAL):
        result.visits, result.objective = _snapshot(problem, compiled, solver)
        result.status = "FEASIBLE"
        for term in compiled.objectives:
            if time.monotonic() >= deadline:
                break
            compiled.model.maximize(term)
            solver = _solver(deadline - time.monotonic())
            status = solver.solve(compiled.model)
            result.solver_status = solver.status_name(status)
            result.objective_bound = (
                solver.best_objective_bound if status in (cp_model.FEASIBLE, cp_model.OPTIMAL) else None
            )
            if status in (cp_model.FEASIBLE, cp_model.OPTIMAL):
                result.visits, result.objective = _snapshot(problem, compiled, solver)
            if status != cp_model.OPTIMAL:
                break
            result.optimized_tiers += 1
            compiled.model.add(term == int(solver.value(term)))
        if result.optimized_tiers == result.total_tiers:
            result.status = "OPTIMAL"
        result.reason = "当前固定快照内可行；事实仍为输入假设，非实地核实"
        _facts(problem, compiled, result)
    else:
        result.reason = "时限内未找到可行解或证明不可行，不能解释成需求冲突"
    result.elapsed_s = round(time.perf_counter() - measured_started, 6)
    return result
