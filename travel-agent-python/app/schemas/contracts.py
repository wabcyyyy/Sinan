"""契约组注册表与导出逻辑（G-1.1 契约单一源扩展）。

职责：
- CONTRACT_GROUPS：跨端契约的唯一取数点——每个组列出该端面的全部请求/响应模型，
  scripts/export_contracts.py 据此导出 contracts/<group>.schema.json 与前端 dts；
  新增端点模型 = 在这里登记一行，导出物与 drift 门禁自动覆盖；
- wire JSON Schema 的公共整形（strip descriptions / 全属性 required）：
  自 stream_events 就地提级为共享实现，事件组与 API 组用同一套 wire 语义。

wire 语义（与 stream_events.export_schema 一致）：
- description 只服务 Python 可读性，导出前剥离，避免注释改动触发契约漂移；
- 模型 dump 必然携带全部键（可空值以 null 出现），required 提升为全部 properties——
  仅按 pydantic「构造必填」导出时，可选字段改名不会触发校验。

依赖：
- app.schemas 各模型模块；pydantic。禁止 import app.services/app.api（防环 + 保层向）。
"""

from pydantic import BaseModel

from app.schemas.agent_ops import (
    ButlerNoteRequest,
    ButlerNoteResponse,
    CityGuideRequest,
    CityGuideResponse,
    CityGuideSuggestion,
    PoiIntrosRequest,
    PoiIntrosResponse,
    PoiNearbyItem,
    PoiNearbyRequest,
    PoiNearbyResponse,
)
from app.schemas.business.auth import LoginBody, LoginData, RegisterBody, UserInfoVO
from app.schemas.business.image_intent import ImageIntentVO
from app.schemas.business.itinerary import (
    ApplyPlansBody,
    ArchiveBody,
    ChatEditBody,
    CoverBody,
    FavoriteBody,
    GenerateTripRequest,
    HotelOptionRequest,
    ItemUpsertRequest,
    LiveQuotesBody,
    NlEditBody,
    OptimizeDayBody,
    PreferenceSignalsBody,
    ShareCreateBody,
    UpdateDayBody,
    VersionBody,
)
from app.schemas.business.llm_gateway import (
    LlmGatewayCreateBody,
    LlmGatewayTestVO,
    LlmGatewayUpdateBody,
    LlmGatewayVO,
)
from app.schemas.business.speech_intent import SpeechIntentVO
from app.schemas.client_errors import ClientErrorReport
from app.schemas.collaboration import (
    InvitationAcceptRequest,
    InvitationAcceptVO,
    InvitationCreate,
    InvitationCreatedVO,
    InvitationListVO,
    InvitationVO,
    MemberListVO,
    MemberRoleUpdate,
    MemberVO,
)
from app.schemas.expense import (
    ExpenseCategoryTotal,
    ExpenseCreate,
    ExpenseListVO,
    ExpenseUpdate,
    ExpenseVO,
)
from app.schemas.feedback import FeedbackCreate, FeedbackListVO, FeedbackVO
from app.schemas.template import (
    TemplateCardVO,
    TemplateCostTier,
    TemplateDayVO,
    TemplateDetailVO,
    TemplateForkVO,
    TemplateItemVO,
    TemplateListVO,
    TemplatePublishVO,
    TemplateSummaryVO,
)
from app.schemas.trip import (
    AdjustRequest,
    AdjustResponse,
    ChatTurnRequest,
    ChatTurnResponse,
    ClarifyRequest,
    ClarifyResponse,
    EditOp,
    EditOpRequest,
    FlightQuote,
    GenerateDayRequest,
    GenerateRequest,
    GenerateResponse,
    HotelOption,
    HotelRoomOption,
    LocalReplanRequest,
    PlanContextRequest,
)
from app.schemas.weather import WeatherDay, WeatherVO

#: 契约组 → 该端面的全部线级模型（声明顺序即导出顺序；嵌套模型无需重复登记，
#: 由 $defs 递归收集）。
CONTRACT_GROUPS: dict[str, tuple[type[BaseModel], ...]] = {
    # Agent 面（/api/agent/v1/**）：生成/对话/辅助端点的请求与响应模型
    "agent_api": (
        GenerateRequest,
        GenerateResponse,
        GenerateDayRequest,
        AdjustRequest,
        AdjustResponse,
        ClarifyRequest,
        ClarifyResponse,
        EditOpRequest,
        EditOp,
        PlanContextRequest,
        LocalReplanRequest,
        ChatTurnRequest,
        ChatTurnResponse,
        HotelOption,
        HotelRoomOption,
        FlightQuote,
        ButlerNoteRequest,
        ButlerNoteResponse,
        PoiIntrosRequest,
        PoiIntrosResponse,
        PoiNearbyRequest,
        PoiNearbyItem,
        PoiNearbyResponse,
        CityGuideRequest,
        CityGuideSuggestion,
        CityGuideResponse,
    ),
    # 业务面核心端点（/api/auth/**、/api/itinerary/** 写路径，G-1.1 取舍范围）
    "business_api": (
        LoginBody,
        RegisterBody,
        UserInfoVO,
        LoginData,
        GenerateTripRequest,
        ItemUpsertRequest,
        HotelOptionRequest,
        LiveQuotesBody,
        PreferenceSignalsBody,
        VersionBody,
        OptimizeDayBody,
        UpdateDayBody,
        NlEditBody,
        ApplyPlansBody,
        ChatEditBody,
        CoverBody,
        FavoriteBody,
        ArchiveBody,
        ShareCreateBody,
        ExpenseCreate,
        ExpenseUpdate,
        ExpenseVO,
        ExpenseCategoryTotal,
        ExpenseListVO,
        InvitationCreate,
        InvitationVO,
        InvitationCreatedVO,
        InvitationListVO,
        MemberVO,
        MemberListVO,
        MemberRoleUpdate,
        InvitationAcceptRequest,
        InvitationAcceptVO,
        FeedbackCreate,
        FeedbackVO,
        FeedbackListVO,
        TemplateCostTier,
        TemplateItemVO,
        TemplateDayVO,
        TemplateSummaryVO,
        TemplatePublishVO,
        TemplateCardVO,
        TemplateDetailVO,
        TemplateListVO,
        TemplateForkVO,
        WeatherDay,
        WeatherVO,
        ClientErrorReport,
        LlmGatewayCreateBody,
        LlmGatewayUpdateBody,
        LlmGatewayVO,
        LlmGatewayTestVO,
        ImageIntentVO,
        SpeechIntentVO,
    ),
}

#: 事件组（stream_events）不在 CONTRACT_GROUPS：它有独立导出函数 export_schema()，
#: 事件是判别联合（oneOf + const），与 API 组的 $defs 库形态不同。

_NAME_MAPS = frozenset({"properties", "patternProperties", "$defs", "definitions"})


def strip_descriptions(node: object) -> None:
    if isinstance(node, dict):
        for key in list(node.keys()):
            if key == "description":
                del node[key]
                continue
            value = node[key]
            if key in _NAME_MAPS and isinstance(value, dict):
                for sub in value.values():
                    strip_descriptions(sub)
            else:
                strip_descriptions(value)
    elif isinstance(node, list):
        for item in node:
            strip_descriptions(item)


def mark_all_properties_required(node: object) -> None:
    """wire 语义：模型 dump 必然携带全部键（可空值以 null 出现）。

    Pydantic 的 required 表达「构造必填」，弱于 wire 事实——仅按它导出时，
    可选字段改名不会触发校验，消费侧仍会静默落 null。这里把每个对象
    schema 的 required 提升为全部 properties。
    """
    if isinstance(node, dict):
        props = node.get("properties")
        if isinstance(props, dict) and props:
            node["required"] = list(props)
        for key, value in node.items():
            if key in _NAME_MAPS and isinstance(value, dict):
                for sub in value.values():
                    mark_all_properties_required(sub)
            else:
                mark_all_properties_required(value)
    elif isinstance(node, list):
        for item in node:
            mark_all_properties_required(item)


def group_json_schema(group: str) -> dict:
    """导出一个契约组的 JSON Schema（$defs 库形态，顺序确定可做字节比对）。"""
    definitions = models_json_schema_definitions(CONTRACT_GROUPS[group])
    schema: dict = {"$schema": "https://json-schema.org/draft/2020-12/schema", "$defs": definitions}
    strip_descriptions(schema)
    mark_all_properties_required(schema)
    return schema


def models_json_schema_definitions(models: tuple[type[BaseModel], ...]) -> dict:
    """收集一组模型的 $defs（含递归嵌套），键为模型名。

    逐模型生成后合并：同一嵌套模型（如 TripItem 被多个请求/响应引用）内容
    必然一致，setdefault 只保留首份；声明顺序即导出顺序（字节比对依赖）。
    """
    definitions: dict = {}
    for model in models:
        schema = model.model_json_schema(by_alias=True, ref_template="#/$defs/{model}")
        for name, nested in schema.pop("$defs", {}).items():
            definitions.setdefault(name, nested)
        definitions[model.__name__] = schema
    return definitions
