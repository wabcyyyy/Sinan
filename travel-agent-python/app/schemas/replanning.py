"""P2：UI 与受控自然语言共用的修改操作及候选比较契约。"""

from typing import Literal

from pydantic import Field, model_validator

from app.schemas.active_evidence import ActiveEvidenceReport
from app.schemas.common import WireModel
from app.schemas.trip_requirements import MAX_TRIP_DAYS, TripRequirements


class ConstraintRepairBody(WireModel):
    planning_revision: int = Field(ge=0)
    constraint_id: str = Field(min_length=1, max_length=128)
    action: Literal["soften_relation", "remove_required"]


class ConstraintRepairVO(WireModel):
    requirements: TripRequirements
    base_revision: int
    notice: str


class ReplanChange(WireModel):
    kind: Literal["move", "delay", "close", "finish_by", "appointment", "prefer"]
    item_id: int | None = Field(default=None, ge=1)
    day_no: int | None = Field(default=None, ge=1, le=MAX_TRIP_DAYS)
    anchor_item_id: int | None = Field(default=None, ge=1)
    placement: Literal["before", "after"] = "before"
    start_time: str | None = Field(default=None, pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    end_time: str | None = Field(default=None, pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    delay_minutes: int | None = Field(default=None, ge=1, le=480)

    @model_validator(mode="after")
    def required_fields(self):
        if self.kind != "finish_by" and self.item_id is None:
            raise ValueError("本次操作必须指定条目 ID")
        if self.kind == "move" and self.day_no is None:
            raise ValueError("移动必须指定目标日")
        if self.kind == "move" and self.end_time and not self.start_time:
            raise ValueError("修改结束时间时需要同时指定开始时间")
        if self.kind == "delay" and self.delay_minutes is None:
            raise ValueError("延误必须指定分钟数")
        if self.kind == "finish_by" and (self.day_no is None or self.end_time is None):
            raise ValueError("提前结束必须指定日期和截止时间")
        if self.kind == "appointment" and (self.day_no is None or not self.start_time or not self.end_time):
            raise ValueError("预约必须给出日期与完整时段")
        if self.start_time and self.end_time and self.start_time >= self.end_time:
            raise ValueError("暂不支持跨午夜的预约")
        return self


class ReplanBody(WireModel):
    planning_revision: int = Field(ge=0)
    change: ReplanChange
    completed_item_ids: list[int] = Field(default_factory=list, max_length=20)
    optional_item_ids: list[int] = Field(default_factory=list, max_length=20)
    allow_cross_day: bool = True


class ReplanParseBody(WireModel):
    planning_revision: int = Field(ge=0)
    message: str = Field(min_length=1, max_length=2000)
    item_id: int | None = Field(default=None, ge=1)
    day_no: int = Field(ge=1, le=MAX_TRIP_DAYS)


class ReplanParseVO(WireModel):
    change: ReplanChange | None = None
    questions: list[str] = Field(default_factory=list)
    parser: Literal["bounded_rules"] = "bounded_rules"


class ReplanMetrics(WireModel):
    removed_items: int = 0
    cross_day_moves: int = 0
    changed_items: int = 0
    order_changes: int = 0
    start_shift_minutes: int = 0
    travel_minutes: int = 0
    mean_start_shift_minutes: float = 0
    locked_violations: int = 0


class ReplanAttempt(WireModel):
    scope: Literal["local", "day", "trip"]
    status: str
    movable_item_ids: list[int]
    reason: str
    seconds: float


class ReplanOption(WireModel):
    key: str
    title: str
    numerical_status: str
    fact_status: str
    metrics: ReplanMetrics
    changed_item_ids: list[int]
    affected_day_nos: list[int]
    reasons: list[str]
    signature: str


class ReplanCompareVO(WireModel):
    comparison_id: str | None = None
    planning_revision: int
    options: list[ReplanOption]
    attempts: list[ReplanAttempt]
    conflicts: list[str]
    dependencies: list[str]
    assumptions: list[str]
    active_evidence: ActiveEvidenceReport | None = None


class ReplanSelectBody(WireModel):
    planning_revision: int = Field(ge=0)
    comparison_id: str = Field(min_length=1, max_length=64)
    choice: str = Field(min_length=1, max_length=40)
