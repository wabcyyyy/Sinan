"""P2：依赖邻域 → 当天 → 跨天；可行后比较扰动与路线，不调用 LLM。"""

import copy
import hashlib
import json
import time
from datetime import timedelta
from decimal import Decimal

from app.agent.generation.content.constraint_replan_check import schedule_metrics, validate_replan_schedule
from app.agent.generation.content.constraint_replan_model import ReplanModel, describe_operation
from app.agent.generation.content.constraint_trip_solver import solve_trip
from app.agent.generation.orchestration import evidence_tools
from app.agent.generation.orchestration.active_evidence import drive_evidence
from app.agent.generation.orchestration.constraint_planning import _assessment, _numerical_status
from app.agent.generation.orchestration.replan_snapshot import create_snapshot, neighborhoods
from app.agent.runtime.trace import record_event
from app.schemas.replanning import ReplanAttempt, ReplanCompareVO, ReplanOption


def _clock(value):
    return f"{value // 60:02d}:{value % 60:02d}"


def _proposal(request, before, results, prepared, matrix, start, requirements, completed):
    by_id = {p["id"]: p for day in before for p in day["items"]}
    facts = {p.fact.candidate_id: p.item for p in prepared}
    plans = []
    for day, result in zip(before, results, strict=True):
        plan = copy.deepcopy(day)
        plan["items"] = []
        for visit in result.visits:
            item = copy.deepcopy(by_id[int(visit.candidate_id.split(":")[1])])
            observed = facts[visit.candidate_id]
            evidence = {k: v.model_dump(mode="json") for k, v in observed.fact_evidence.items()}
            item.update(
                cost=observed.cost,
                cost_currency=observed.cost_currency,
                open_time=observed.open_time,
                fact_evidence=evidence,
                fact_evidence_json=json.dumps(evidence, ensure_ascii=False),
            )
            if item.get("duration_min") != visit.end_min - visit.start_min:
                evidence = dict(item.get("fact_evidence") or {})
                if observed.duration_min != visit.end_min - visit.start_min:
                    evidence.pop("duration_min", None)
                item["fact_evidence"] = evidence
                item["fact_evidence_json"] = json.dumps(evidence, ensure_ascii=False)
            item.update(
                start_time=_clock(visit.start_min),
                end_time=_clock(visit.end_min),
                duration_min=visit.end_min - visit.start_min,
            )
            plan["items"].append(item)
        assessment = _assessment(
            request, day["day_no"], start + timedelta(days=day["day_no"] - 1), result, prepared, matrix, 0
        )
        plan["constraintPlanning"] = assessment.model_dump(mode="json", by_alias=True)
        plan["executedItemIds"] = sorted(set(completed).intersection(p["id"] for p in plan["items"]))
        plan["_replanned"] = True
        plans.append(plan)
    plans[0]["_proposedRequirements"] = requirements.model_dump(mode="json", by_alias=True)
    return plans


def _option(key, title, before, plans, matrix, protected, results, change, removable, fact_updates):
    metrics, changed = schedule_metrics(before, plans, matrix, protected)
    errors = validate_replan_schedule(before, plans, change, protected, matrix, removable, fact_updates=fact_updates)
    if errors:
        raise ValueError("独立排程检查失败：" + "；".join(errors))
    signature = hashlib.sha256(
        json.dumps(
            [(d["day_no"], [(p["id"], p["start_time"], p["end_time"]) for p in d["items"]]) for d in plans]
        ).encode()
    ).hexdigest()
    days = sorted({d["day_no"] for d in before + plans if any(p["id"] in changed for p in d["items"])})
    option = ReplanOption(
        key=key,
        title=title,
        numerical_status=_numerical_status(results[0]),
        fact_status="UNKNOWN" if any(r.status == "UNKNOWN" for r in results) else results[0].status,
        metrics=metrics,
        changed_item_ids=changed,
        affected_day_nos=days,
        reasons=[
            describe_operation(
                change,
                next((p["poi_name"] for d in before for p in d["items"] if p["id"] == change.item_id), ""),
                next((p["poi_name"] for d in before for p in d["items"] if p["id"] == change.anchor_item_id), ""),
            ),
            f"按本轮确认的变化调整；保留 {len(protected)} 个已执行/固定项",
            "事实假设和路线来源见逐日规划说明；最优性仅针对本次候选与求解范围",
            f"本轮有来源的费用更新 {len(fact_updates)} 项，随草稿确认后保存",
        ],
        signature=signature,
    )
    return option


def _vector(option):
    m = option.metrics
    return (
        m.removed_items,
        m.cross_day_moves,
        m.changed_items,
        m.order_changes,
        m.start_shift_minutes,
        m.travel_minutes,
    )


def nondominated(options):
    unique = {o.signature: o for o in reversed(options)}
    return [
        o
        for o in unique.values()
        if not any(
            all(a <= b for a, b in zip(_vector(other), _vector(o), strict=True)) and _vector(other) != _vector(o)
            for other in unique.values()
        )
    ]


def _find_scope(problems, previous, change, scopes, deadline, budget, optional_ids, *, use_hints=True):
    attempts, conflicts = [], []
    for index, (scope, movable) in enumerate(scopes):
        started = time.monotonic()
        remaining = deadline - started
        if remaining <= 0:
            # A proved local conflict is not a proof for the unsearched wider scope.
            attempts.append(
                ReplanAttempt(
                    scope=scope,
                    status="UNKNOWN",
                    movable_item_ids=sorted(movable),
                    reason="总预算耗尽，本范围未启动；不能据局部无解推断整趟无解",
                    seconds=0,
                )
            )
            return attempts, None, ["仍有更大范围未完成搜索，当前为未决而非整趟无解"]
        repair = ReplanModel(
            previous, change, movable, cross_day=scope == "trip", removable=set(optional_ids), use_hints=use_hints
        )
        results = solve_trip(
            problems,
            budget=budget,
            time_limit_s=max(0.001, min(2.5, remaining / (len(scopes) - index))),
            repair=repair,
            shrink_conflicts=index == len(scopes) - 1,
        )
        numerical = _numerical_status(results[0])
        attempts.append(
            ReplanAttempt(
                scope=scope,
                status=numerical,
                movable_item_ids=sorted(movable),
                reason=(
                    "先冻结邻域外活动"
                    if not attempts
                    else "前一范围已证明不可行，扩大可移动范围"
                    if attempts[-1].status == "INFEASIBLE"
                    else "前一范围时限内未决，扩大可移动范围寻找可行方案"
                ),
                seconds=round(time.monotonic() - started, 6),
            )
        )
        if numerical in {"OPTIMAL", "FEASIBLE"}:
            return attempts, (scope, movable, results), []
        conflicts = [check.reason for check in results[0].report.checks if check.status == "violation"]
        if numerical not in {"INFEASIBLE", "UNKNOWN"}:
            break
    return attempts, None, conflicts


def _alternates(problems, previous, change, chosen, first, deadline, budget, optional_ids):
    scope, movable, _ = chosen
    bounds = {
        "removed": first.metrics.removed_items,
        "cross_day": first.metrics.cross_day_moves,
        "changed": first.metrics.changed_items + 2,
        "shift": first.metrics.start_shift_minutes + 120,
    }
    for strategy, title in (("route", "在改动界限内减少交通"), ("time", "尽量贴近原开始时间")):
        if time.monotonic() >= deadline:
            break
        repair = ReplanModel(
            previous,
            change,
            movable,
            strategy=strategy,
            cross_day=scope == "trip",
            bounds=bounds,
            removable=set(optional_ids),
        )
        results = solve_trip(
            problems, budget=budget, time_limit_s=max(0.001, min(2.5, deadline - time.monotonic())), repair=repair
        )
        if _numerical_status(results[0]) in {"OPTIMAL", "FEASIBLE"}:
            yield strategy, title, results


def _collect_options(request, plans, change, completed, optional_ids, snapshot, chosen, deadline, budget):
    problems, prepared, previous, protected, req, matrix, start = snapshot
    _, movable, results = chosen
    options, proposals = [], {}
    old = {p["id"]: p for day in plans for p in day["items"]}
    fact_updates = []
    for candidate in prepared:
        identifier = int(candidate.fact.candidate_id.split(":")[1])
        original, item = old[identifier], candidate.item
        if original.get("cost") != item.cost or original.get("cost_currency") != item.cost_currency:
            fact = item.fact_evidence.get("cost")
            fact_updates.append(
                {
                    "itemId": identifier,
                    "entityId": candidate.fact.source_entity_id,
                    "oldAmount": original.get("cost"),
                    "oldCurrency": original.get("cost_currency"),
                    "amount": item.cost,
                    "currency": item.cost_currency,
                    "evidence": fact.model_dump(mode="json", by_alias=True) if fact else {},
                }
            )
    proposal = _proposal(request, plans, results, prepared, matrix, start, req, completed)
    first = _option(
        "minimal", "优先减少改动", plans, proposal, matrix, protected, results, change, optional_ids, fact_updates
    )
    options.append(first)
    proposals[first.key] = proposal
    for key, title, alternate in _alternates(problems, previous, change, chosen, first, deadline, budget, optional_ids):
        proposed = _proposal(request, plans, alternate, prepared, matrix, start, req, completed)
        option = _option(key, title, plans, proposed, matrix, protected, alternate, change, optional_ids, fact_updates)
        options.append(option)
        proposals[key] = proposed
    options = nondominated(options)
    for option in options:
        proposals[option.key][0]["_replanSummary"] = option.model_dump(mode="json", by_alias=True)
        frozen = protected | {v.item_id for v in previous if v.item_id not in movable}
        proposals[option.key][0]["_replanProof"] = {
            "before": plans,
            "operation": change.model_dump(mode="json"),
            "protected": sorted(frozen),
            "matrix": [{"from": a, "to": b, "route": r} for (a, b), r in matrix.items()],
            "completedItemIds": completed,
            "optionalItemIds": list(optional_ids),
            "metrics": option.metrics.model_dump(mode="json", by_alias=True),
            "factUpdates": fact_updates,
        }
    return options, {key: value for key, value in proposals.items() if key in {o.key for o in options}}


def run_constraint_replan(
    request,
    plans,
    change,
    completed,
    saved_routes=(),
    *,
    allow_cross_day=True,
    optional_item_ids=(),
    time_limit_s: float = 8,
):
    if not 0 < time_limit_s <= 30:
        raise ValueError("重规划求解预算须在 0–30 秒内")
    deadline = time.monotonic() + time_limit_s
    snapshot = create_snapshot(request, plans, change, completed, saved_routes)
    problems, prepared, previous, protected, req, matrix, start = snapshot
    scopes, dependencies = neighborhoods(plans, change, req, allow_cross_day)
    budget = Decimal(str(request.budget)) if request.budget is not None else None
    evidence_report = None
    evidence_options = req.constraint_planning.active_evidence
    if evidence_options:
        # 双端试解保留用户操作、固定/已执行项及可删除集合，不能用放松后的模型挑瓶颈。
        evidence_options = evidence_options.model_copy(
            update={
                "time_limit_seconds": min(
                    evidence_options.time_limit_seconds, max(0.001, (deadline - time.monotonic()) / 2)
                )
            }
        )

        def resolve(current, seconds):
            repair = ReplanModel(
                previous,
                change,
                {v.item_id for v in previous},
                cross_day=allow_cross_day,
                removable=set(optional_item_ids),
            )
            return solve_trip(current, budget=budget, time_limit_s=seconds, repair=repair)

        _, evidence_report = drive_evidence(
            problems,
            prepared,
            request,
            options=evidence_options,
            solve=resolve,
            observe=evidence_tools.query_evidence,
            budget=budget,
        )
        matrix = problems[0].route_matrix
        snapshot = problems, prepared, previous, protected, req, matrix, start
    attempts, chosen, conflicts = _find_scope(problems, previous, change, scopes, deadline, budget, optional_item_ids)
    options, proposals = [], {}
    if chosen:
        options, proposals = _collect_options(
            request, plans, change, completed, optional_item_ids, snapshot, chosen, deadline, budget
        )
        for proposal in proposals.values():
            for day in proposal:
                day["constraintPlanning"]["activeEvidence"] = (
                    evidence_report.model_dump(mode="json", by_alias=True) if evidence_report else None
                )
    record_event("node", "constraint_replan", metadata={"attempts": len(attempts), "options": len(options)})
    result = ReplanCompareVO(
        planning_revision=0,
        options=options,
        attempts=attempts,
        conflicts=conflicts,
        dependencies=dependencies,
        active_evidence=evidence_report,
        assumptions=[
            "仅重排现有 1–7 日、最多20条安排；不自动增加新地点",
            "候选集内非支配；不代表完整 Pareto 前沿；坐标估算/未知事实仍为条件结论",
            "依赖驱动的局部重求解，未保留旧搜索树；确认前不改正式行程",
        ],
    )
    return result, proposals
