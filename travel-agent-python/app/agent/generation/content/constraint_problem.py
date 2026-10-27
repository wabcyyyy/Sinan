"""单日求解快照与结果。需求仍只来自 TripRequirements；矩阵直接消费 RouteService。

P0 的分钟/金额分别是当地单日分钟和该币种的 1/100 单位；不含汇率或实时交通。
输入均为人工冻结事实；没有供应商/LLM/数据库 IO。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Literal

from app.agent.generation.content.constraint_candidate_policy import DAY_CANDIDATE_LIMIT
from app.schemas.constraints import ConstraintReport
from app.schemas.trip_requirements import MAX_TRIP_DAYS, TripRequirements


def minute(value: str) -> int:
    hour, minutes = value.split(":")
    return int(hour) * 60 + int(minutes)


def money_minor(value: Decimal) -> int:
    if not value.is_finite() or value < 0 or value * 100 != (value * 100).to_integral_value():
        raise ValueError("P0 金额须非负且最多两位小数，不进行隐式舍入")
    return int(value * 100)


@dataclass(frozen=True)
class PlanningCandidate:
    candidate_id: str
    name: str
    duration_min: int
    price_minor: int | None
    currency: str
    # None = 未知；空 tuple = 已知全天不可用；可含午休等多段营业窗。
    opening_windows: tuple[tuple[int, int], ...] | None = None
    utility: int = 1
    item_type: str = "attraction"
    categories: tuple[str, ...] | None = None
    source: str = "manual-fixture"
    aliases: tuple[str, ...] = ()


@dataclass(frozen=True)
class DayPlanningProblem:
    requirements: TripRequirements
    candidates: tuple[PlanningCandidate, ...]
    route_matrix: dict[tuple[str, str], dict]
    currency: str
    day_no: int = 1
    # 金额从调用方的既有 budget 字段传入；单日原型不分摊全程预算。
    budget: Decimal | None = None

    def __post_init__(self) -> None:
        if not 1 <= len(self.candidates) <= DAY_CANDIDATE_LIMIT or not 1 <= self.day_no <= MAX_TRIP_DAYS:
            raise ValueError("P0 支持 1–20 个候选及合法单日编号")
        ids = [c.candidate_id for c in self.candidates]
        if len(set(ids)) != len(ids) or any(not key for key in ids):
            raise ValueError("候选 ID 必须非空且唯一")
        if not self.currency:
            raise ValueError("必须指定快照币种")
        constraint_ids = [
            c.constraint_id for c in [*self.requirements.required_places, *self.requirements.place_relations]
        ]
        if len(set(constraint_ids)) != len(constraint_ids):
            raise ValueError("用户约束 ID 必须唯一，才能解释冲突来源")
        if self.budget is not None:
            money_minor(self.budget)
        for candidate in self.candidates:
            if not candidate.name or not candidate.source or not candidate.currency:
                raise ValueError("候选名称、来源及币种不可为空")
            internal_origin = (
                self.requirements.fact_source == "chinatravel"
                and candidate.item_type == "anchor"
                and candidate.candidate_id.startswith("origin:")
            )
            if (
                not isinstance(candidate.duration_min, int)
                or not (0 if internal_origin else 1) <= candidate.duration_min <= 1440
            ):
                raise ValueError("访问时长必须为 1–1440 整数分钟")
            if not isinstance(candidate.utility, int) or not 0 <= candidate.utility <= 1000:
                raise ValueError("候选收益须为 0–1000 整数")
            if candidate.price_minor is not None and (
                # P1 候选费用可为最多 20 人的合计；不是每人单价。
                not isinstance(candidate.price_minor, int) or not 0 <= candidate.price_minor <= 2_000_000_000
            ):
                raise ValueError("候选金额须为非负整数分")
            for start, end in candidate.opening_windows or ():
                if not 0 <= start < end <= 1440:
                    raise ValueError("营业窗须在同一当地日内且起点小于终点")
        for (first, second), route in self.route_matrix.items():
            if first not in ids or second not in ids or first == second:
                raise ValueError("矩阵边必须引用两个不同候选 ID")
            duration = route.get("duration_min")
            if not isinstance(duration, int) or not 0 <= duration <= 1440 or not route.get("source"):
                raise ValueError("矩阵边必须有非负整数分钟和来源")
            if route.get("from_poi_id", first) != first or route.get("to_poi_id", second) != second:
                raise ValueError("矩阵键与路线实体身份不一致")


@dataclass(frozen=True)
class ScheduledVisit:
    candidate_id: str
    start_min: int
    end_min: int


@dataclass
class DayPlanningResult:
    status: Literal["OPTIMAL", "FEASIBLE", "INFEASIBLE", "UNKNOWN"]
    solver_status: str
    visits: list[ScheduledVisit] = field(default_factory=list)
    report: ConstraintReport = field(default_factory=ConstraintReport)
    conflict_ids: list[str] = field(default_factory=list)
    conflict_irreducible: bool = False
    objective: list[int] = field(default_factory=list)
    objective_bound: float | None = None
    optimized_tiers: int = 0
    total_tiers: int = 0
    travel_min: int = 0
    known_cost_minor: int = 0
    total_cost_minor: int | None = None
    elapsed_s: float = 0
    timings: list[dict] = field(default_factory=list)
    conditional: bool = True
    reason: str = ""
    selected_routes: list[dict] = field(default_factory=list)
