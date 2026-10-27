"""P3 截图抽取、显式地点查询与确认的业务契约。"""

from datetime import date
from decimal import Decimal
from typing import Literal

from pydantic import Field, model_validator

from app.schemas.business.preparation import TravelBooking
from app.schemas.common import WireModel
from app.schemas.screenshot_input import ScreenshotEntry
from app.schemas.trip_requirements import TripRequirements


class ScreenshotExtraction(WireModel):
    entries: list[ScreenshotEntry] = Field(default_factory=list, max_length=8)
    questions: list[str] = Field(default_factory=list, max_length=8)


class ScreenshotDocumentVO(ScreenshotExtraction):
    document_id: str
    image_id: str
    width: int
    height: int
    document_kind: Literal["guide", "booking"]
    extracted_at: str
    extractor: str


class ScreenshotResolveBody(WireModel):
    document_id: str = Field(min_length=1, max_length=64)
    record_id: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=64)
    city: str = Field(min_length=1, max_length=80)
    country_code: str = Field(pattern=r"^[A-Z]{2}$")


class ScreenshotEntity(WireModel):
    entity_id: str
    name: str
    aliases: list[str] = Field(default_factory=list)
    address: str
    country_code: str
    latitude: float | None = Field(default=None, ge=-90, le=90, allow_inf_nan=False)
    longitude: float | None = Field(default=None, ge=-180, le=180, allow_inf_nan=False)
    source_url: str | None = None
    identity_source: Literal["nominatim", "user_provided"] = "nominatim"
    match_score: int = 0
    match_reasons: list[str] = Field(default_factory=list)
    retrieved_at: str | None = None
    category: str
    entity_type: str


class ScreenshotManualBody(ScreenshotResolveBody):
    address: str = Field(min_length=1, max_length=255)
    latitude: float | None = Field(default=None, ge=-90, le=90, allow_inf_nan=False)
    longitude: float | None = Field(default=None, ge=-180, le=180, allow_inf_nan=False)
    entity_type: Literal["unconfirmed", "museum", "station", "airport", "bus_station", "ferry_terminal"] = "unconfirmed"

    @model_validator(mode="after")
    def coordinates(self):
        if (self.latitude is None) != (self.longitude is None) or (self.latitude == 0 and self.longitude == 0):
            raise ValueError("坐标需成对填写有效 WGS84 经纬度，或两项均留空待定位")
        return self


class ScreenshotResolveVO(WireModel):
    resolution_id: str
    status: Literal["choices", "not_found", "unavailable"]
    choices: list[ScreenshotEntity] = Field(default_factory=list)
    question: str


class ScreenshotConfirmBody(WireModel):
    document_id: str = Field(min_length=1, max_length=64)
    record_id: str = Field(min_length=1, max_length=64)
    resolution_id: str = Field(min_length=1, max_length=64)
    entity_id: str = Field(min_length=1, max_length=128)
    disposition: Literal["candidate", "required", "excluded", "fixed", "booking"]
    requirements: TripRequirements
    start_date: date | None = None
    days: int = Field(ge=1, le=3)
    local_date: date | None = None
    end_date: date | None = None
    start_time: str | None = Field(default=None, pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    end_time: str | None = Field(default=None, pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    time_zone: str | None = Field(default=None, max_length=64)
    amount: Decimal | None = Field(default=None, ge=0, le=1_000_000, decimal_places=2)
    currency: str | None = Field(default=None, pattern=r"^[A-Z]{3}$")
    # 对原图否定语义的改写必须由用户单独明确选择。
    override_polarity: bool = False


class ScreenshotConfirmVO(WireModel):
    requirements: TripRequirements
    booking: TravelBooking | None = None
    notice: str
