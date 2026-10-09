"""mode=stream 分支（PR-4 流式归一收编）：stream_generate 节点 + 整段流式入口。

原旁路 `trip_stream` + 手写字符级 JSON 状态机删除后，整段流式由统一图承载：

```text
dispatch ──mode=stream──► stream_generate（生成 → 落地 → 摊铺/备选池，逐天 yield）──► END
```

- 逐天 yield 走 LangGraph `stream_mode="custom"`（节点内 `get_stream_writer()`），
  wire 事件仍走 `schemas/stream_events.to_wire`（契约不变）；
- LLM 形态 = 整段一次调用（llm_open_trip，模型看得见全盘）+ 截断逐日兜底
  （缺口天 llm_open_day，suggestions 随天收集——截断不可能丢建议池）；
  M5a 起经 run config `item_previews` 显式开启时整段腿改走 llm_open_trip_stream，
  逐项候选以 day_item_preview wire 事件先于正式 day 事件下发（默认关 = 既有
  事件序列逐字节不变，业务面/agent 面入口显式开启）。
- 生成/落地/去重实现全在 `open_plans`（on_day/on_patch 挂点），本模块只做
  「事件化 + 产品语义」：研究在外层（上下文随请求带入）、整段天已补终检
  （业务侧 plan_days 落库后跑 validate_plans，违规天重置交逐日循环）、
  缺口天走逐日循环；空草案天不算产出、摊铺补丁只补已发的天。

**可 mock 契约**：测试经本模块属性注入 `fill_suggestion_gaps` / `record_event`，
经 `open_plans.llm_open_trip` / `open_plans.llm_open_day` / `open_plans.generate_open_plans`
注入生成替身（调用期解析）。
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Iterator
from typing import cast
from uuid import uuid4

from langchain_core.runnables import RunnableConfig
from langgraph.config import get_stream_writer

from app.agent.generation.content.day_prompts import DAY_ATTRACTION_CONTEXT_LIMIT, DAY_FOOD_CONTEXT_LIMIT
from app.agent.generation.content.generators import pick_hotels
from app.agent.generation.content.narrative import sync_schedule_summary
from app.agent.generation.content.suggestions import build_suggestions, fill_suggestion_gaps
from app.agent.generation.rules.budget import budget_tier
from app.agent.research.agent_state import MODE_STREAM, UnifiedAgentState
from app.agent.runtime import checkpoint
from app.agent.runtime.trace import current_run_id, record_event, traced
from app.common.llm_client import StreamCancelled
from app.common.model_registry import configured
from app.schemas.stream_events import (
    DayEvent,
    DayPatchEvent,
    DoneEvent,
    ItemPreviewEvent,
    ItemPreviewWithdrawnEvent,
    SuggestionsEvent,
    to_wire,
)
from app.schemas.trip import DailyPlan, GenerateDayRequest, GenerateRequest, Suggestion

logger = logging.getLogger(__name__)


def _stream_plan_model(plan: dict) -> DailyPlan:
    """plan dict → 契约模型（与 generate-day 相同的字段定义，非法字段在此被拒/裁剪）。"""
    result = DailyPlan(**plan)
    sync_schedule_summary(result)
    return result


def _stream_suggestion_models(rows: list[dict]) -> list[Suggestion]:
    return [Suggestion(**row) for row in rows if isinstance(row, dict)]


def _cancel_event(config: RunnableConfig | None) -> threading.Event | None:
    """取消信号经 run config 注入：PR-3 后 state 必须可序列化，cancel 不入 state。"""
    if config is None:
        return None
    value = (config.get("configurable") or {}).get("cancel")
    return value if isinstance(value, threading.Event) else None


def _item_previews_enabled(config: RunnableConfig | None) -> bool:
    """M5a 候选预览开关（run config 注入，默认关 = 既有事件序列不变）。"""
    if config is None:
        return False
    return bool((config.get("configurable") or {}).get("item_previews"))


def _raise_if_cancelled(cancel: threading.Event | None) -> None:
    """取消检查点：在逐天产出与重活（备选池）边界调用。"""
    if cancel is not None and cancel.is_set():
        raise StreamCancelled("客户端断开，生成已取消")


@traced("node", "stream_generate")
def stream_generate(state: UnifiedAgentState, config: RunnableConfig) -> dict:
    """mode=stream 分支：生成 → 落地 → 摊铺/备选池，经 custom stream 逐天 yield。

    产品语义与旧旁路 trip_stream 对齐：研究在外层（上下文随请求带入）、校验修复交给
    业务侧逐日循环；wire 事件仍走 `schemas/stream_events` 的 `to_wire`（契约不变）。
    事件只对**已产出**的天发（空草案天不算产出、摊铺补丁只补已发的天）——旧旁路
    只发解析出的天，语义一致。取消不是失败：StreamCancelled 在此收口，不出 done。
    """
    writer = get_stream_writer()
    req = cast("GenerateDayRequest", state.day_request)
    cancel = _cancel_event(config)
    total_days = max(int(req.days or 1), 1)
    context = req.context or {}
    candidates = context.get("candidates") or []
    foods = context.get("foods") or []
    hotels = context.get("hotels") or []
    request = GenerateRequest(
        city=req.city,
        days=total_days,
        persons=req.persons,
        budget=req.budget,
        start_date=req.start_date,
        hotel_tier=req.hotel_tier,
        preferences=req.preferences,
        requirements=req.requirements,
        intent=req.intent,
        region_hint=req.region_hint,
        # M1b：结构化需求经整段请求贯通（此前反向重建时静默丢弃；
        # origin_city 不在 GenerateDayRequest 上，出发地由研究上下文的报价承载）
        requirements_struct=req.requirements_struct,
    )
    emitted: list[int] = []
    plans: list[dict] = []
    trip_theme: str | None = None

    def on_day(day_no: int, plan: dict) -> None:
        nonlocal trip_theme
        if not plan.get("items"):
            return
        _raise_if_cancelled(cancel)
        emitted.append(day_no)
        plans.append(plan)
        if day_no == 1 and isinstance(plan.get("trip_theme"), str) and plan["trip_theme"].strip():
            trip_theme = plan["trip_theme"].strip()
        writer(to_wire(DayEvent(type="day", plan=_stream_plan_model(plan))))

    def on_patch(plan: dict) -> None:
        if int(plan.get("day_no") or 0) in emitted:
            writer(to_wire(DayPatchEvent(type="day_patch", plan=_stream_plan_model(plan))))

    # M5a 逐项候选挂点：候选包装成契约事件（模型构造 → dump，禁止手搓键名）
    # 后立即下发——预览先于同一 day 的正式 day 事件，day 快照是权威内容，
    # 消费方用它整体替换该日预览。runId 取当前 trace（无 trace 上下文如实空串）。
    def on_item_preview(day_no: int, item_ordinal: int, item: dict) -> None:
        _raise_if_cancelled(cancel)
        writer(
            to_wire(
                ItemPreviewEvent(
                    type="day_item_preview",
                    run_id=current_run_id() or "",
                    day_no=int(day_no),
                    item_ordinal=int(item_ordinal),
                    item=dict(item),
                )
            )
        )

    def on_item_withdrawn(day_no: int, item_ordinal: int, reason: str) -> None:
        writer(
            to_wire(
                ItemPreviewWithdrawnEvent(
                    type="day_item_preview_withdrawn",
                    run_id=current_run_id() or "",
                    day_no=int(day_no),
                    item_ordinal=int(item_ordinal),
                    reason=str(reason),
                )
            )
        )

    increment: dict | None = None
    research_errors: list[str] = []
    try:
        from app.agent.generation.orchestration import open_plans

        if _item_previews_enabled(config):
            increment, research_errors = open_plans.generate_open_plans(
                request,
                req.feedback or "",
                context_hotels=hotels,
                candidates=candidates,
                foods=foods,
                weather=context.get("weather") or [],
                on_day=on_day,
                on_patch=on_patch,
                on_item_preview=on_item_preview,
                on_item_withdrawn=on_item_withdrawn,
            )
        else:
            increment, research_errors = open_plans.generate_open_plans(
                request,
                req.feedback or "",
                context_hotels=hotels,
                candidates=candidates,
                foods=foods,
                weather=context.get("weather") or [],
                on_day=on_day,
                on_patch=on_patch,
            )
    except StreamCancelled:
        logger.info("trip stream cancelled for %s after %d day(s)", req.city, len(emitted))
    if cancel is not None and cancel.is_set():
        # 取消：消费端已断开，不产出 done/suggestions，也跳过备选池等重活；
        # run_status=cancelled 让 metrics 把本次 run 记入 cancelled_runs 而非失败。
        record_event(
            "decision",
            "run_status",
            status="cancelled",
            metadata={"status": "cancelled", "days_emitted": emitted, "days_expected": total_days},
        )
        return {}
    result = increment or {}

    # 备选池：模型建议（含按天携带）+ 权威候选补齐；城市归属过滤已下沉
    # build_suggestions 单一真源（BIZ-3），三条链同口径
    _raise_if_cancelled(cancel)
    raw_suggestions = result.get("raw_suggestions") or []
    tier_label, _tier_g, _tier_ppd = budget_tier(req.budget, req.persons or 1, total_days)
    suggestion_rows = build_suggestions(
        plans,
        candidates[:DAY_ATTRACTION_CONTEXT_LIMIT],
        foods[:DAY_FOOD_CONTEXT_LIMIT],
        pick_hotels(hotels, req.hotel_tier, 3),
        raw_suggestions,
        allow_external=True,
        dest_city=req.city,
    )
    suggestion_rows = fill_suggestion_gaps(suggestion_rows, req.city, budget_tier=tier_label or None)
    writer(to_wire(SuggestionsEvent(type="suggestions", items=_stream_suggestion_models(suggestion_rows))))

    if research_errors and len(emitted) < total_days:
        # 交付不齐才留三态（`observability.record()` 只从 run_status 判
        # degraded/failed）：截断后兜底补齐不算失败，缺天真失败才记账。
        interrupted_status = "failed" if not emitted else "degraded"
        record_event(
            "decision",
            "run_status",
            status=interrupted_status,
            metadata={"status": interrupted_status, "error": "；".join(research_errors), "days_emitted": emitted},
        )

    complete = len(emitted) == total_days and all(plan.get("items") for plan in plans)
    message = None if complete else ("；".join(research_errors) or "部分天未产出")
    record_event(
        "decision",
        "trip_stream_done",
        metadata={
            "city": req.city,
            "days_expected": total_days,
            "days_emitted": emitted,
            "complete": complete,
            "research_errors": len(research_errors),
            "raw_suggestions": len(raw_suggestions),
            "suggestions_final": len(suggestion_rows),
        },
    )
    writer(
        to_wire(
            DoneEvent(
                type="done",
                days_expected=total_days,
                days_emitted=emitted,
                trip_theme=trip_theme,
                complete=complete,
                message=message,
            )
        )
    )
    return result


def run_generate_trip_stream(
    req: GenerateDayRequest, cancel: threading.Event | None = None, *, item_previews: bool = False
) -> Iterator[dict]:
    """整段流式生成入口：统一图 mode=stream 分支的 custom stream 转发。

    逐个 yield wire 事件 dict（day / day_patch / suggestions / done；M5a 开启
    候选预览时另有 day_item_preview / day_item_preview_withdrawn）。cancel 经
    run config 注入（PR-3 后 state 必须可序列化，取消信号不入 state）；LLM 未配置
    沿旧口径抛 ValueError——调用方（业务侧）据此降级逐日生成。

    `item_previews`（M5a，spec §10）：逐项候选预览开关，默认 False——不开启时
    事件序列与既有逐字节一致；业务整段流式腿与 /v1/generate-stream 显式开启。
    """
    if cancel is not None and cancel.is_set():
        record_event(
            "decision",
            "run_status",
            status="cancelled",
            metadata={"status": "cancelled", "reason": "cancelled_before_start"},
        )
        return
    if not configured("main"):
        raise ValueError("未配置 LLM，无法生成行程内容")
    config: RunnableConfig = {
        "configurable": {
            "thread_id": checkpoint.checkpoint_thread_id(f"stream-{uuid4().hex}"),
            "cancel": cancel,
            # M5a：候选预览经 run config 进节点（同 cancel 的注入通道，不入 state）
            "item_previews": item_previews,
        }
    }
    state = {"mode": MODE_STREAM, "day_request": req, "feedback": req.feedback or ""}
    from app.agent.generation.orchestration.trip_graph import unified_agent_graph

    yield from unified_agent_graph.stream(state, stream_mode="custom", config=config)
