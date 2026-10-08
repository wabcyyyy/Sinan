"""行程相关业务模型（请求/响应 Schema）。

职责：
- 定义生成、调整、澄清、编辑、对话、酒店等接口的入参与返回结构。

实现要点：
- 全部继承 WireModel，自动支持 camelCase 序列化；
- GenerateRequest/GenerateResponse 描述整段行程生成；
  ChatTurnRequest/ChatTurnResponse 承载对话式草稿编辑；
  EditOp 描述结构化编辑操作（delete/add/update_time/move_day/upgrade_hotel）；
- HotelOption/HotelRoomOption 封装酒店候选与预算判定结果。

依赖：
- app.schemas.common.WireModel。
"""

from typing import Annotated, Literal, Self

from pydantic import BeforeValidator, Field, model_validator

from app.schemas.common import WireModel
from app.schemas.trip_requirements import (
    MAX_TRIP_DAYS,  # 常量真源在 trip_requirements（M1a），trip.py 引用并保持既有导入路径可用
    IntakeState,
    TripRequirements,
)


def _clip_text(limit: int):
    """生成「超长静默截断」校验器（M3-① 契约叙事化 AD5）。

    为什么不用裸 Field(max_length=...) 直接抛错：叙事字段由 LLM 产出，
    模型偶尔无视长度约束；旧持久化数据也可能超长。若在校验层抛错，
    一条超长文案会让整份行程（乃至整日生成）校验失败。这里统一
    「先截断、后声明 max_length」——max_length 表达契约意图，
    BeforeValidator 保证超限输入被裁剪而不是拒绝（旧数据兼容）。
    """

    def _clip(value: object) -> object:
        if isinstance(value, str) and len(value) > limit:
            return value[:limit]
        return value

    return _clip


# 叙事文本类型（截断 + max_length 兜底）：40=主题句级，60=名称级，120=句子级。
# Opt 后缀为可空版本。字数与 prompt 契约（open_generation）一一对照。
_Str40 = Annotated[str, Field(max_length=40)]
_Str60 = Annotated[str, Field(max_length=60)]
_Str120 = Annotated[str, Field(max_length=120)]
Clip40 = Annotated[_Str40, BeforeValidator(_clip_text(40))]
Clip60 = Annotated[_Str60, BeforeValidator(_clip_text(60))]
Clip120 = Annotated[_Str120, BeforeValidator(_clip_text(120))]
# 可空版本：max_length 必须挂在 union 的 str 成员上——直接对 str|None 整体
# 加 Field(max_length) 会让 None 也进 max_length 校验而 TypeError。
Clip40Opt = Annotated[Clip40 | None, BeforeValidator(_clip_text(40))]
Clip120Opt = Annotated[Clip120 | None, BeforeValidator(_clip_text(120))]


class FactEvidence(WireModel):
    """单个事实字段的来源与时效信息。

    状态按字段记录，避免把坐标已核实误解成价格、营业时间也已核实。
    """

    source_ref: str | None = None
    source_url: str | None = None
    provider: str | None = None
    retrieved_at: str | None = None
    expires_at: str | None = None
    verification_status: Literal["verified", "partially_verified", "unverified"] = "unverified"
    value_kind: Literal["observed", "estimated", "generated"] = "generated"
    freshness_status: Literal["fresh", "stale", "unknown"] = "unknown"
    review_requirement: Literal["none", "before_departure"] = "before_departure"


class SourceRecord(WireModel):
    """可追溯来源记录；storage_source 不等同于原始发布者。"""

    source_id: str
    storage_source: str | None = None
    provider: str | None = None
    publisher: str | None = None
    source_url: str | None = None
    retrieved_at: str | None = None
    published_at: str | None = None
    expires_at: str | None = None


class QualityIssue(WireModel):
    code: str
    path: str | None = None
    message: str


class QualityReport(WireModel):
    quality_status: Literal["DRAFT", "READY_WITH_WARNINGS", "READY", "BLOCKED", "STALE"] = "DRAFT"
    quality_rule_version: str = "travel-quality-1.0"
    validated_at: str | None = None
    blocking_issues: list[QualityIssue] = Field(default_factory=list)
    warnings: list[QualityIssue] = Field(default_factory=list)
    metrics: dict[str, float] = Field(default_factory=dict)


class GenerateRequest(WireModel):
    city: str = Field(min_length=1, max_length=64)
    days: int = Field(default=1, ge=1, le=MAX_TRIP_DAYS)
    persons: int = Field(default=1, ge=1, le=20)
    budget: float | None = None
    start_date: str | None = None
    preferences: list[str] = Field(default_factory=list)
    hotel_tier: str | None = Field(default=None)  # 经济型/舒适型/高档型/豪华型/奢华型
    requirements: str | None = Field(default=None, max_length=4000)  # 客户额外要求（自然语言）
    # 用户一句话旅行意图（M1 意图贯通的一等字段）。用户输入建议 ≤800 字
    # （前端 maxlength 限制）；Java 兜底 intent=requirements 时可达 4000，
    # 故线级上限与 requirements 对齐。
    intent: str | None = Field(default=None, max_length=4000)
    # 用户最初输入的省/区域提示（如"云南"→city 已解析为"丽江"）。此前 Java
    # 会发送但 pydantic 默认 ignore extra 直接丢弃；保留以便开放模式提示模型
    # 目的地所属区域，并为管家讲解提供上下文。
    region_hint: str | None = Field(default=None, max_length=64)
    # 可选出发地（城市名，L13 契约先行；L14 接线进 agent state）。**有 origin
    # 才查航班**——未填/未映射 IATA 时航段整段如实缺席，不冒充。
    origin_city: str | None = Field(default=None, max_length=64)
    # 结构化需求（M1a 单一真源）：只记录现有参数未覆盖的语义（窗口/必去/排除/
    # 节奏/交通/预算口径/住宿），与自然语言 requirements 原话并存，语义以本字段为准
    requirements_struct: TripRequirements | None = None


class TripItem(WireModel):
    item_type: str = Field(default="attraction", pattern="^(attraction|food|hotel|transport)$")
    poi_name: str
    poi_id: str | None = None
    address: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    start_time: str | None = None
    end_time: str | None = None
    duration_min: int | None = None
    open_time: str | None = None
    cost: float | None = None
    tag: str | None = None
    remark: str | None = None
    # 入选理由（≤120 字，M3-① 叙事层字段）：attraction 必填，讲该点与本趟
    # 意图的具体关系。why_this 属于 value_kind="generated" 的叙事层文案，
    # 只解释「为什么来」，不代表 POI 事实（坐标/价格/营业时间）被核实，
    # 不影响 fact_evidence 的核验状态。
    why_this: Clip120Opt = None
    image: str | None = None  # POI 图片 URL（高德检索，可选）
    source: str | None = None
    source_updated_at: str | None = None
    verification_status: Literal["verified", "partially_verified", "unverified"] = "unverified"
    value_kind: Literal["observed", "estimated", "generated"] = "generated"
    freshness_status: Literal["fresh", "stale", "unknown"] = "unknown"
    review_requirement: Literal["none", "before_departure"] = "before_departure"
    fact_evidence: dict[str, FactEvidence] = Field(default_factory=dict)


class Suggestion(WireModel):
    """备选池条目：生成时未排入行程、可在「发现更多」一键加入的候选点位。

    候选池保底链路中条目必须来自候选池（代码侧兜底校验）；开放模式
    （open_research）下允许池外模型建议，坐标留空由前端经地图检索补齐。
    """

    poi_id: str | None = None
    name: str
    # shopping 为产品语义（商城/名店）；souvenir 兼容历史数据
    category: Literal["attraction", "activity", "food", "hotel", "shopping", "souvenir"] = "attraction"
    address: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    intro: str | None = None
    need_reservation: bool = False
    estimated_cost: float | None = None
    used: bool = False

    @model_validator(mode="after")
    def _strip_shopping_cost(self) -> Self:
        # 购物类不估价：花多少取决于用户自己买什么，固定「人均 ¥5000」
        # 只会削弱可信度（前端展示「按店内消费为准」）。契约层统一兜底，
        # 覆盖候选池/联网补齐/模型建议等所有组装路径。
        if self.category in ("shopping", "souvenir") and self.estimated_cost is not None:
            self.estimated_cost = None
        return self


class PhotoSpot(WireModel):
    """出片点位（M3-① 叙事层）：名称 + 拍摄建议 + 最佳时段。

    纯叙事建议，不参与事实核验；每日 0-4 个，意图为空时模型可整体省略。
    """

    name: Clip60
    tip: Clip120Opt = None
    best_time: Clip40Opt = None


class BackupRule(WireModel):
    """备用方案规则（M3-① 叙事层）：if 触发条件 → action 替换动作。

    wire 键为 "if"（显式别名，覆盖 to_camel 生成器，因为 if_ 的 camel
    形态没有意义）；每日 0-3 条。
    """

    if_: Clip120 = Field(alias="if")
    action: Clip120

    @model_validator(mode="before")
    @classmethod
    def _forward_compat(cls, data: object) -> object:
        """旧输入前向兼容：补齐缺失 key，避免整份行程校验失败。

        历史数据/模型输出可能出现三种形态：wire 键 "if"、python 键 "if_"、
        以及两者皆缺的残缺 dict（如旧版 {"name": ...}）。缺 key 一律补
        空串而不是抛错——备用方案是辅助叙事，不能因为一条脏规则毁掉
        整日行程（AD4 降级精神）。
        """
        if isinstance(data, dict):
            row = dict(data)
            if row.get("if_") is None and row.get("if") is not None:
                row["if_"] = row["if"]
            row.setdefault("if_", "")
            row.setdefault("action", "")
            return row
        return data


class DayOption(WireModel):
    """当日可选方案（M3-① 叙事层）：方案名 + 概述 + 取舍说明。

    每日 0-2 组，仅当天存在值得取舍的分叉（如「暴走版 vs 休闲版」）时输出；
    意图为空时模型可整体省略。items 为该方案对应的点位引用（开放结构，
    M3 仅承接不消费）。
    """

    label: Clip40
    summary: Clip120
    tradeoff: Clip120
    items: list = Field(default_factory=list)


class DailyPlan(WireModel):
    day_no: int
    note: str | None = None
    items: list[TripItem] = Field(default_factory=list)
    # 当日主题：叙事句语义（≤40 字）——一句说清「当天怎么玩」的主线，
    # 禁止「A→B→C」纯路径串（M3-① 契约 v1.1.narrative 硬约束）。
    theme: Clip40Opt = None
    mini_route: dict = Field(default_factory=dict)
    backup_plan: list[BackupRule] = Field(default_factory=list)
    photo_spots: list[PhotoSpot] = Field(default_factory=list)
    practical_notes: list[str] = Field(default_factory=list)
    # 当日可选方案（0-2 组）：叙事层，见 DayOption。
    day_options: list[DayOption] = Field(default_factory=list)
    # 整趟主题标题（≤40 字）：由第 1 天（open_day day_no==1 或 open_trip
    # 顶层）生成后随行程透传，第 2 天起模型省略该字段。
    trip_theme: Clip40Opt = None
    suggestions: list[Suggestion] = Field(default_factory=list)


class GenerateResponse(WireModel):
    schema_version: str = "1.0"
    city: str
    days: int
    title: str
    # 整趟主题标题（≤40 字）：承接第 1 天 daily_plans[0].trip_theme，
    # 供前端整段行程页头部展示（M3-① 叙事层）。
    trip_theme: Clip40Opt = None
    daily_plans: list[DailyPlan]
    budget_estimate: dict[str, float] = Field(default_factory=dict)
    suggestions: list[Suggestion] = Field(default_factory=list)
    validation_log: list[str] = Field(default_factory=list)
    price_note: str | None = None
    schedule_report: dict = Field(default_factory=dict)
    critic_report: dict = Field(default_factory=dict)
    destination_status: Literal["knowledge_backed", "researched", "draft_only"] = "knowledge_backed"
    sources: list[SourceRecord] = Field(default_factory=list)
    quality_report: QualityReport = Field(default_factory=QualityReport)
    status: Literal["success", "degraded", "failed"] = "success"
    status_reason: str | None = None


class AdjustRequest(WireModel):
    city: str = Field(min_length=1, max_length=64)
    item_type: str = Field(default="attraction", pattern="^(attraction|food|hotel|transport)$")
    poi_name: str = Field(min_length=1, max_length=128)
    preferences: list[str] = Field(default_factory=list)


class PoiOption(WireModel):
    poi_id: int | None = None
    name: str
    category: str | None = None
    address: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    ticket_price: float | None = None
    duration_min: int | None = None
    open_time: str | None = None
    tags: str | None = None
    rating: float | None = None
    description: str | None = None


class AdjustResponse(WireModel):
    city: str
    current: str
    recommendations: list[PoiOption] = Field(default_factory=list)


class ClarifyRequest(WireModel):
    # 上限对齐 ChatTurnRequest.message=2000（审计 §3.3.5）：message 原样拼进 clarify
    # prompt，无上限时配额内单次可塞数百 KB 文本；一句话意图收集用不到 2000 之外的空间。
    message: str = Field(min_length=1, max_length=2000)
    # slots 是 state 的兼容投影（老前端继续随轮回传）；state 传入时以 state 为权威
    slots: dict = Field(default_factory=dict)
    # 累计收集状态（M1a）：服务端权威合并 patch 后随响应返回完整规范化状态，
    # 客户端不再重复做语义合并；未传时服务端从 slots 播种
    state: IntakeState | None = None


class ClarifyResponse(WireModel):
    # state 是权威完整规范化状态；slots 为其兼容投影（老前端只读 slots 不破坏）
    state: IntakeState
    slots: dict = Field(default_factory=dict)
    missing: list[str] = Field(default_factory=list)
    question: str | None = None
    ready: bool = False
    # 快捷回复选项：点选即以该文本继续对话（对话内嵌澄清卡），旧客户端可忽略
    options: list[str] = Field(default_factory=list)
    # 天数超上限等不可就绪的协商态：恒为 ready=False，前端不得据此触发生成
    blocked: bool = False
    # AILIVE-2：本轮 LLM 抽取失败（调用失败或输出不可解析）——槽位零进展且服务降级，
    # question 已换成如实告知的兜底话术，前端据此提示而非无标记地继续追问
    degraded: bool = False


class EditOpRequest(WireModel):
    city: str = Field(min_length=1, max_length=64)
    days: int = Field(ge=1, le=MAX_TRIP_DAYS)
    plans: list[dict] = Field(default_factory=list, max_length=MAX_TRIP_DAYS)
    instruction: str = Field(min_length=1, max_length=2000)


class EditOp(WireModel):
    action: str  # delete / add / update_time / move_day / upgrade_hotel
    day_no: int | None = None
    poi_name: str | None = None
    start_time: str | None = None
    tier: str | None = None  # upgrade_hotel 用的档次


class PlanContextRequest(WireModel):
    city: str = Field(min_length=1)
    preferences: list[str] = Field(default_factory=list)
    # 行程会话 ID（wire 名 itineraryId）：用于进度事件发布到对应 Redis 通道；
    # 可选兼容旧调用方，缺失时 Python 侧不发布任何事件。
    itinerary_id: int | None = None


class GenerateDayRequest(WireModel):
    city: str = Field(min_length=1, max_length=64)
    persons: int = 1
    budget: float | None = None
    start_date: str | None = None
    day_no: int = Field(default=1, ge=1, le=MAX_TRIP_DAYS)
    days: int | None = None  # 整个行程总天数，用于按总时长调整当日节奏；不传则按单日处理
    # BIZ-2 独立重试信号：单日链（逐日兜底/终检重生成）为 True，重试预算取
    # MAX_*，不再从「days>1 ⇒ 整段链 ⇒ 只试 1 次」推断——此前这个推断让单日链
    # 为保重试不敢传 days，预算分档被放大成整天数倍（¥7000/天 奢华档）。
    per_day_chain: bool = False
    # used_names/context 会整体进 prompt 并被 ReferencePool 展开；无上限时
    # 超大 body 直接吃内存与 token。context 由服务端 plan-context 生成，
    # 条目数受参考资料池上限约束，这里给宽松但有限的边界。
    used_names: list[str] = Field(default_factory=list, max_length=200)
    hotel_tier: str | None = None
    preferences: list[str] = Field(default_factory=list, max_length=20)
    chosen_hotel: str | None = None
    needs_hotel: bool = True
    requirements: str | None = Field(default=None, max_length=4000)  # 客户额外要求（自然语言）
    # 结构化需求（M1a）：与 GenerateRequest.requirements_struct 同义
    requirements_struct: TripRequirements | None = None
    # 用户一句话旅行意图（M1 意图贯通）：上限与 requirements 对齐 4000
    # （Java 兜底 intent=requirements 时可达 4000）。
    intent: str | None = Field(default=None, max_length=4000)
    region_hint: str | None = Field(default=None, max_length=64)  # 用户最初输入的省/区域
    feedback: str = Field(default="", max_length=4000)
    context: dict = Field(default_factory=dict)
    request_id: str | None = Field(default=None, max_length=128)
    action_id: str | None = Field(default=None, max_length=128)


class LocalReplanRequest(WireModel):
    """只对受影响日期做重规划的显式输入契约。"""

    city: str = Field(min_length=1, max_length=64)
    affected_day_nos: list[int] = Field(min_length=1, max_length=MAX_TRIP_DAYS)
    locked_names: list[str] = Field(default_factory=list, max_length=100)
    candidate_names: list[str] = Field(default_factory=list, max_length=100)
    failure_reasons: list[str] = Field(default_factory=list, max_length=20)
    plans: list[dict] = Field(default_factory=list, max_length=MAX_TRIP_DAYS)
    budget: float | None = None
    # 实际已花费（C3.4 透传，与 budget 同币种）：由外部调用方投喂，服务端不读库；
    # 与 budget 同时提供时以剩余预算（budget - spent）作为重排的预算约束。
    spent: float | None = None
    # 结构化需求（M1a）：重规划时随请求携带，约束受影响日的重排
    requirements_struct: TripRequirements | None = None
    request_id: str | None = Field(default=None, max_length=128)
    action_id: str | None = Field(default=None, max_length=128)


class ChatTurnRequest(WireModel):
    city: str = Field(min_length=1, max_length=64)
    days: int = Field(ge=1, le=MAX_TRIP_DAYS)
    persons: int = Field(default=1, ge=1, le=20)
    budget: float | None = None
    current_total: float | None = None
    current_hotel_total: float | None = None
    # 实际花费聚合（C3.4）：只统计与预算同币种（CNY）的账目，跨币种不换算不相加；
    # 其他币种只列币种码供模型提示用户。与 current_total（计划口径）语义无关。
    spent_total: float | None = None
    spent_by_category: dict[str, float] | None = None
    spent_other_currencies: list[str] | None = None
    start_date: str | None = None
    end_date: str | None = None
    preferences: list[str] = Field(default_factory=list, max_length=20)
    hotel_tier: str | None = None
    plans: list[dict] = Field(default_factory=list, max_length=MAX_TRIP_DAYS)
    history: list[dict] = Field(default_factory=list, max_length=20)
    message: str = Field(min_length=1, max_length=2000)
    # 结构化需求（M1a）：对话编辑上下文里的硬约束（必去/排除/窗口等），
    # 编辑决策不得违反；由调用方从行程存档的需求快照带入
    requirements_struct: TripRequirements | None = None


class HotelRoomOption(WireModel):
    id: str
    room_name: str
    base_price: float
    nightly_price: float
    nights: int
    rooms: int
    total_price: float
    price_delta: float | None = None
    projected_hotel_total: float | None = None
    within_budget: bool
    budget_overage: float = 0
    capacity: int = 2
    bed_type: str | None = None
    breakfast: str | None = None
    description: str | None = None
    is_default: bool = False
    nightly_breakdown: list[dict] = Field(default_factory=list)


class HotelOption(WireModel):
    id: str
    hotel_name: str
    tier: str
    address: str | None = None
    rating: float | None = None
    base_price: float
    season_factor: float
    season_label: str
    nightly_price: float
    nights: int
    rooms: int
    total_price: float
    price_delta: float | None = None
    within_budget: bool
    budget_capacity: float | None = None
    budget_overage: float = 0
    is_current: bool = False
    reason: str
    requested_nights: int = 1
    requested_day_nos: list[int] = Field(default_factory=list)
    available_day_nos: list[int] = Field(default_factory=list)
    room_types: list[HotelRoomOption] = Field(default_factory=list)
    # 观测价证据（L13 契约先行；L15 组装）：Hotellook 观测价命中时填 observed 票，
    # 估价链路保持 None。完全复用 FactEvidence 单一真源，不发明平铺第二套。
    # LA2 后数据层不再产出 observed 票（端点已死），字段保留给「查实时价」按需
    # 通道（google_hotels）经 L16 接线后复用，契约形状不变。
    price_fact: FactEvidence | None = None
    # 核实深链（LA2）：Hotellook 深链已死，改挂地图搜索出口（国内高德/海外谷歌，
    # map_link 统一口径）——"估价 + 出发前核实"叙事的出口，L16 接前端呈现。
    search_link: str | None = None


class FlightQuote(WireModel):
    """一条航班报价（observed 事实票；L13 契约先行，L14 组装消费）。

    全字段 optional/带默认——旧客户端零破坏，组装方按可得性如实填。
    时效字段（retrieved_at/expires_at）对齐 FactEvidence 口径：聚合缓存价不是
    实时价，**消费方必须把 expires 收紧到 ≤24h** 并呈现观测时点（叙事是
    「出发前核实」而非「实时」）。往返 = 去程/返程两条本模型，不另造往返容器。
    """

    origin_iata: str | None = None
    destination_iata: str | None = None
    origin_city: str | None = None
    destination_city: str | None = None
    depart_date: str | None = None
    return_date: str | None = None
    airline: str | None = None
    flight_number: str | None = None
    transfers: int | None = None
    price: float | None = None
    currency: str | None = None
    #: 数据提供方（如 aviasales / google_flights）；深链与 provider 对应
    provider: str | None = None
    source_url: str | None = None
    retrieved_at: str | None = None
    expires_at: str | None = None
    deep_link: str | None = None


class ChatTurnResponse(WireModel):
    reply: str
    plans: list[dict] = Field(default_factory=list)
    changed: bool = False
    hotel_options: list[HotelOption] = Field(default_factory=list)
    requires_confirmation: bool = False
    plan_document: dict | None = None
    operations: list[dict] = Field(default_factory=list)
    pending_action: dict | None = None
