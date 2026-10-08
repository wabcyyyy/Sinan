"""确定性硬约束检查器（M3，spec §8.2）：结构化需求 → ConstraintReport。

职责：首期硬检查——指定日必去、排除点/受支持排除类别、明确到离时间窗口、
锁定条目、strict（hard_cap）预算上限、时间冲突。逐条产出 ConstraintCheck
（status 四态 + blocking），替代交付门的中文子串判据。

语义纪律：
- "pass" 只表示当前输入/估算下符合排程，不代表实地事实已核实（规则报告与
  事实核验分开）；
- 未支持的自然语言/词表外类别/缺坐标致窗口不可验证 → unknown，不算 pass；
- blocking 按需求是否明确与未知是否影响可执行性定义：明确需求的违例阻断
  交付；unknown 不阻断但必须显式透出待确认；
- 单天视角只查"指定当日必去"；全程必去是整趟检查（单天 not_applicable）。

依赖：app.schemas；纯函数，无 IO/LLM。
"""

from __future__ import annotations

import itertools

from app.agent.generation.rules.day_policy import DayPolicy
from app.agent.generation.rules.transfer_time import item_end, item_start
from app.schemas.constraints import ConstraintCheck, ConstraintReport
from app.schemas.trip_requirements import SUPPORTED_EXCLUDED_CATEGORIES, TripRequirements


def _norm(name: str) -> str:
    return "".join(str(name).split()).casefold()


def _plan_items(plan: dict | None) -> list[dict]:
    return [item for item in ((plan or {}).get("items") or []) if isinstance(item, dict)]


def _item_names(items: list[dict]) -> list[str]:
    return [str(item.get("poi_name") or item.get("name") or "") for item in items]


def _check_required(requirements: TripRequirements | None, day_no: int, items: list[dict]) -> list[ConstraintCheck]:
    checks: list[ConstraintCheck] = []
    for place in requirements.required_places if requirements else []:
        if place.day_no != day_no:
            # 全程必去（day_no=None）与别日指定：单天视角不适用（整趟检查负责）
            checks.append(
                ConstraintCheck(
                    constraint_id=f"required:{place.constraint_id}",
                    kind="required_place",
                    status="not_applicable",
                    day_no=day_no,
                    reason="全程必去由整趟验收，本日不判" if place.day_no is None else f"指定日为第 {place.day_no} 天",
                    blocking=True,
                )
            )
            continue
        matched = [raw for raw in _item_names(items) if _norm(raw) == _norm(place.name)]
        checks.append(
            ConstraintCheck(
                constraint_id=f"required:{place.constraint_id}",
                kind="required_place",
                status="pass" if matched else "violation",
                day_no=day_no,
                item_refs=matched,
                reason="" if matched else f"指定日必去「{place.name}」未出现在当日行程",
                blocking=True,
            )
        )
    return checks


def _check_excluded(requirements: TripRequirements | None, day_no: int, items: list[dict]) -> list[ConstraintCheck]:
    checks: list[ConstraintCheck] = []
    if requirements is None:
        return checks
    names = _item_names(items)
    for excluded in requirements.excluded_places:
        hit = [name for name in names if _norm(name) == _norm(excluded)]
        checks.append(
            ConstraintCheck(
                constraint_id=f"excluded-place:{_norm(excluded)}",
                kind="excluded_place",
                status="violation" if hit else "pass",
                day_no=day_no,
                item_refs=hit,
                reason=f"用户明确不去「{excluded}」" if hit else "",
                blocking=True,
            )
        )
    for category in requirements.excluded_categories:
        if category not in SUPPORTED_EXCLUDED_CATEGORIES:
            # 词表外类别不可判 → unknown（不得算 pass），提示待确认
            checks.append(
                ConstraintCheck(
                    constraint_id=f"excluded-category:{_norm(category)}",
                    kind="excluded_category",
                    status="unknown",
                    day_no=day_no,
                    reason=f"暂不支持排除类别「{category}」，需人工确认",
                    blocking=False,
                )
            )
            continue
        # 类别判据受限于条目 item_type 语义（museum 类别 ≠ item_type 枚举）：
        # 按名称包含类别展示名做保守判定，命中才违例，未命中不冒充 pass 依据充分
        label_hit = [name for name in names if any(token and token in name for token in _category_tokens(category))]
        checks.append(
            ConstraintCheck(
                constraint_id=f"excluded-category:{_norm(category)}",
                kind="excluded_category",
                status="violation" if label_hit else "pass",
                day_no=day_no,
                item_refs=label_hit,
                reason=f"用户明确不去类别「{category}」" if label_hit else "",
                evidence_refs=["name-substring"],
                blocking=True,
            )
        )
    return checks


def _category_tokens(category: str) -> tuple[str, ...]:
    """类别的保守名称判据（受控词表 → 展示名同源）。"""
    from app.schemas.trip_requirements import EXCLUDED_CATEGORY_LABELS

    label = EXCLUDED_CATEGORY_LABELS.get(category, category)
    return (label,)


def _check_window(plan: dict | None, policy: DayPolicy) -> ConstraintCheck:
    items = _plan_items(plan)
    if not policy.has_known_window:
        return ConstraintCheck(
            constraint_id=f"window:day:{policy.day_no}",
            kind="day_window",
            status="not_applicable" if not policy.is_transit_day else "unknown",
            day_no=policy.day_no,
            reason="用户未给出明确时间窗口" if policy.is_transit_day else "普通全天无窗口约束",
            evidence_refs=["estimated-city-boundary"] if policy.is_transit_day else [],
            blocking=False,
        )
    start = policy.window_start_min
    end = policy.window_end_min
    if start is None or end is None:
        return ConstraintCheck(
            constraint_id=f"window:day:{policy.day_no}",
            kind="day_window",
            status="unknown",
            day_no=policy.day_no,
            reason="窗口只有单边，无法完整验收",
            blocking=False,
        )
    out_of_window = []
    for item in items:
        begin = item_start(item)
        finish = item_end(item)
        # 条目缺时间（都为 0 哨兵）时不猜：单列 unknown
        if begin == 0 and finish == 0:
            continue
        if begin < start or (finish > end and finish != 24 * 60):
            out_of_window.append(str(item.get("poi_name") or item.get("name") or ""))
    return ConstraintCheck(
        constraint_id=f"window:day:{policy.day_no}",
        kind="day_window",
        status="violation" if out_of_window else "pass",
        day_no=policy.day_no,
        item_refs=out_of_window,
        reason=f"{len(out_of_window)} 条安排越出 {start // 60:02d}:{start % 60:02d}-{end // 60:02d}:{end % 60:02d} 窗口"
        if out_of_window
        else "",
        evidence_refs=["user-window"],
        blocking=True,
    )


def _check_locked(plan: dict | None, locked_names: set[str] | None, day_no: int) -> ConstraintCheck | None:
    if not locked_names:
        return None
    names = {_norm(name) for name in _item_names(_plan_items(plan))}
    missing = sorted(name for name in locked_names if _norm(name) not in names)
    return ConstraintCheck(
        constraint_id=f"locked:day:{day_no}",
        kind="locked_item",
        status="violation" if missing else "pass",
        day_no=day_no,
        item_refs=missing,
        reason=f"锁定条目缺失：{'、'.join(missing)}" if missing else "",
        blocking=True,
    )


def _check_time_conflicts(plan: dict | None, day_no: int) -> ConstraintCheck:
    items = sorted(_plan_items(plan), key=item_start)
    conflicts: list[str] = []
    for prev, nxt in itertools.pairwise(items):
        prev_end, next_start = item_end(prev), item_start(nxt)
        if prev_end == 0 or next_start == 0:
            continue
        if prev_end > next_start:
            conflicts.append(f"{prev.get('poi_name')}→{nxt.get('poi_name')}")
    return ConstraintCheck(
        constraint_id=f"time:day:{day_no}",
        kind="time_conflict",
        status="violation" if conflicts else "pass",
        day_no=day_no,
        item_refs=conflicts,
        reason="；".join(conflicts[:5]),
        blocking=True,
    )


def check_constraints(
    plan: dict | None,
    requirements: TripRequirements | None,
    day_no: int,
    policy: DayPolicy,
    *,
    locked_names: set[str] | None = None,
    strict_budget: float | None = None,
) -> ConstraintReport:
    """单日硬约束检查（spec §8.2 首期集合）。

    strict_budget：hard_cap 预算上限（当日口径，无容差）；None = 无 strict 检查。
    """
    items = _plan_items(plan)
    checks: list[ConstraintCheck] = [
        *_check_required(requirements, day_no, items),
        *_check_excluded(requirements, day_no, items),
        _check_window(plan, policy),
        _check_time_conflicts(plan, day_no),
    ]
    locked = _check_locked(plan, locked_names, day_no)
    if locked is not None:
        checks.append(locked)
    if strict_budget is not None:
        spent = sum(float(item.get("cost") or 0) for item in items)
        checks.append(
            ConstraintCheck(
                constraint_id=f"budget:strict:day:{day_no}",
                kind="budget_strict",
                status="violation" if spent > strict_budget else "pass",
                day_no=day_no,
                reason=f"hard_cap 预算 {strict_budget:g}，当日估算 {spent:.0f}（无容差）"
                if spent > strict_budget
                else "",
                evidence_refs=["estimate"],
                blocking=True,
            )
        )
    return ConstraintReport(checks=checks)
