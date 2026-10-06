"""行程/生成域线级契约（G-1.1 就地迁入：原 app/api/business/itinerary.py 的 Body 类
与 services 层的 GenerateTripRequest/ItemUpsertRequest/HotelOptionRequest）。

职责：
- 生成（/api/itinerary/generate）、条目写路径（items）、酒店应用、NL 编辑、
  对话编辑、偏好信号、版本、封面/收藏/归档/分享等端点的请求体模型；
- 字段名与校验规则逐字保留：多数字段名本身就是 camelCase（与 Java DTO 对齐，
  前端已在发这个形状），不换 WireModel 别名机制。

边界：
- 本域端点的响应多为 itinerary_query.detail() 动态组装的详情 VO（ORM +
  metadata_json 透传的开放结构），本期不建模，理由见包 __init__；
- GenerateTripRequest 的约束不写在 pydantic 上：报错文案走 Java Bean Validation
  同序的 _validate（services/itinerary_generation.py），保持前端拿同一句中文提示。

依赖：
- app.schemas.common.WireModel（仅 GenerateTripRequest）；pydantic。
"""

from datetime import date, datetime, time
from decimal import Decimal
from typing import Any

from pydantic import BaseModel

from app.schemas.common import WireModel


class GenerateTripRequest(WireModel):
    """`POST /api/itinerary/generate` 的请求体；与 Java `GenerateRequest` 字段一一对应。

    约束**不写在 pydantic 上**：Java 的报错文案来自 Bean Validation 的 message，
    由 `_validate` 按同一顺序逐条判定，才能保证前端拿到同一句中文提示。
    """

    city: str | None = None
    days: int | None = None
    persons: int | None = 1
    stayNights: int | None = None
    startDate: date | None = None
    endDate: date | None = None
    # budget 上界 1000000（API-2，_validate 判定，与 cost 先例/DECIMAL(12,2) 对齐）
    budget: Decimal | None = None
    preferences: list[str] | None = None
    hotelTier: str | None = None
    regionHint: str | None = None
    # 可选出发地（L13 契约先行；L16 表单透传、L14 接线进 agent 请求）。有 origin
    # 才查航班。约束同本模型其余可选字段不上 pydantic（_validate 的 Java 文案口径，
    # 可选字段无校验规则）。
    originCity: str | None = None
    # requirements 上界 10000 字（API-2，_validate 判定；Text 列 64KB 字节防溢出）
    requirements: str | None = None
    intent: str | None = None


class LiveQuotesBody(WireModel):
    """`POST /api/itinerary/{id}/quotes/live` 的请求体（L14 按需实时价）。

    出发地/目的地不由前端指定：一律取行程自身的（origin_city + city），避免
    "给别人的行程查我的出发地"。日期可覆盖——默认用行程的出发/返程日，用户
    想比较别的时间窗时可以显式给。
    """

    startDate: date | None = None
    endDate: date | None = None
    limit: int | None = None


class ItemUpsertRequest(BaseModel):
    """字段名与 Java `ItemUpsertRequest` 一致（camelCase，前端已在发这个形状）。"""

    dayId: int | None = None
    itemType: str | None = None
    poiName: str | None = None
    poiId: str | None = None
    address: str | None = None
    latitude: Decimal | None = None
    longitude: Decimal | None = None
    startTime: time | None = None
    endTime: time | None = None
    durationMin: int | None = None
    cost: Decimal | None = None
    tag: str | None = None
    remark: str | None = None
    openTime: str | None = None
    imageUrl: str | None = None
    source: str | None = None
    sourceUpdatedAt: datetime | None = None
    verificationStatus: str | None = None
    valueKind: str | None = None
    freshnessStatus: str | None = None
    reviewRequirement: str | None = None
    factEvidenceJson: str | None = None


class HotelOptionRequest(BaseModel):
    """字段名与 Java `HotelOptionApplyRequest` 一致（前端按 camelCase 发）。"""

    hotelName: str | None = None
    tier: str | None = None
    roomType: str | None = None
    dayNos: list[int] | None = None
    actionMessageId: int | None = None
    baseRevision: str | None = None


class PreferenceSignalsBody(BaseModel):
    explicitPreferences: list[str] | None = None
    hardConstraints: list[str] | None = None
    negativePreferences: list[str] | None = None
    source: str | None = None
    confidence: float | None = None


class VersionBody(BaseModel):
    operation: str = "snapshot"
    summary: str = "行程快照"


class OptimizeDayBody(BaseModel):
    dayId: int | None = None


class UpdateDayBody(BaseModel):
    theme: str | None = None


class NlEditBody(BaseModel):
    instruction: str = ""


class ApplyPlansBody(BaseModel):
    # `plans` 只为兼容既有前端 body 形状而存在：服务端**不使用**它，
    # 落库内容一律取自 actionMessageId 指向的草稿（同 Java 门面的有意丢弃）。
    plans: list[dict[str, Any]] | None = None
    actionMessageId: int | None = None
    baseRevision: str | None = None


class ChatEditBody(BaseModel):
    """对话改行程入参。

    `message` 这里**故意不做非空校验**：Java 的 controller 是 `getOrDefault("message","")`，
    空消息一路走到 agent 才被判无效、再由网关映射成 `502 行程助手暂不可用`。
    在这层加 min_length 会把同一请求的响应从 502 变成 400，属于改契约不改 bug。
    """

    message: str = ""
    history: list[dict[str, Any]] = []


class CoverBody(BaseModel):
    source: str
    unsplashId: str | None = None


class FavoriteBody(BaseModel):
    favorite: bool


class ArchiveBody(BaseModel):
    archived: bool


class ShareCreateBody(BaseModel):
    expireDays: int | None = None
