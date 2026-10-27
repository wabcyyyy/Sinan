"""求解瓶颈驱动的有界取证循环；同一执行预算、取消及轨迹，不另建 Agent 平台。"""

import time
from contextlib import nullcontext

from app.agent.generation.content.constraint_problem import DayPlanningResult
from app.agent.generation.content.evidence_gaps import (
    collect_gaps,
    decision_signature,
    invalidate_expired,
    with_assumption,
)
from app.agent.generation.content.evidence_observations import apply_observation
from app.agent.generation.orchestration.evidence_policy import (
    certificate_complete,
    choose_decision_gap,
    infeasibility_proof,
)
from app.agent.runtime.run_limits import (
    RunLimitExceeded,
    RunLimits,
    TaskStopped,
    current_limits,
    phase_scope,
    use_task_limits,
)
from app.agent.runtime.trace import record_event
from app.schemas.active_evidence import ActiveEvidenceReport, EvidenceProbe, EvidenceQueryRecord


def drive_evidence(
    problems, prepared, request, *, options, solve, observe, initial=None, budget=None, strategy=None, seed=0
):
    import random

    started = time.monotonic()
    strategy = strategy or options.strategy
    deadline = started + options.time_limit_seconds
    report = ActiveEvidenceReport(
        policy=f"{strategy}_heuristic", budget_units=options.budget_units, stop_reason="query_budget"
    )
    tried = set()
    rng = random.Random(seed)

    def run(snapshot):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise RunLimitExceeded("主动取证子时限已耗尽")
        limits = current_limits()
        if limits:
            limits.check("deadline")
        report.solve_calls += 1
        return solve(snapshot, min(0.25, remaining))

    result = None if invalidate_expired(problems, prepared) else initial
    pending_solve = False
    active_limits = current_limits() or RunLimits(
        deadline_seconds=options.time_limit_seconds,
        max_llm_calls=2 if options.allow_source_search else 0,
        max_retrievals=options.max_queries * 3,
    )
    local_limits = nullcontext() if current_limits() else use_task_limits(active_limits)
    counters = ("retrievals", "llm_calls", "prompt_tokens", "completion_tokens")
    initial_counts = {key: getattr(active_limits, key) for key in counters}
    try:
        with local_limits, phase_scope(options.time_limit_seconds):
            if result is None:
                result = run(problems)
            for _ in range(options.max_queries):
                if (
                    strategy != "all"
                    and result[0].status == "INFEASIBLE"
                    and infeasibility_proof(problems, result, prepared, run, budget)
                ):
                    report.stop_reason = "optimistic_infeasible"
                    break
                if strategy in {"fixed", "decision"} and certificate_complete(problems, result, prepared):
                    report.stop_reason = "certificate_complete"
                    break
                gaps = collect_gaps(problems, result, prepared, excluded=tried)
                if not gaps:
                    report.stop_reason = "no_remaining_gap"
                    break
                affordable = [g for g in gaps if report.spent_units + g.cost_units <= options.budget_units]
                if not affordable:
                    report.stop_reason = "cost_budget"
                    break
                candidates = []
                if strategy == "decision":
                    gap = choose_decision_gap(problems, result, prepared, affordable, run, report, budget)
                    if gap is None:
                        break
                elif strategy == "all":
                    gap = min(affordable, key=lambda g: g.gap_id)
                elif strategy == "bottleneck":
                    for gap in affordable:
                        low = run(with_assumption(problems, gap, False, budget=budget))
                        high = run(with_assumption(problems, gap, True, budget=budget))
                        a, b = decision_signature(low), decision_signature(high)
                        undecided = a[0] == "UNKNOWN" or b[0] == "UNKNOWN"
                        sensitivity = 0 if undecided else 3 if a[0] != b[0] else int(a[1] != b[1])
                        score = sensitivity * gap.importance / gap.cost_units
                        report.probes.append(
                            EvidenceProbe(
                                gap_id=gap.gap_id,
                                kind=gap.kind,
                                targets=list(gap.names),
                                importance=gap.importance,
                                cost_units=gap.cost_units,
                                fact_state=gap.fact_state,
                                low_status=a[0],
                                high_status=b[0],
                                sensitivity=sensitivity,
                                score=score,
                                reason="试解未知，不能判定无价值" if undecided else "本轮单字段双端决策比较",
                            )
                        )
                        if sensitivity:
                            candidates.append((score, gap))
                    if not candidates:
                        report.stop_reason = (
                            "probe_unknown"
                            if any(p.low_status == "UNKNOWN" or p.high_status == "UNKNOWN" for p in report.probes)
                            else "decision_stable"
                        )
                        break
                    gap = max(candidates, key=lambda pair: (pair[0], pair[1].importance, pair[1].gap_id))[1]
                elif strategy == "random":
                    gap = rng.choice(affordable)
                elif strategy == "confidence":
                    # 全部缺口均未确认，置信度臂按已冻结的稳定顺序打破平局。
                    gap = sorted(affordable, key=lambda g: g.gap_id)[0]
                else:
                    # 固定三域顺序：营业/停留 → 费用 → 路线；共享候选池，不借用隐藏真相。
                    order = {"opening": 0, "duration": 1, "price": 2, "route": 3}
                    gap = min(affordable, key=lambda g: (order[g.kind], g.gap_id))
                tried.add(gap.gap_id)
                report.spent_units += gap.cost_units
                before = decision_signature(result)
                try:
                    observation = observe(gap, problems, prepared, request, options)
                    if observation.question:
                        report.questions.append(observation.question)
                        report.queries.append(
                            EvidenceQueryRecord(
                                gap_id=gap.gap_id,
                                tool=observation.tool,
                                outcome="ask_user",
                                cost_units=gap.cost_units,
                                reason=observation.question,
                            )
                        )
                        report.stop_reason = "awaiting_user"
                        break
                    updated, reason = apply_observation(problems, prepared, gap, observation, request)
                    fact = observation.fact
                    query = EvidenceQueryRecord(
                        gap_id=gap.gap_id,
                        tool=observation.tool,
                        outcome="updated" if updated else "rejected",
                        cost_units=gap.cost_units,
                        source_url=fact.source_url if fact else None,
                        retrieved_at=fact.retrieved_at if fact else None,
                        binding_basis=observation.binding_basis,
                        reason=reason,
                    )
                    report.queries.append(query)
                    if updated:
                        pending_solve = True
                        result = run(problems)
                        pending_solve = False
                        query.decision_changed = before != decision_signature(result)
                except (TaskStopped, RunLimitExceeded):
                    raise
                except Exception as exc:
                    if pending_solve:
                        raise  # 求解器实现错误必须暴露，不冒充外部取证失败。
                    report.queries.append(
                        EvidenceQueryRecord(
                            gap_id=gap.gap_id,
                            tool="evidence",
                            outcome="error",
                            cost_units=gap.cost_units,
                            reason=f"取证失败：{type(exc).__name__}",
                        )
                    )
                record_event("decision", "active_evidence.query", metadata=report.queries[-1].model_dump(mode="json"))
    except RunLimitExceeded:
        report.stop_reason = "time_or_run_budget"
    if result is None or pending_solve:
        # 新事实到达后尚未重求解，不能把旧解冒充为新事实下的可行安排。
        result = [
            DayPlanningResult(
                status="UNKNOWN", solver_status="NOT_RUN", reason="取证子预算耗尽，当前事实尚未完成重求解"
            )
            for _ in problems
        ]
    if result[0].status == "INFEASIBLE" and report.stop_reason != "optimistic_infeasible":
        try:
            proven = infeasibility_proof(problems, result, prepared, run, budget)
        except RunLimitExceeded:
            proven = False
        if not proven:
            for day in result:
                day.status = "UNKNOWN"
                day.reason = "当前假设下冲突，未知事实仍可能改变结论；尚未证实无解"
    report.elapsed_seconds = round(time.monotonic() - started, 6)
    report.retrieval_calls = active_limits.retrievals - initial_counts["retrievals"]
    report.llm_calls = active_limits.llm_calls - initial_counts["llm_calls"]
    report.prompt_tokens = active_limits.prompt_tokens - initial_counts["prompt_tokens"]
    report.completion_tokens = active_limits.completion_tokens - initial_counts["completion_tokens"]
    record_event("decision", "active_evidence.summary", metadata=report.model_dump(mode="json"))
    return result, report
