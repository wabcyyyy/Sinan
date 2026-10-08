"""受约束的局部重规划。

输入明确给出受影响日期、锁定点位和候选替换池。模块只复制并修改受影响
日期，未受影响日期按值原样返回；硬约束仍由时间窗优化器和路线服务判断，
不会退化成重新生成整份行程。
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any, cast

from app.agent.generation.output.schedule_optimizer import optimize_daily_plan
from app.agent.runtime.run_limits import current_limits
from app.agent.runtime.trace import record_event, traced
from app.agent.tools.registry import registry
from app.common.config import settings
from app.schemas.trip import LocalReplanRequest


def _name(item: dict) -> str:
    return str(item.get("poi_name") or item.get("name") or "").strip()


def _active(item: dict) -> bool:
    return item.get("item_type") in ("attraction", "food")


def _candidate_items(req: LocalReplanRequest, plans: list[dict]) -> dict[str, dict]:
    existing = {
        _name(item): deepcopy(item)
        for plan in plans
        for item in plan.get("items") or []
        if isinstance(item, dict) and _name(item)
    }
    requested = [str(name).strip() for name in req.candidate_names if str(name).strip()]
    candidates = {name: existing[name] for name in requested if name in existing}
    missing = [name for name in requested if name not in candidates]
    if missing:
        result = registry.invoke("search_pois", {"city": req.city, "preferences": []})
        for item in (result.get("attractions") or []) + (result.get("foods") or []):
            name = _name(item)
            if name in missing:
                candidates[name] = deepcopy(item)
    return candidates


def _replace_unlocked(plan: dict, candidate_items: dict[str, dict], locked: set[str]) -> tuple[dict, list[str]]:
    """在原活动槽位替换非锁定项，确保锁定点的位置不会变化。"""
    result = deepcopy(plan)
    items = [deepcopy(item) for item in result.get("items") or []]
    active_indexes = [index for index, item in enumerate(items) if isinstance(item, dict) and _active(item)]
    unlocked_indexes = [index for index in active_indexes if _name(items[index]) not in locked]
    replacements = list(candidate_items.values())
    replaced_from: list[str] = []
    for index, candidate in zip(unlocked_indexes, replacements, strict=False):
        old_name = _name(items[index])
        if old_name == _name(candidate):
            continue
        replaced_from.append(old_name)
        items[index] = deepcopy(candidate)
    result["items"] = items
    return result, replaced_from


@traced("node", "local_replan")
def run_local_replan(req: LocalReplanRequest) -> dict[str, Any]:
    if not req.plans:
        raise ValueError("局部重规划需要提供当前 plans")
    affected = list(dict.fromkeys(req.affected_day_nos))
    if any(day_no < 1 for day_no in affected):
        raise ValueError("受影响日期必须从第 1 天开始")
    # day_no 缺失/非数字会抛 TypeError（API 层只捕 ValueError → 500）；
    # 显式校验为业务错误，返回可读文案。
    by_day: dict[int, dict] = {}
    for plan in req.plans:
        if not isinstance(plan, dict):
            continue
        raw_day = plan.get("day_no")
        try:
            # except 分支承担 None/坏值的真实处理，这里 cast 只是 appease stub 的 ConvertibleToInt
            by_day[int(cast(int, raw_day))] = plan
        except (TypeError, ValueError) as exc:
            raise ValueError(f"plans 中存在缺少或非法的 day_no：{raw_day!r}") from exc
    missing_days = [day_no for day_no in affected if day_no not in by_day]
    if missing_days:
        raise ValueError(f"找不到受影响日期：{missing_days}")

    locked = {str(name).strip() for name in req.locked_names if str(name).strip()}
    # 剩余预算约束（C3.4 透传）：调用方同时投喂 budget 与 spent 时，重排只受剩余额度
    # 约束（已超支则钳到 0）；只给其一或都不给时维持原语义，spent 本身不读库。
    budget_limit = req.budget
    if req.budget is not None and req.spent is not None:
        budget_limit = max(req.budget - req.spent, 0.0)
    candidate_items = _candidate_items(req, req.plans)
    requested_candidates = [
        str(name).strip()
        for name in req.candidate_names
        if str(name).strip() and str(name).strip() not in set(req.locked_names)
    ]
    unresolved = [name for name in requested_candidates if name not in candidate_items]
    if unresolved:
        return {
            "status": "failed",
            "city": req.city,
            "affected_day_nos": affected,
            "plans": deepcopy(req.plans),
            "locked_items": sorted(locked),
            "replaced_items": [],
            "violations": [f"候选点位不存在：{', '.join(unresolved)}"],
            "score": 0.0,
            "route_report": {},
            "budget_limit": budget_limit,
        }

    output = deepcopy(req.plans)
    reports: dict[int, dict] = {}
    replaced_items: list[dict] = []
    violations: list[str] = []
    scores: list[float] = []
    for day_no in affected:
        original = deepcopy(by_day[day_no])
        day_locked = {name for name in locked if any(_name(item) == name for item in original.get("items") or [])}
        working, replaced_from = _replace_unlocked(original, candidate_items, day_locked)
        active = [item for item in working.get("items") or [] if isinstance(item, dict) and _active(item)]
        try:
            matrix = registry.invoke("get_route_matrix", {"items": active, "mode": settings.route_mode})
            optimized = optimize_daily_plan(
                working,
                route_matrix=matrix,
                mode=settings.route_mode,
                budget_limit=budget_limit,
                required_names=day_locked,
                locked_names=day_locked,
            )
        except Exception as exc:
            violations.append(f"第 {day_no} 天局部重规划失败：{exc}")
            reports[day_no] = {"status": "failed", "violations": [str(exc)]}
            continue
        report = optimized.as_dict()
        optimized_names = {_name(item) for item in optimized.plan.get("items") or []}
        missing_locked = sorted(day_locked - optimized_names)
        if missing_locked:
            # 约束失败时保留原锁定计划，不能把“必留项”从响应中静默删除。
            reason = f"锁定点位无法安排但必须保留：{', '.join(missing_locked)}"
            violations.append(f"第 {day_no} 天：{reason}")
            reports[day_no] = {**report, "status": "failed", "violations": [reason]}
            continue
        reports[day_no] = report
        scores.append(optimized.score)
        for old_name in replaced_from:
            new_names = [_name(item) for item in optimized.plan.get("items") or []]
            replacement = next((name for name in new_names if name in candidate_items and name != old_name), None)
            replaced_items.append({"day_no": day_no, "from": old_name, "to": replacement})
        violations.extend([f"第 {day_no} 天：{issue}" for issue in optimized.violations])
        output = [
            optimized.plan if isinstance(plan, dict) and int(plan.get("day_no", -1)) == day_no else plan
            for plan in output
        ]

    failed = bool(violations) or len(reports) != len(affected)
    limits = current_limits()
    if limits:
        limits.record_replan(progressed=not failed and bool(replaced_items))
    record_event(
        "decision",
        "local_replan_result",
        metadata={
            "affected_days": affected,
            "locked_count": len(locked),
            "replacement_count": len(replaced_items),
            "failed": failed,
            "spent": req.spent,
            "budget_limit": budget_limit,
        },
        action_id=req.action_id,
    )
    return {
        "status": "failed"
        if failed
        else ("degraded" if any(report.get("degraded") for report in reports.values()) else "success"),
        "city": req.city,
        "affected_day_nos": affected,
        "plans": output,
        "locked_items": sorted(locked),
        "replaced_items": replaced_items,
        "violations": violations,
        "score": round(sum(scores) / len(scores), 4) if scores else 0.0,
        "route_report": reports,
        "failure_reasons": list(req.failure_reasons),
        "budget_limit": budget_limit,
    }
