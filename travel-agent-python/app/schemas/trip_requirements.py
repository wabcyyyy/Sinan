"""TripRequirements：需求单一真源（M1a，spec 2026-10-08 §5）。

职责：
- 定义"有效旅行需求"中现有请求参数（city/days/persons/budget/startDate/stayNights/
  preferences/hotelTier，各有一份权威值）未覆盖的语义部分：日窗口、必去、排除、
  节奏、交通偏好、预算口径、住宿要求、未决请求。整份有效需求 = 已规范化现有参数
  + TripRequirements，本模块不复制现有参数；
- 定义 clarify 收集阶段的累计状态 IntakeState：生成前的需求状态，不是另一份生成
  请求——days 无上限约束，"想玩 8 天"这类超限原值必须保留并走协商，不能提前 422；
- 确定性校验（界限/一致性/合法性）：生成入口用于拒绝非法请求，收集侧用于
  保留冲突并协商，不静默改写；
- canonical_requirements_payload：指纹用的确定性序列化。

设计边界：
- 排除类别受控词表 SUPPORTED_EXCLUDED_CATEGORIES 不是景点语料库，只在此登记；
- clarify 的有类型 patch（set/unset/add/remove）与其应用在 requirement_patches.py
  （应用器体积自成一体）；
- source_turn_id 追溯需求来自哪轮对话，不是事实证据；
- MAX_TRIP_DAYS 常量真源在本模块（更底层，供 trip.py 与本模块共同引用，避免环）。

依赖：pydantic；无内部依赖（保持 schemas 层零内部 import）。
"""

from typing import Annotated, Any, Literal

from pydantic import Field, StringConstraints

from app.schemas.common import WireModel

MAX_TRIP_DAYS = 7

#: 本版需求结构版本；旧行程缺失时按旧默认规则（空结构 = 无额外约束），不反推用户没说过的约束。
REQUIREMENTS_SCHEMA_VERSION = 1

#: 排除类别受控词表：首期至少支持 museum；不等同 TripItem.item_type，词表外类别
#: 在 patch 应用时拒绝并记入 unresolved_requests，不静默丢弃。
SUPPORTED_EXCLUDED_CATEGORIES: tuple[str, ...] = ("museum",)

_BoundedStr = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=64)]
_FreeText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=400)]
_TimeHhMm = Annotated[str, StringConstraints(pattern=r"^([01]\d|2[0-3]):[0-5]\d$")]


class DayWindow(WireModel):
    """单日时间窗口：kind 决定窗口语义（full=全天、arrival=到达、departure=离开）。"""

    day_no: int = Field(ge=1, le=MAX_TRIP_DAYS)
    kind: Literal["full", "arrival", "departure"] = "full"
    not_before: _TimeHhMm | None = None
    finish_by: _TimeHhMm | None = None
    source_turn_id: str | None = Field(default=None, max_length=64)


class RequiredPlace(WireModel):
    """必去地点约束：day_nonull=全程必去；指定 day_no 属于硬要求（该日必须出现）。"""

    constraint_id: _BoundedStr
    name: _BoundedStr
    day_no: int | None = Field(default=None, ge=1, le=MAX_TRIP_DAYS)
    source_turn_id: str | None = Field(default=None, max_length=64)


class BudgetPolicy(WireModel):
    """预算口径：金额本身仍取现有 budget 字段，不重复保存 total。"""

    mode: Literal["target", "hard_cap"] | None = None
    include_intercity_transport: bool | None = None


class LodgingRequirements(WireModel):
    """住宿要求：夜数仍取现有 stayNights，这里只记录显式性与锁定身份。"""

    rooms: int | None = Field(default=None, ge=1, le=20)
    locked_hotel_identity: str | None = Field(default=None, max_length=128)
    stay_nights_explicit: bool = False


class TripRequirements(WireModel):
    """现有请求参数之外的结构化需求；全默认 = 无额外约束（等价于旧行为）。"""

    schema_version: int = REQUIREMENTS_SCHEMA_VERSION
    day_windows: list[DayWindow] = Field(default_factory=list, max_length=MAX_TRIP_DAYS)
    required_places: list[RequiredPlace] = Field(default_factory=list, max_length=20)
    excluded_places: list[_BoundedStr] = Field(default_factory=list, max_length=20)
    excluded_categories: list[_BoundedStr] = Field(default_factory=list, max_length=10)
    pace: Literal["normal", "relaxed"] | None = None
    transport_preference: Literal["walking", "driving", "mixed", "unspecified"] | None = None
    max_walk_minutes_per_leg: int | None = Field(default=None, gt=0, le=480)
    budget_policy: BudgetPolicy | None = None
    lodging: LodgingRequirements | None = None
    unresolved_requests: list[_FreeText] = Field(default_factory=list, max_length=20)


class IntakeState(WireModel):
    """clarify 收集阶段的累计状态（生成前需求状态，非生成请求）。

    基础参数可空 = 未收集；days 刻意无上限——超限原值保留由出口协商处理，
    生成入口才要求完整、合法参数。negotiations 承载服务端一次性协商闸标记，
    随状态往返实现无状态服务的"只拦一次"。
    """

    schema_version: int = REQUIREMENTS_SCHEMA_VERSION
    city: str | None = Field(default=None, min_length=1, max_length=64)
    days: int | None = Field(default=None, ge=1)
    persons: int | None = Field(default=None, ge=1, le=20)
    budget: float | None = Field(default=None, ge=0)
    start_date: str | None = Field(default=None, max_length=10)
    stay_nights: int | None = Field(default=None, ge=0)
    preferences: list[_BoundedStr] = Field(default_factory=list, max_length=10)
    hotel_tier: str | None = Field(default=None, min_length=1, max_length=16)
    origin_city: str | None = Field(default=None, min_length=1, max_length=64)
    requirements: TripRequirements = Field(default_factory=TripRequirements)
    #: 可选信息（start_date/origin_city/hotel_tier/stay_nights）已问过/待问的槽位名
    optional_asked: list[str] = Field(default_factory=list, max_length=10)
    optional_pending: list[str] = Field(default_factory=list, max_length=10)
    negotiations: dict[str, bool] = Field(default_factory=dict)


def requirements_issues(req: TripRequirements, *, days: int | None = None) -> list[str]:
    """确定性校验，返回问题列表（空 = 合法）。

    生成入口：非空即拒绝（422），生成必须拿到完整合法参数；
    收集侧：非空时保留冲突并协商，不静默改写 day_no 或丢弃请求。
    """
    issues: list[str] = []
    seen_windows: set[int] = set()
    for window in req.day_windows:
        if window.day_no in seen_windows:
            issues.append(f"第 {window.day_no} 天存在重复的时间窗口")
        seen_windows.add(window.day_no)
        if window.not_before and window.finish_by and window.not_before >= window.finish_by:
            issues.append(
                f"第 {window.day_no} 天时间窗口倒置（不早于 {window.not_before} 但须在 {window.finish_by} 前结束）"
            )
        if days is not None and window.day_no > days:
            issues.append(f"时间窗口指向第 {window.day_no} 天，超出行程天数 {days}")
    seen_ids: set[str] = set()
    for place in req.required_places:
        if place.constraint_id in seen_ids:
            issues.append(f"必去地点约束重复：{place.constraint_id}")
        seen_ids.add(place.constraint_id)
        if days is not None and place.day_no is not None and place.day_no > days:
            issues.append(f"必去地点「{place.name}」指定第 {place.day_no} 天，超出行程天数 {days}")
    for category in req.excluded_categories:
        if category not in SUPPORTED_EXCLUDED_CATEGORIES:
            issues.append(f"暂不支持排除类别「{category}」，当前支持：{'、'.join(SUPPORTED_EXCLUDED_CATEGORIES)}")
    return issues


def intake_state_issues(state: IntakeState) -> list[str]:
    """收集状态整体一致性校验（含需求结构 + 基础参数交叉检查）。"""
    issues = requirements_issues(state.requirements, days=state.days)
    if state.days is not None and state.stay_nights is not None and state.stay_nights > state.days:
        issues.append(f"住宿 {state.stay_nights} 晚多于行程 {state.days} 天")
    return issues


def canonical_requirements_payload(req: TripRequirements | None) -> dict[str, Any]:
    """指纹用的确定性需求序列化：默认值全排除（全默认 = 空对象）、集合按语义排序。

    有顺序含义的数组（day_windows 按天）排序后仍保序；required_places 按
    constraint_id 排序消除 LLM 输出顺序抖动；excluded 集合排序。
    """
    if req is None:
        return {}
    payload = req.model_dump(mode="json", by_alias=True, exclude_defaults=True, exclude_none=True)
    payload.pop("schema_version", None)
    if isinstance(payload.get("dayWindows"), list):
        payload["dayWindows"] = sorted(payload["dayWindows"], key=lambda w: w.get("dayNo", 0))
    if isinstance(payload.get("requiredPlaces"), list):
        payload["requiredPlaces"] = sorted(payload["requiredPlaces"], key=lambda p: p.get("constraintId", ""))
    for key in ("excludedPlaces", "excludedCategories", "unresolvedRequests"):
        if isinstance(payload.get(key), list):
            payload[key] = sorted(payload[key])
    return payload
