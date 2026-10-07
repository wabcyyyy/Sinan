"""辅助型 Agent 端点的线级契约（M0 契约先行）。

职责：
- 为 /v1/butler-note、/v1/poi-intros、/v1/poi-nearby、/v1/city-guide 四个原先收裸
  dict 的路由提供请求/响应模型，非法 payload 在线级即被 422 拒绝；
- 响应模型只做结构化包装：wire 键名、取值与降级路径与改造前保持一致。

实现要点：
- 继承 WireModel：wire 层 camelCase、内部 snake_case，两种键都能收（populate_by_name）；
  Java 侧（AgentButlerNoteRequest 等 DTO）发 camelCase，历史前端透传可能是 snake_case；
- Pydantic 默认 lax 模式：字符串数字（"39.9"）可转 float，兼容前端透传，不加 strict；
- LLM 直通的响应字段（intros 值、city-guide 的 city）用 before 校验器兜底转 str：
  改造前这些值原样透传，若模型偶发非字符串输出，严格类型会把原本 200 的响应打成 500；
- poi-nearby 行结构由 app/agent/tools.find_nearby_pois 构造（原生 str/float/int/None），
  PoiNearbyItem extra="allow" 保留未知键（经纬度、票价等），_distance_m 用显式
  serialization_alias 固定 wire 键（Java @JsonProperty 依赖 "_distance_m"）。

依赖：
- app.schemas.common.WireModel；pydantic。
"""

from pydantic import Field, field_validator

from app.schemas.common import WireModel


class ButlerNoteRequest(WireModel):
    """管家讲解请求；字段与 run_butler_note（app/agent/generation/content/butler.py）消费的键逐一对齐。

    兼容性说明：
    - preferences：Java 侧传字符串（原始偏好串），历史行为 str/list 都接受，原值进 prompt；
    - budget：Java 可能传数字（BigDecimal）或字符串，保持原值进 prompt 不做归一化；
    - intent：为 M1 预留的线级字段（可选，M1 才接入 prompt）。
    """

    city: str | None = None
    days: int | None = None
    persons: int | None = None
    preferences: str | list[str] | None = None
    hotel_tier: str | None = None
    region_hint: str | None = None
    budget: str | float | int | None = None
    requirements: str | None = None
    plans: list[dict] | None = None  # 元素形如 {"day_no": 1, "items": ["名称", ...]}
    validation_log: list | None = None
    # 上限与 requirements 对齐 4000：Java 无 intent 时兜底 intent=requirements，
    # 但例 payload 会携带长文本，800 会 422 打断管家讲解（M1 意图贯通）。
    intent: str | None = Field(default=None, max_length=4000)


class ButlerNoteResponse(WireModel):
    note: str = ""


class PoiIntrosRequest(WireModel):
    city: str = ""
    names: list[str] = Field(default_factory=list)
    # 上限与 requirements 对齐 4000（Java 兜底 intent=requirements 可达 4000）。
    intent: str | None = Field(default=None, max_length=4000)


class PoiIntrosResponse(WireModel):
    intros: dict[str, str] = Field(default_factory=dict)

    @field_validator("intros", mode="before")
    @classmethod
    def _coerce_intros_values(cls, v):
        """LLM 输出的介绍值兜底转 str：改造前为原样透传，避免偶发非字符串值打成 500。"""
        if isinstance(v, dict):
            return {str(k): (x if isinstance(x, str) else str(x)) for k, x in v.items()}
        return v


class PoiNearbyRequest(WireModel):
    city: str = ""
    name: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    limit: int | None = None
    radius_m: int | None = None
    category: str | None = None


class PoiNearbyItem(WireModel):
    """近邻 POI 行；行结构见 app/agent/tools.find_nearby_pois（原生类型）。

    extra="allow"：不丢未知键（经纬度、票价等），序列化时原键保留。
    distance_m 的 wire 键必须是 "_distance_m"（Java @JsonProperty 依赖），
    显式 serialization_alias 覆盖 to_camel 生成器（已验证）。
    """

    model_config = WireModel.model_config.copy()
    model_config["extra"] = "allow"

    name: str = ""
    category: str | None = None
    rating: float | None = None
    address: str | None = None
    distance_m: int | None = Field(default=None, alias="_distance_m", serialization_alias="_distance_m")


class PoiNearbyResponse(WireModel):
    items: list[PoiNearbyItem] = Field(default_factory=list)


class CityGuideRequest(WireModel):
    """城市引导请求；run_city_guide 读 "input" 键，wire 键同为 "input"。

    input 是 Python 内置名，字段名用 user_input + 显式 alias="input"；
    显式 alias 优先于 to_camel 生成器（已验证），路由需 model_dump(by_alias=True)。
    """

    # 上限放宽一档到 4000（审计 §3.3.5）：与相邻模型的 requirements/intent 同档——
    # city-guide 是开放式的"说说你想怎么玩"，比 clarify 的一句话意图收得更宽，
    # 但仍须有界（user_input 原样拼进 prompt）。history 由 run_city_guide 组装时
    # 裁到最后 8 条、每条 600 字符，无需在此重复设界。
    user_input: str = Field(default="", alias="input", max_length=4000)
    supported: list[str] = Field(default_factory=list)  # Java 会传；Python 现阶段忽略
    history: list[dict] | None = None  # 元素 {role, content}；保持 dict 让原逻辑工作


class CityGuideSuggestion(WireModel):
    name: str = ""
    reason: str = ""


class CityGuideResponse(WireModel):
    kind: str = "unclear"
    city: str | None = None
    message: str = ""
    suggestions: list[CityGuideSuggestion] = Field(default_factory=list)

    @field_validator("city", mode="before")
    @classmethod
    def _coerce_city(cls, v):
        """run_city_guide 对 city 原样透传；非字符串（模型偶发输出）兜底转 str，避免 500。"""
        if v is None or isinstance(v, str):
            return v
        return str(v)
