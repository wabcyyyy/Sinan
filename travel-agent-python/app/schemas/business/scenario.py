"""只使用已有安排的时间协商与版本恢复契约。"""

from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field


class ScenarioBody(BaseModel):
    dayNo: int = Field(ge=1, le=7)
    planningRevision: int = Field(ge=0)
    startTime: str = Field(pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    finishBy: str = Field(pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    transferMinutes: int = Field(ge=0, le=240)
    departureBufferMinutes: int = Field(ge=0, le=480)
    fixedItemIds: list[int] = Field(default_factory=list, max_length=20)
    optionalItemIds: list[int] = Field(default_factory=list, max_length=20)


class ScenarioSelectBody(ScenarioBody):
    choice: Literal["keep", "omit"]


class ScenarioParseBody(BaseModel):
    dayNo: int = Field(ge=1, le=7)
    planningRevision: int = Field(ge=0)
    message: str = Field(min_length=1, max_length=2000)


class ScenarioIntentVO(BaseModel):
    startTime: str | None = Field(default=None, pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    finishBy: str | None = Field(default=None, pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    transferMinutes: int | None = Field(default=None, ge=0, le=240)
    departureBufferMinutes: int | None = Field(default=None, ge=0, le=480)
    fixedItemIds: list[int] = Field(default_factory=list, max_length=20)
    optionalItemIds: list[int] = Field(default_factory=list, max_length=20)
    questions: list[str] = Field(default_factory=list, max_length=10)


class ScenarioItemVO(BaseModel):
    id: int
    name: str
    startTime: str | None
    endTime: str | None
    fixed: bool


class ScenarioOptionVO(BaseModel):
    key: Literal["keep", "omit"]
    title: str
    feasible: bool
    conflicts: list[str]
    items: list[ScenarioItemVO]
    removed: list[str]
    finishTime: str | None
    slackMinutes: int | None
    costDelta: float | None


class ScenarioCompareVO(BaseModel):
    planningRevision: int
    options: list[ScenarioOptionVO]
    assumptions: list[str]


class RestoreVersionBody(BaseModel):
    planningRevision: int = Field(ge=0)


class ItemEditFields(BaseModel):
    startTime: str | None = Field(default=None, pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    endTime: str | None = Field(default=None, pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    address: str | None = Field(default=None, max_length=255)
    cost: Decimal | None = Field(default=None, ge=0, le=1_000_000, max_digits=12, decimal_places=2)
    remark: str | None = Field(default=None, max_length=255)


class ItemActionBody(ItemEditFields):
    dayNo: int | None = Field(default=None, ge=1, le=7)
    poiName: str | None = Field(default=None, min_length=1, max_length=128)
    planningRevision: int = Field(ge=0)
    action: Literal["update", "delete", "fix", "unfix"]


class ItemCreateBody(ItemEditFields):
    planningRevision: int = Field(ge=0)
    action: Literal["create"] = "create"
    itemType: Literal["attraction", "food", "hotel"]
    poiName: str = Field(min_length=1, max_length=128)
    dayNo: int = Field(ge=1, le=7)
    fixed: bool = False


class ItineraryVersionVO(BaseModel):
    id: int
    versionNo: int
    parentVersionId: int | None
    operation: str
    summary: str
    createdAt: str | None
