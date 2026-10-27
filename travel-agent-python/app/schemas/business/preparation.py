"""行程私有准备资料；用户记录不改变地点事实的核验状态。"""

from datetime import datetime
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.schemas.screenshot_input import ScreenshotEvidence


class PreparationEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    id: str = Field(min_length=1, max_length=64)


class SavedPlace(PreparationEntry):
    name: str = Field(min_length=1, max_length=128)
    address: str = Field(default="", max_length=255)
    url: str = Field(default="", max_length=2048, pattern=r"^(https?://[^\s]+)?$")
    note: str = Field(default="", max_length=1000)
    latitude: float | None = Field(default=None, ge=-90, le=90, allow_inf_nan=False)
    longitude: float | None = Field(default=None, ge=-180, le=180, allow_inf_nan=False)
    coordinateSource: Literal["map_wgs84"] | None = None

    @model_validator(mode="after")
    def coordinates(self):
        if (self.latitude is None) != (self.longitude is None):
            raise ValueError("坐标必须成对填写")
        if self.latitude is not None and (
            self.coordinateSource != "map_wgs84" or (self.latitude == 0 and self.longitude == 0)
        ):
            raise ValueError("只接受用户在地图选择的 WGS84 落点")
        return self


class TravelBooking(PreparationEntry):
    kind: Literal["hotel", "transport", "ticket", "other"]
    name: str = Field(min_length=1, max_length=128)
    startAt: datetime | None = None
    endAt: datetime | None = None
    reference: str = Field(default="", max_length=128)
    address: str = Field(default="", max_length=255)
    note: str = Field(default="", max_length=2000)
    documentUrl: str = Field(default="", max_length=2048, pattern=r"^(https?://[^\s]+)?$")
    itemId: int | None = Field(default=None, ge=1)
    inputEvidence: ScreenshotEvidence | None = None
    timeZone: str | None = Field(default=None, max_length=64)
    originalAmount: str | None = Field(default=None, max_length=32, pattern=r"^\d{1,7}(\.\d{1,2})?$")
    originalCurrency: str | None = Field(default=None, pattern=r"^[A-Z]{3}$")

    @field_validator("timeZone")
    @classmethod
    def valid_zone(cls, value):
        if value is not None:
            try:
                ZoneInfo(value)
            except (ValueError, ZoneInfoNotFoundError) as exc:
                raise ValueError("需要有效的 IANA 当地时区") from exc
        return value

    @model_validator(mode="after")
    def period(self):
        if self.startAt and self.endAt and (self.startAt.utcoffset() is None) != (self.endAt.utcoffset() is None):
            raise ValueError("开始与结束应采用一致的当地时间或带时区时间")
        if self.startAt and self.endAt and self.endAt <= self.startAt:
            raise ValueError("结束必须晚于开始；跨夜请选次日日期")
        return self


class PreparationTask(PreparationEntry):
    title: str = Field(min_length=1, max_length=200)
    category: Literal["reservation", "transport", "stay", "documents", "packing", "other"] = "other"
    dayNo: int | None = Field(default=None, ge=1, le=7)
    done: bool = False


class UserConfirmation(PreparationEntry):
    itemId: int = Field(ge=1)
    name: str = Field(min_length=1, max_length=128)
    aspect: Literal["location", "opening", "price", "booking"]
    note: str = Field(default="", max_length=1000)
    sourceUrl: str = Field(default="", max_length=2048, pattern=r"^(https?://[^\s]+)?$")
    recordedAt: str | None = None


class PreparationData(BaseModel):
    model_config = ConfigDict(extra="forbid")
    places: list[SavedPlace] = Field(default_factory=list, max_length=100)
    bookings: list[TravelBooking] = Field(default_factory=list, max_length=100)
    tasks: list[PreparationTask] = Field(default_factory=list, max_length=150)
    confirmations: list[UserConfirmation] = Field(default_factory=list, max_length=200)
    completedItemIds: list[int] = Field(default_factory=list, max_length=200)

    @model_validator(mode="after")
    def unique_ids(self):
        for entries in (self.places, self.bookings, self.tasks, self.confirmations):
            if len({entry.id for entry in entries}) != len(entries):
                raise ValueError("同类资料的 ID 不可重复")
        return self


class PreparationPutBody(BaseModel):
    revision: int = Field(ge=0)
    data: PreparationData


class PreparationVO(PreparationPutBody):
    updatedAt: str | None = None
