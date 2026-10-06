"""模板（SPEC C2.4）跨端契约模型——登记进 contracts.py 的 business_api 组。

投影物是脱敏快照：不含 expense/成员/userId/备注；花费只以档位文案出现。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class TemplateCostTier(BaseModel):
    category: Literal["门票", "餐饮", "交通", "酒店"]
    amountRangeText: str = Field(min_length=1, max_length=64)


class TemplateItemVO(BaseModel):
    poiName: str
    itemType: str | None = None
    startTime: str | None = None
    endTime: str | None = None
    latitude: float | None = None
    longitude: float | None = None


class TemplateDayVO(BaseModel):
    dayNo: int
    theme: str | None = None
    items: list[TemplateItemVO]


class TemplateSummaryVO(BaseModel):
    """发布物化进 itinerary_main.template_summary 的投影结构。"""

    title: str
    city: str
    days: int
    persons: int
    intro: str
    costTiers: list[TemplateCostTier]
    dayList: list[TemplateDayVO]


class TemplatePublishVO(BaseModel):
    itineraryId: int
    publishedAt: str
    summary: TemplateSummaryVO


class TemplateCardVO(BaseModel):
    """广场卡片：封面/城市/天数/花费档位（作者等身份信息刻意不出）。"""

    id: int
    title: str
    city: str
    days: int
    coverUrl: str | None = None
    costTiers: list[TemplateCostTier]
    publishedAt: str


class TemplateDetailVO(TemplateCardVO):
    summary: TemplateSummaryVO


class TemplateListVO(BaseModel):
    templates: list[TemplateCardVO]


class TemplateForkVO(BaseModel):
    itineraryId: int
    title: str
