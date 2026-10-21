"""来源文本中可确定提取的开放规则；缺失字段不猜测。"""

from datetime import date
from typing import Annotated, Literal

from pydantic import Field

from app.schemas.common import WireModel

Clock = Annotated[str, Field(pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$|^24:00$")]
Weekday = Annotated[int, Field(ge=0, le=6)]  # Monday=0


class OpeningInterval(WireModel):
    start: Clock
    end: Clock


class OpeningRule(WireModel):
    weekdays: list[Weekday] | None = None  # None = 原文没有周限定
    intervals: list[OpeningInterval] = Field(default_factory=list)
    closed: bool = False
    last_admission: Clock | None = None
    exceptions: list[str] = Field(default_factory=list)
    source_text: str  # 原文句子，不是模型改写


class OpeningHours(WireModel):
    status: Literal["known", "unknown"] = "unknown"
    rules: list[OpeningRule] = Field(default_factory=list)
    reason: str | None = None
    valid_from: date | None = None
    valid_to: date | None = None
