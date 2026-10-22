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

from app.agent.generation.rules.category_evidence import category_membership, item_category_evidence
from app.agent.generation.rules.day_policy import DayPolicy, diet_gate_blocking
from app.agent.generation.rules.transfer_time import item_end, item_start
from app.schemas.constraints import ConstraintCheck, ConstraintReport
from app.schemas.trip import FactEvidence
from app.schemas.trip_requirements import EXCLUDED_CATEGORY_TOKENS, SUPPORTED_EXCLUDED_CATEGORIES, TripRequirements


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
    pairs = item_category_evidence(items)
    names = [name for name, _tags in pairs]
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
        # 未知不等于非目标类别；每一项独立检查，同名项不能借另一项的证据放行。
        evidence_used = any(tags for _name, tags in pairs)
        violations: list[str] = []
        for name, tags in pairs:
            membership = category_membership(category, tags)
            name_hit = any(token in name.casefold() for token in EXCLUDED_CATEGORY_TOKENS[category])
            if (membership is True or (membership is None and name_hit)) and name not in violations:
                violations.append(name)
        checks.append(
            ConstraintCheck(
                constraint_id=f"excluded-category:{_norm(category)}",
                kind="excluded_category",
                status="violation" if violations else "pass",
                day_no=day_no,
                item_refs=violations,
                reason=f"用户明确不去类别「{category}」" if violations else "",
                evidence_refs=["osm-category"] if evidence_used else ["name-substring"],
                blocking=True,
            )
        )
    return checks


def _fact_dict(raw: object) -> dict:
    if isinstance(raw, FactEvidence):
        return raw.model_dump()
    return raw if isinstance(raw, dict) else {}


def _check_content(
    requirements: TripRequirements | None, day_no: int, items: list[dict], policy: DayPolicy
) -> list[ConstraintCheck]:
    """数量是硬限制；饮食只有外部字段证据才算有据，候选策略必须由用户明确选择。"""
    checks: list[ConstraintCheck] = []
    if requirements is None:
        return checks
    if requirements.max_attractions_per_day is not None:
        count = sum(item.get("item_type") == "attraction" for item in items)
        checks.append(
            ConstraintCheck(
                constraint_id=f"attractions:day:{day_no}",
                kind="attraction_limit",
                day_no=day_no,
                status="violation" if count > policy.max_attractions else "pass",
                blocking=True,
                reason=f"安排 {count} 个景点，用户上限为 {policy.max_attractions} 个"
                if count > policy.max_attractions
                else "",
            )
        )
    diet = requirements.dietary_requirement
    if not diet:
        return checks
    for index, item in enumerate(items):
        if item.get("item_type") != "food":
            continue
        facts = item.get("fact_evidence") or item.get("factEvidence") or {}
        raw = facts.get(f"diet:{diet}") if isinstance(facts, dict) else None
        raw = _fact_dict(raw)
        identity = _fact_dict(facts.get("identity")) if isinstance(facts, dict) else {}
        valid = (
            isinstance(raw, dict)
            and raw.get("verification_status", raw.get("verificationStatus")) == "verified"
            and raw.get("value_kind", raw.get("valueKind")) == "observed"
            and bool(raw.get("source_url", raw.get("sourceUrl")))
            and raw.get("freshness_status", raw.get("freshnessStatus")) != "stale"
            and identity.get("verification_status", identity.get("verificationStatus")) == "verified"
        )
        name = str(item.get("poi_name") or "餐厅")
        label = "纯素" if diet == "vegan" else "素食"
        checks.append(
            ConstraintCheck(
                constraint_id=f"diet:day:{day_no}:item:{index}",
                kind="dietary_requirement",
                day_no=day_no,
                status="pass" if valid else "unknown",
                item_refs=[name],
                blocking=diet_gate_blocking(requirements),
                reason=f"「{name}」有外部{label}标签，当前菜单仍须出发前核实"
                if valid
                else f"「{name}」尚无{label}可用性依据，仅为待核实餐饮候选，不能确认满足饮食要求",
                evidence_refs=[str(raw.get("source_url", raw.get("sourceUrl")))]
                if valid and isinstance(raw, dict)
                else [],
            )
        )
    return checks


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
    # 单边也是明确要求：另一侧只取自然日边界，不推断用户未给出的到离时间。
    start = policy.window_start_min if policy.window_start_min is not None else 0
    end = policy.window_end_min if policy.window_end_min is not None else 24 * 60
    out_of_window = []
    missing_times = []
    for item in items:
        begin = item_start(item)
        finish = item_end(item)
        # 条目缺时间（都为 0 哨兵）时不猜：单列 unknown
        if not item.get("start_time") or not item.get("end_time"):
            missing_times.append(str(item.get("poi_name") or item.get("name") or ""))
            continue
        if begin < start or finish > end:
            out_of_window.append(str(item.get("poi_name") or item.get("name") or ""))
    return ConstraintCheck(
        constraint_id=f"window:day:{policy.day_no}",
        kind="day_window",
        status="violation" if out_of_window else "unknown" if missing_times else "pass",
        day_no=policy.day_no,
        item_refs=out_of_window,
        reason=f"{len(out_of_window)} 条安排越出 {start // 60:02d}:{start % 60:02d}-{end // 60:02d}:{end % 60:02d} 窗口"
        if out_of_window
        else "部分条目缺少时间，无法验收"
        if missing_times
        else "",
        evidence_refs=["user-window"],
        blocking=True,
    )


def _check_fixed(requirements: TripRequirements | None, day_no: int, items: list[dict]) -> list[ConstraintCheck]:
    checks = []
    for fixed in requirements.fixed_arrangements if requirements else []:
        if fixed.day_no != day_no:
            continue
        matches = [
            p for p in items if p.get("poi_name", p.get("name")) == fixed.name and p.get("item_type") == fixed.item_type
        ]
        valid = len(matches) == 1 and all(
            str(matches[0].get(key) or "")[:5] == getattr(fixed, key) for key in ("start_time", "end_time")
        )
        checks.append(
            ConstraintCheck(
                constraint_id=f"fixed:{day_no}:{fixed.name}",
                kind="locked_item",
                status="pass" if valid else "violation",
                day_no=day_no,
                item_refs=[fixed.name],
                reason=""
                if valid
                else (
                    f"固定安排「{fixed.name}」必须保留在第 {day_no} 天 {fixed.start_time}–{fixed.end_time}；"
                    "请先解除固定再修改"
                ),
                blocking=True,
            )
        )
    return checks


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
        *_check_fixed(requirements, day_no, items),
        *_check_excluded(requirements, day_no, items),
        *_check_content(requirements, day_no, items, policy),
        _check_window(plan, policy),
        _check_time_conflicts(plan, day_no),
    ]
    locked = _check_locked(plan, locked_names, day_no)
    if (
        requirements
        and requirements.dietary_requirement
        and policy.playable
        and not any(item.get("item_type") == "food" for item in items)
    ):
        checks.append(
            ConstraintCheck(
                constraint_id=f"diet:day:{day_no}:missing",
                kind="dietary_requirement",
                day_no=day_no,
                status="unknown",
                reason="本日未提供餐饮安排，无法确认饮食要求已满足",
                blocking=diet_gate_blocking(requirements),
            )
        )
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
