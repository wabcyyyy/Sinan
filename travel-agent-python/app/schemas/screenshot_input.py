"""截图来源与用户选定实体；不是营业/票价的官方核验凭据。"""

from decimal import Decimal
from typing import Literal

from pydantic import Field, model_validator

from app.schemas.common import WireModel


class EvidenceRegion(WireModel):
    x: float = Field(ge=0, le=1, allow_inf_nan=False)
    y: float = Field(ge=0, le=1, allow_inf_nan=False)
    width: float = Field(gt=0, le=1, allow_inf_nan=False)
    height: float = Field(gt=0, le=1, allow_inf_nan=False)

    @model_validator(mode="after")
    def inside_image(self):
        if self.x + self.width > 1.000001 or self.y + self.height > 1.000001:
            raise ValueError("证据区域必须位于图片内")
        return self


class ScreenshotField(WireModel):
    key: Literal[
        "name",
        "localName",
        "city",
        "countryCode",
        "date",
        "endDate",
        "startTime",
        "endTime",
        "timeZone",
        "currency",
        "amount",
        "note",
    ]
    value: str = Field(min_length=1, max_length=400)
    quote: str = Field(min_length=1, max_length=600)
    region: EvidenceRegion
    language: str | None = Field(default=None, max_length=40)


class ScreenshotEntry(WireModel):
    record_id: str = Field(default="", max_length=64)
    kind: Literal["place", "reservation", "transport"]
    polarity: Literal["recommended", "excluded", "unclear"] = "unclear"
    fields: list[ScreenshotField] = Field(min_length=1, max_length=16)
    questions: list[str] = Field(default_factory=list, max_length=8)

    @model_validator(mode="after")
    def unique_fields(self):
        if len({f.key for f in self.fields}) != len(self.fields):
            raise ValueError("同一条记录的抽取字段不可重复")
        return self


class ScreenshotEvidence(WireModel):
    image_id: str = Field(min_length=1, max_length=64)
    record_id: str = Field(min_length=1, max_length=64)
    width: int = Field(ge=1)
    height: int = Field(ge=1)
    extracted_at: str
    extractor: str
    fields: list[ScreenshotField] = Field(min_length=1, max_length=16)
    confirmed_fields: dict[str, str] = Field(default_factory=dict, max_length=16)
    # 原图仅在当前页面内存预览；本结构不存图片或公开订单 URL。
    status: Literal["user_confirmed_input"] = "user_confirmed_input"


class ConfirmedPlaceInput(WireModel):
    input_id: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=64)
    local_name: str = Field(min_length=1, max_length=128)
    item_type: Literal["attraction", "transport"] = "attraction"
    aliases: list[str] = Field(default_factory=list, max_length=40)
    city: str = Field(min_length=1, max_length=80)
    address: str | None = Field(default=None, min_length=1, max_length=255)
    country_code: str = Field(pattern=r"^[A-Z]{2}$")
    entity_id: str = Field(min_length=1, max_length=128)
    latitude: float | None = Field(default=None, ge=-90, le=90, allow_inf_nan=False)
    longitude: float | None = Field(default=None, ge=-180, le=180, allow_inf_nan=False)
    source_url: str | None = Field(default=None, pattern=r"^https://www\.openstreetmap\.org/(node|way|relation)/\d+$")
    identity_source: Literal["nominatim", "user_provided"] = "nominatim"
    retrieved_at: str | None = None
    original_amount: Decimal | None = Field(default=None, ge=0, le=1_000_000, decimal_places=2)
    original_currency: str | None = Field(default=None, pattern=r"^[A-Z]{3}$")
    evidence: ScreenshotEvidence

    @model_validator(mode="after")
    def identity_provenance(self):
        if (self.latitude is None) != (self.longitude is None) or (self.latitude == 0 and self.longitude == 0):
            raise ValueError("坐标需成对且不能使用零点占位")
        if self.identity_source == "nominatim" and (not self.source_url or self.latitude is None):
            raise ValueError("地图实体需要来源及坐标")
        if self.identity_source == "user_provided" and (not self.entity_id.startswith("manual:") or self.source_url):
            raise ValueError("手工地点不可伪装为地图实体")
        return self
