"""约束规划的目的地口径及结果；需求金额仍使用原有 budget 字段。"""

from datetime import date
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, field_validator

from app.schemas.active_evidence import ActiveEvidenceOptions, ActiveEvidenceReport
from app.schemas.common import WireModel
from app.schemas.constraints import ConstraintReport


class ConstraintPlanningOptions(WireModel):
    """显式选择实验规划路径；本币/时区/国家由用户确认，不猜未知城市。"""

    country_code: str = Field(pattern=r"^[A-Z]{2}$")
    time_zone: str = Field(min_length=1, max_length=64)
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    active_evidence: ActiveEvidenceOptions | None = None

    @field_validator("time_zone")
    @classmethod
    def valid_time_zone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError("需要有效的 IANA 目的地时区") from exc
        return value


class PlanningRouteFact(WireModel):
    from_id: str
    to_id: str
    mode: str
    duration_min: int | None = None
    source: str
    source_url: str | None = None
    attribution: str | None = None
    fetched_at: str | None = None
    data_date: str | None = None
    basis: Literal["road_network", "coordinate_estimate", "unknown"]
    # ORS 静态路网时长不等于实时交通，也不伪造出发时间桶。
    time_dependent: bool = False


class PlanningFactContext(WireModel):
    candidate_id: str
    name: str
    aliases: list[str] = Field(default_factory=list)
    source: str
    source_entity_id: str | None = None
    source_url: str | None = None
    source_language: str | None = None
    retrieved_at: str | None = None
    country_code: str | None = None
    original_currency: str | None = None
    original_amount: float | None = None


class ConstraintRepairSuggestion(WireModel):
    constraint_id: str
    action: Literal["soften_relation", "remove_required"]
    label: str
    source_text: str | None = None


class ConstraintPlanningAssessment(WireModel):
    status: Literal["OPTIMAL", "FEASIBLE", "INFEASIBLE", "UNKNOWN"]
    solver_status: str
    numerical_status: Literal["OPTIMAL", "FEASIBLE", "INFEASIBLE", "UNKNOWN"]
    planning_revision: int | None = None
    is_current: bool = True
    conditional: bool = True
    scope: Literal["single_city_trip"] = "single_city_trip"
    local_date: date
    destination: ConstraintPlanningOptions
    destination_source: Literal["user_confirmed"] = "user_confirmed"
    budget_scope: str = "仅已列活动费用；未列交通、住宿或其他费用待补，不代表全程总费用"
    report: ConstraintReport = Field(default_factory=ConstraintReport)
    conflict_ids: list[str] = Field(default_factory=list)
    conflict_irreducible: bool = False
    repair_suggestions: list[ConstraintRepairSuggestion] = Field(default_factory=list)
    routes: list[PlanningRouteFact] = Field(default_factory=list)
    facts: list[PlanningFactContext] = Field(default_factory=list)
    known_cost: float = 0
    total_cost: float | None = None
    solve_seconds: float = 0
    optimized_tiers: int = 0
    total_tiers: int = 0
    objective_bound: float | None = None
    reason: str
    active_evidence: ActiveEvidenceReport | None = None
