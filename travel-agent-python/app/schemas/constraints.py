"""约束检查报告（M3，spec §8.2）：规则校验结果的类型定义，schemas 单一源。

职责：
- ConstraintCheck：单条约束检查结果——constraint_id 稳定可定位（必去用
  RequiredPlace.constraint_id，其余按 kind:目标 命名），status 四态
  （pass/violation/unknown/not_applicable），blocking 标记是否硬阻断交付；
- ConstraintReport：一次检查的集合，`has_blocking` 供交付门判定。

语义边界（spec 原文）：
- "pass" 只表示在当前输入/估算下符合排程，不表示实地事实已全部核实；
- 关键 unknown（未支持类别、缺坐标致窗口不可验证）必须显式标 unknown，
  不得算 pass；普通非阻断事实（缺图片、估算票价）不进本报告（走警告）；
- 中文字符串 issues 只是展示派生值，关键业务逻辑一律判 status/blocking。

依赖：pydantic；无内部依赖。
"""

from typing import Literal

from pydantic import Field

from app.schemas.common import WireModel

ConstraintKind = Literal[
    "required_place",  # 指定日必去（含全程必去的单日投影）
    "excluded_place",  # 排除地点
    "excluded_category",  # 排除类别（受控词表内）
    "day_window",  # 明确到离时间窗口
    "locked_item",  # 锁定条目（身份与明确时间）
    "budget_strict",  # strict（hard_cap）预算上限（无容差）
    "time_conflict",  # 时间冲突
]
CheckStatus = Literal["pass", "violation", "unknown", "not_applicable"]


class ConstraintCheck(WireModel):
    constraint_id: str = Field(min_length=1, max_length=128)
    kind: ConstraintKind
    status: CheckStatus
    day_no: int | None = None
    #: 涉及的条目定位（poi_name / item 引用）
    item_refs: list[str] = Field(default_factory=list, max_length=50)
    reason: str = Field(default="", max_length=400)
    #: 依据（矩阵 source、估算标记、需求来源轮次等）
    evidence_refs: list[str] = Field(default_factory=list, max_length=10)
    blocking: bool = False


class ConstraintReport(WireModel):
    checks: list[ConstraintCheck] = Field(default_factory=list, max_length=200)

    @property
    def blocking_checks(self) -> list[ConstraintCheck]:
        return [check for check in self.checks if check.blocking and check.status == "violation"]

    @property
    def has_blocking(self) -> bool:
        return any(check.blocking and check.status == "violation" for check in self.checks)

    @property
    def unknown_checks(self) -> list[ConstraintCheck]:
        return [check for check in self.checks if check.status == "unknown"]
