"""输出装配：图节点的 format 阶段（G-2.4 自 workflow 拆出）。

把生成态（raw_plans + 研究态）装配为 GenerateResponse：事实落地、价格与预算
重算、最终校验、质量判定、备选池补齐、质量兜底原因。

依赖：formatting 包内三块（facts/prices/quality）+ 生成侧下层面；不 import workflow。
"""

import logging
from typing import cast

from app.agent.generation.content.suggestions import activity_floor, build_suggestions, fill_suggestion_gaps
from app.agent.generation.output.facts import (
    apply_item_facts,
    build_lookup,
    sync_duration_from_time_window,
)
from app.agent.generation.output.prices import PriceStage
from app.agent.generation.output.quality import judge_output, run_final_validation
from app.agent.generation.rules.budget import budget_tier
from app.agent.generation.rules.generation_core import (
    MAX_FIX_ATTEMPTS,
    PRICE_ESTIMATE_NOTE_SUFFIX,
    count_hotel_nights_in_budget,
    sanitize_itinerary_items,
)
from app.agent.research.agent_state import UnifiedAgentState
from app.agent.runtime.trace import record_event, traced
from app.schemas.trip import DailyPlan, GenerateRequest, GenerateResponse, SourceRecord, Suggestion, TripItem

logger = logging.getLogger(__name__)


def generation_attempt_limit(state: UnifiedAgentState) -> int:
    request = state.request
    return 1 if request is not None and request.days > 1 else MAX_FIX_ATTEMPTS


@traced("node", "format")
def quality_fallback_reason(state: UnifiedAgentState) -> str | None:
    """重试耗尽时的口径：保留开放模式结果并如实降级标注，不用知识库候选替换模型。"""
    if state.validation_issues and state.fix_count > generation_attempt_limit(state):
        # LLM-only 原则：重试耗尽也不允许用知识库候选拼装行程替换模型结果
        # （那是"直接使用知识库"）。保留开放模式结果，把未修复的约束问题
        # 如实降级标注，交给用户复核。
        record_event("route", "keep_llm_result", metadata={"reason": "validation_retry_exhausted"})
        return "开放模式结果未通过全部约束校验（见校验日志），已保留但请复核当日时间安排"
    return None


def format_output(state: UnifiedAgentState) -> dict:
    """组装 GenerateResponse：回填权威事实 → 定价 → 终检 → 质量结论 → 建议池。

    各阶段实现见 app.agent.generation.output；本函数只保留跨阶段的编排与状态流转。
    """
    req: GenerateRequest = cast("GenerateRequest", state.request)
    raw_plans = state.daily_plans
    schedule_report: dict = dict(state.schedule_report or {})
    # 多 Agent 研究阶段统计随 schedule_report 透出（证据包规模/置信度/缺口）。
    if state.research_report:
        schedule_report["research"] = state.research_report
    fallback_reason = quality_fallback_reason(state)
    consumption = state.consumption or {}

    lookup = build_lookup(state.candidates, state.foods, state.hotels)
    prices = PriceStage.create(req)
    source_records: dict[str, SourceRecord] = {}
    daily_plans: list[DailyPlan] = []
    hotel_total = 0.0
    attraction_total = 0.0
    # BIZ-6：与 day_persistence 落库同口径（N-1 晚）——wire 响应也不再保留
    # 「最后一天入住」的酒店项，items 与预算口径才不互相矛盾
    raw_stay_nights = getattr(req, "stay_nights", None)
    stay_nights = int(raw_stay_nights) if raw_stay_nights is not None else max((req.days or 0) - 1, 0)
    for plan in raw_plans:
        items = []
        # 开放模式（LLM 世界知识定价）note 后缀（GROUND-3）：与单日链同口径同常量，
        # 同一行程混排两种链不再出现肉眼可见的口径差
        note = plan.get("note")
        if bool((state.schedule_report or {}).get("open_research")) and note:
            note = (note + PRICE_ESTIMATE_NOTE_SUFFIX).strip()
        # LLM 可能输出 souvenir/activity 等扩展类型：归一到 TripItem 契约，避免 Pydantic 500
        for item in sanitize_itinerary_items(plan.get("items")):
            if item.get("item_type") == "hotel" and int(plan["day_no"] or 0) > stay_nights:
                continue
            # city 必须传：证据票按 `name:{城市}|{归一名}` 建键，漏传等于查询侧换了
            # 一把键去取，服务端已接地的字段反被降级成 client-context。
            apply_item_facts(item, lookup.get(str(item.get("poi_name") or "")), source_records, city=req.city)
            if item.get("item_type") == "hotel":
                prices.price_hotel(item)
                if count_hotel_nights_in_budget(plan["day_no"], req.days, "hotel"):
                    hotel_total += float(item.get("cost") or 0)
            elif item.get("item_type") == "food":
                prices.price_food(item, float(consumption.get("meal_price") or 0) or None)
            elif item.get("item_type") == "attraction":
                attraction_total += float(item.get("cost") or 0)
            sync_duration_from_time_window(item)
            items.append(TripItem(**item))
        daily_plans.append(
            DailyPlan(
                day_no=plan["day_no"],
                note=note,
                items=items,
                theme=plan.get("theme"),
                mini_route=plan.get("mini_route") or {},
                backup_plan=plan.get("backup_plan") or [],
                photo_spots=plan.get("photo_spots") or [],
                practical_notes=plan.get("practical_notes") or [],
                day_options=plan.get("day_options") or [],
                trip_theme=plan.get("trip_theme"),
            )
        )

    budget_estimate = prices.recompute_budget(
        state.budget_estimate, daily_plans, hotel_total, attraction_total, consumption
    )
    check = run_final_validation(req, daily_plans, schedule_report, state.consumption)
    outcome = judge_output(state, daily_plans, schedule_report, check, fallback_reason)

    # 开放模式下模型建议可来自候选池之外（allow_external）：坐标留空的
    # 条目由前端在加入行程前经地图检索补齐；候选池保底链路仍保持池内过滤。
    open_research = bool((state.schedule_report or {}).get("open_research"))
    activities = activity_floor(req.city)
    tier_label, _tier_g, _tier_ppd = budget_tier(req.budget, req.persons, req.days)
    suggestion_rows = fill_suggestion_gaps(
        build_suggestions(
            raw_plans,
            (state.candidates or []) + activities,
            state.foods,
            state.hotels or [],
            state.raw_suggestions or [],
            allow_external=open_research,
            dest_city=req.city,
        ),
        req.city,
        budget_tier=tier_label or None,
    )

    return {
        "result": GenerateResponse(
            city=req.city,
            days=req.days,
            title=f"{req.city}{req.days}日游",
            # 整趟主题承接第 1 天（open_trip 顶层 / open_day day_no==1 产出）
            trip_theme=(daily_plans[0].trip_theme if daily_plans else None),
            daily_plans=daily_plans,
            budget_estimate=budget_estimate,
            suggestions=[Suggestion(**row) for row in suggestion_rows],
            validation_log=outcome.validation_log,
            price_note=prices.price_note(),
            schedule_report=schedule_report,
            critic_report=check.critic_report,
            destination_status=outcome.destination_status,
            sources=list(source_records.values()),
            quality_report=outcome.quality_report,
            status=outcome.status,
            status_reason=outcome.status_reason,
        )
    }
