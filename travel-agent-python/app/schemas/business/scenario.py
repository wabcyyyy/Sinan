"""只使用已有安排的时间协商与版本恢复契约。"""

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


class ItineraryVersionVO(BaseModel):
    id: int
    versionNo: int
    parentVersionId: int | None
    operation: str
    summary: str
    createdAt: str | None
