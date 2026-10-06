"""条目对/错反馈（SPEC C3.5）跨端契约模型——登记进 contracts.py 的 business_api 组。

线级口径（SPEC §1 推荐列）：值域二元 right/wrong（库内 1/0）；reason 只在
value=wrong 时必填、value=right 恒空；note 可选 ≤200 字。展示匿名：VO 不含
userId——反馈是私域质量信号，user_id 不出管理员审计面之外。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator

FeedbackValue = Literal["right", "wrong"]

#: 错误原因枚举（英文入库、前端中文映射）；value=right 时必须缺省。
FeedbackReason = Literal["wrong_location", "wrong_time", "wrong_price", "not_interested", "closed", "other"]


class FeedbackCreate(BaseModel):
    itemId: int = Field(ge=1)
    value: FeedbackValue
    reason: FeedbackReason | None = None
    note: str | None = Field(default=None, max_length=200)

    @model_validator(mode="after")
    def _reason_gate(self) -> FeedbackCreate:
        if self.value == "wrong" and self.reason is None:
            raise ValueError("标记错误时必须选择原因")
        if self.value == "right" and self.reason is not None:
            raise ValueError("标记正确时不携带原因")
        return self


class FeedbackVO(BaseModel):
    """本人反馈回显视图：展示匿名，刻意不含 userId/itineraryId。"""

    itemId: int
    value: FeedbackValue
    reason: FeedbackReason | None
    note: str | None
    createdAt: str
    updatedAt: str


class FeedbackListVO(BaseModel):
    feedbacks: list[FeedbackVO]
