"""确定性行程时间窗优化器。

LLM 只负责提出候选，最终顺序和时间由本模块计算。第一版采用小规模全排列
加贪心修复：最多 7 个活动时枚举顺序，选择能安排最多点位且交通耗时更少的
方案；更大输入使用当前顺序。无法满足营业时间或每日结束时间的可选点会被
移除，并返回原因，便于 UI/Trace 解释结果。
"""

from __future__ import annotations

import itertools
import re
from copy import deepcopy
from dataclasses import dataclass, field

from app.agent.data.route_service import ESTIMATE_SOURCE, RouteService, default_route_service, is_estimated
from app.agent.generation.rules.day_policy import MAX_DAILY_ATTRACTIONS, MAX_DAILY_MINUTES
from app.agent.generation.rules.transfer_time import estimate_transfer_minutes, parse_time

_OPEN_RE = re.compile(r"(\d{1,2}):(\d{2})\s*[-~至]\s*(\d{1,2}):(\d{2})")

# M3（spec §8.1）：矩阵缺路段时先走坐标估算；连坐标都缺（unknown）按保守罚分
# 参与排序——unknown 不能变成 0 分钟并作为最优解，也不能被算作达到步行上限。
UNKNOWN_ROUTE_SOURCE = "unknown-route"
UNKNOWN_ROUTE_PENALTY_MIN = 45


def _window(value: str | None) -> tuple[int, int] | None:
    if not value:
        return None
    match = _OPEN_RE.search(value)
    if not match:
        return None
    start = int(match.group(1)) * 60 + int(match.group(2))
    end = int(match.group(3)) * 60 + int(match.group(4))
    return start, end


def _key(item: dict) -> str:
    return str(item.get("poi_id") or item.get("id") or item.get("poi_name") or item.get("name") or "")


def _duration(item: dict) -> int:
    value = item.get("duration_min")
    if value is not None:
        try:
            return max(int(value), 1)
        except (TypeError, ValueError):
            pass
    start, end = parse_time(item.get("start_time")), parse_time(item.get("end_time"))
    if start is not None and end is not None and end > start:
        return end - start
    return 120


def _time(value: int) -> str:
    return f"{value // 60:02d}:{value % 60:02d}"


def _matrix_route(first: dict, second: dict, matrix: dict[tuple[str, str], dict]) -> dict | None:
    first_key, second_key = _key(first), _key(second)
    route = matrix.get((first_key, second_key))
    if route is None:
        # 测试替身或调用方可能按名称构造矩阵，而业务项使用 poi_id。
        route = matrix.get(
            (str(first.get("poi_name") or first.get("name")), str(second.get("poi_name") or second.get("name")))
        )
    if isinstance(route, dict):
        raw = route.get("duration_min")
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            return None
        return route
    return None


def _route_minutes(
    first: dict, second: dict, matrix: dict[tuple[str, str], dict], estimates: dict[tuple[str, str], int]
) -> tuple[int, dict | None]:
    """路段时长：真实矩阵 → 坐标估算（预计算表）→ unknown 保守罚分。

    unknown（缺坐标）不是 0 也不假装可达：罚分让含未知路段的排列在排序中
    劣于可估排列，source=unknown-route 如实进入 route_sources（degraded）。
    """
    route = _matrix_route(first, second, matrix)
    if route is not None:
        return max(int(route.get("duration_min") or 0), 0), route
    pair = (_key(first), _key(second))
    estimated = estimates.get(pair)
    if estimated is not None:
        return estimated, {"source": ESTIMATE_SOURCE, "duration_min": estimated}
    return UNKNOWN_ROUTE_PENALTY_MIN, None


@dataclass
class OptimizationResult:
    plan: dict
    score: float
    objective: str
    violations: list[str] = field(default_factory=list)
    removed_candidates: list[dict] = field(default_factory=list)
    travel_time_total_min: int = 0
    route_sources: list[str] = field(default_factory=list)
    degraded: bool = False

    def as_dict(self) -> dict:
        return {
            "plan": self.plan,
            "score": round(self.score, 4),
            "objective": self.objective,
            "violations": list(self.violations),
            "removed_candidates": list(self.removed_candidates),
            "travel_time_total_min": self.travel_time_total_min,
            "route_sources": list(dict.fromkeys(self.route_sources)),
            "degraded": self.degraded,
        }


def optimize_daily_plan(
    plan: dict,
    *,
    route_matrix: dict[tuple[str, str], dict] | None = None,
    route_service: RouteService | None = None,
    mode: str = "walking",
    objective: str = "可执行且少走路",
    day_start: str = "09:00",
    day_end: str = "20:30",
    max_attractions: int = MAX_DAILY_ATTRACTIONS,
    budget_limit: float | None = None,
    required_names: set[str] | None = None,
    locked_names: set[str] | None = None,
) -> OptimizationResult:
    """优化单日计划并返回解释报告；不会修改传入的 ``plan``。"""
    result_plan = deepcopy(plan)
    all_items = [deepcopy(item) for item in (result_plan.get("items") or []) if isinstance(item, dict)]
    active = [item for item in all_items if item.get("item_type") in ("attraction", "food")]
    inactive = [item for item in all_items if item.get("item_type") not in ("attraction", "food")]
    required = required_names or set()
    locked = locked_names or set()

    attractions = [item for item in active if item.get("item_type") == "attraction"]
    pre_removed: list[dict] = []
    if len(attractions) > max_attractions:
        keep = set(id(item) for item in attractions[:max_attractions]) | {
            id(item) for item in attractions if item.get("poi_name") in required
        }
        pre_removed = [
            {
                "poi_id": item.get("poi_id") or item.get("id"),
                "name": item.get("poi_name") or item.get("name"),
                "reason": f"超过每日景点上限 {max_attractions}",
            }
            for item in attractions
            if id(item) not in keep and item.get("poi_name") not in required
        ]
        active = [item for item in active if item.get("item_type") != "attraction" or id(item) in keep]

    matrix = route_matrix
    if matrix is None:
        service = route_service or default_route_service
        matrix = service.matrix(active, mode=mode)

    original_order = {id(item): index for index, item in enumerate(active)}
    orders = itertools.permutations(active) if len(active) <= 7 else (tuple(active),)

    # 坐标估算预计算（≤7 点两两 ≤42 对）：估算便宜但不能在 5040 个排列里重复算。
    estimates: dict[tuple[str, str], int] = {}
    for first in active:
        for second in active:
            if first is second:
                continue
            pair = (_key(first), _key(second))
            if pair in estimates:
                continue
            value = estimate_transfer_minutes(first, second, mode)
            if value is not None:
                estimates[pair] = value

    best: tuple[tuple[int, int, int, int], list[dict], list[str], list[dict], int, list[str]] | None = None
    start_min = parse_time(day_start) or 540
    end_min = parse_time(day_end) or 1230

    for order in orders:
        if locked:
            # 局部重规划只能调整未锁定点位；锁定点保留原来的活动槽位。
            locked_positions = {index: item for index, item in enumerate(active) if item.get("poi_name") in locked}
            if any(order[index] is not item for index, item in locked_positions.items()):
                continue
        cursor = start_min
        previous: dict | None = None
        scheduled: list[dict] = []
        violations: list[str] = []
        removed: list[dict] = list(pre_removed)
        travel_total = 0
        sources: list[str] = []
        spent = 0.0
        active_duration_total = 0

        for item in order:
            travel, route = (0, None) if previous is None else _route_minutes(previous, item, matrix, estimates)
            window = _window(item.get("open_time"))
            start = max(cursor + travel, window[0] if window else cursor + travel)
            finish = start + _duration(item)
            name = str(item.get("poi_name") or item.get("name") or "未命名地点")
            reason = None
            if window and finish > window[1]:
                reason = f"加入后超过营业时间（{item.get('open_time')}）"
            elif finish > end_min:
                reason = f"加入后超过每日结束时间 {_time(end_min)}"
            elif budget_limit is not None:
                cost = float(item.get("cost") or 0)
                if spent + cost > budget_limit:
                    reason = f"加入后超过当日预算 {budget_limit:g}"
            if reason is None and active_duration_total + _duration(item) > MAX_DAILY_MINUTES:
                reason = f"加入后超过每日活动时长上限 {MAX_DAILY_MINUTES} 分钟"
            if reason:
                if name in required:
                    violations.append(f"必去点「{name}」无法安排：{reason}")
                    # 让后续项目仍可被安排，最终报告会明确硬约束失败。
                    continue
                removed.append({"poi_id": item.get("poi_id") or item.get("id"), "name": name, "reason": reason})
                continue
            normalized = deepcopy(item)
            normalized["start_time"] = _time(start)
            normalized["end_time"] = _time(finish)
            normalized["duration_min"] = _duration(item)
            scheduled.append(normalized)
            if route is not None:
                sources.append(str(route.get("source") or "unknown"))
            elif previous is not None:
                sources.append(UNKNOWN_ROUTE_SOURCE)
            travel_total += travel
            spent += float(item.get("cost") or 0)
            active_duration_total += _duration(item)
            cursor = finish
            previous = item

        # M3（spec §8.3）目标序：可行硬约束优先 → 体力/路程 → 变更范围 → 可选数量。
        # 不能为了增加数量丢必去点（violations/required_missing 排在数量之前）。
        required_missing = sum(1 for name in required if name not in {item.get("poi_name") for item in scheduled})
        order_change = sum(abs(index - original_order[id(item)]) for index, item in enumerate(order))
        rank = (
            -len(violations) - required_missing,
            -travel_total,
            -len(removed) - order_change,
            len(scheduled),
        )
        if best is None or rank > best[0]:
            best = (rank, scheduled, violations, removed, travel_total, sources)

    assert best is not None
    _, scheduled, violations, removed, travel_total, sources = best
    # 酒店、交通等非活动项继续保留，酒店通常放在日程末尾便于展示。
    result_plan["items"] = scheduled + inactive
    target = max(len(active), 1)
    score = max(0.0, min(1.0, len(scheduled) / target))
    score -= min(travel_total / 2400, 0.25)
    score -= min(len(violations) * 0.2, 0.5)
    return OptimizationResult(
        plan=result_plan,
        score=score,
        objective=objective,
        violations=violations,
        removed_candidates=removed,
        travel_time_total_min=travel_total,
        route_sources=sources,
        degraded=any(is_estimated(source) or source == UNKNOWN_ROUTE_SOURCE for source in sources) and bool(sources),
    )
