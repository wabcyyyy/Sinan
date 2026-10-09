"""跨语言流事件契约：generate-stream 的 JSON Lines 事件模型（单一模型源）。

职责：
- 为整段流式生成的既有一组事件（start / day / day_patch / suggestions / done /
  error）与 M5a 增量附加的逐项候选事件（day_item_preview / day_item_preview_withdrawn）
  定义唯一模型；Python 产出点一律「模型构造 → dump」输出 wire dict，
  杜绝手搓键名漂移；
- export_schema() 导出联合 JSON Schema（仓库根 contracts/stream_events.schema.json），
  供 Java 侧运行时校验与 CI 漂移比对。

分域边界（AILIVE-3 口径，2026-10-06）：本契约只覆盖 agent 面 generate-stream 的
JSONL 事件；业务面进度 SSE（`GET /api/itinerary/{id}/events`）的运行时帧集
（research_start/day_start/heartbeat 等 12 类）以 `app/services/generation_events.py`
头注的帧表为单一真源，**不并入本契约**——两个消费域不同（agent 面 wire 消费方 vs
浏览器进度 UI），合并会把浏览器心跳帧也塞进跨语言契约。

实现要点：
- 字段与既有 wire 形状严格一致：camelCase 由 WireModel 别名生成
  （run_id→runId、days_expected→daysExpected 等）；
- 嵌套 plan/items 复用 trip 契约模型（DailyPlan/Suggestion），
  嵌套字段（dayNo/poiName/...）由它们定义，不在此重复声明；
- type 用 Literal 判别（discriminated union），schema 导出为 oneOf + const，
  Java 侧可对未知类型做前向兼容忽略；
- 导出前剥离 description：docstring 只服务 Python 可读性，不应触发契约漂移。
"""

from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field, TypeAdapter

from app.schemas.common import WireModel
from app.schemas.contracts import mark_all_properties_required, strip_descriptions
from app.schemas.trip import DailyPlan, Suggestion


class StartEvent(WireModel):
    type: Literal["start"]
    run_id: str


class DayEvent(WireModel):
    type: Literal["day"]
    plan: DailyPlan


class DayPatchEvent(WireModel):
    """酒店摊铺等确定性修补后的单天重发（Java 按 overwrite 落库）。"""

    type: Literal["day_patch"]
    plan: DailyPlan


class SuggestionsEvent(WireModel):
    type: Literal["suggestions"]
    items: list[Suggestion]


class DoneEvent(WireModel):
    type: Literal["done"]
    days_expected: int
    days_emitted: list[int]
    trip_theme: str | None
    complete: bool
    message: str | None


class ErrorEvent(WireModel):
    type: Literal["error"]
    message: str


class ItemPreviewEvent(WireModel):
    """逐项候选预览（M5a）：流式生成中单个 item JSON 完整闭合即发布。

    候选身份 = runId + dayNo + itemOrdinal，由服务端分配，**与 LLM 的 chunk
    切割无关**（同一候选无论流怎么切片/重组，身份三元组不变）。
    `item` 是开放形状（dict）：候选未过 ground/终检，坐标与关键事实保持
    未知，消费方按「正在完善」呈现；同一天的正式 day 事件随后到达，day
    快照是权威内容，用它整体替换该日预览。预览不落库，断线重连后以
    DB snapshot 对账即可，不要求复活所有未落库候选。
    """

    type: Literal["day_item_preview"]
    run_id: str
    day_no: int
    item_ordinal: int
    item: dict[str, Any]
    status: Literal["drafting"] = "drafting"


class ItemPreviewWithdrawnEvent(WireModel):
    """逐项候选撤回（M5a）：已发布的候选失效时显式撤回。

    典型场景：整段生成腿在发布候选后失败/降级，缺口天交逐日兜底重生成——
    旧候选必须显式撤回（消费方按 runId + dayNo + itemOrdinal 定位居右移除），
    不能留在板上，也不能静默变成另一个地点。身份同样与 chunk 切割无关。
    """

    type: Literal["day_item_preview_withdrawn"]
    run_id: str
    day_no: int
    item_ordinal: int
    reason: str


StreamEvent = Annotated[
    StartEvent
    | DayEvent
    | DayPatchEvent
    | SuggestionsEvent
    | DoneEvent
    | ErrorEvent
    | ItemPreviewEvent
    | ItemPreviewWithdrawnEvent,
    Field(discriminator="type"),
]

_EVENT_ADAPTER = TypeAdapter(StreamEvent)


def to_wire(event: BaseModel) -> dict:
    """事件模型 → JSON Lines 的 wire dict（camelCase）。"""
    return event.model_dump(mode="json", by_alias=True)


def export_schema() -> dict:
    """导出事件联合的 JSON Schema（含 $defs 嵌套契约，顺序确定可做字节比对）。

    wire 整形（剥 description / 全属性 required）是 G-1.1 提级的共享实现，
    与 API 契约组（app/schemas/contracts.py）同一套语义。
    """
    schema = _EVENT_ADAPTER.json_schema(by_alias=True)
    strip_descriptions(schema)
    mark_all_properties_required(schema)
    return schema
