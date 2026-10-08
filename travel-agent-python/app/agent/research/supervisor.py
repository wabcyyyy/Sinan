"""Supervisor 研究编排：需求分解 → 并行派发 → 证据整合。

职责：
- decompose：把 GenerateRequest 分解为三个 ResearchTask（酒店/景点/美食）；
- run_research_parallel：3-worker 线程池并行执行研究 Agent，携带 trace 上下文，
  单域异常降级为 degraded 证据包（不阻塞其它域与整合）；
- synthesize：三份证据包映射回现有上下文契约 {candidates, foods, hotels, consumption}
  + research_report（供 format_output 写入 schedule_report）；
- run_research_context：整段生成（workflow.search）与逐日（plan-context）共用的入口。

实现要点：
- 工具调用统一走 app.agent.tools.impl 模块属性（运行时解析），单测注入契约不变；
- 城市级消费数据（get_consumption）不属于任何 POI 域，由 Supervisor 直接获取；
- 输出形状与旧 search_pois 完全一致，下游 generate/format 零改动。
"""

from __future__ import annotations

import contextvars
import logging
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import date, timedelta

from app.agent.core.intent import build_intent_keywords
from app.agent.data import weather as weather_module
from app.agent.research.evidence import EvidencePack, ResearchDomain, ResearchTask
from app.agent.research.factory import run_research
from app.agent.runtime.observability import metrics
from app.agent.runtime.trace import record_event
from app.agent.tools import flight_quotes
from app.agent.tools import impl as tools
from app.common.config import settings
from app.schemas.trip import GenerateRequest

logger = logging.getLogger(__name__)


def decompose(req: GenerateRequest) -> list[ResearchTask]:
    """按用户请求分解研究任务；各域规模支撑「发现更多」候选池。"""
    # M3-②（AD5）：intent 纯规则抽词随任务卡下发，驱动补池检索与意图覆盖评估。
    intent_keywords = build_intent_keywords(req.intent)
    return [
        ResearchTask(
            domain="attraction",
            city=req.city,
            preferences=req.preferences,
            budget=req.budget,
            limit=40,
            intent=req.intent,
            intent_keywords=intent_keywords,
        ),
        ResearchTask(
            domain="food",
            city=req.city,
            budget=req.budget,
            limit=16,
            intent=req.intent,
            intent_keywords=intent_keywords,
        ),
        ResearchTask(
            domain="hotel",
            city=req.city,
            budget=req.budget,
            hotel_tier=req.hotel_tier,
            limit=10,
            intent=req.intent,
            intent_keywords=intent_keywords,
        ),
    ]


def run_research_parallel(tasks: list[ResearchTask]) -> dict[ResearchDomain, EvidencePack]:
    """并行执行全部研究任务；单域失败降级为 degraded 证据包。

    必须在提交点（主线程）copy_context：worker 线程默认不继承 contextvars，
    若在 worker 内才 copy，trace/预算上下文已经丢失，研究轨迹挂不到本次运行。
    """
    packs: dict[ResearchDomain, EvidencePack] = {}
    # 研究阶段并行度 = 三个领域 Agent；与 _search_pois 的并行度口径一致，
    # 避免并行检索把高德/RAG 的等待从串行叠加放大。调用期读 settings
    # （PR-11 并发配置化，原硬编码 3），monkeypatch 单点可注入。
    with ThreadPoolExecutor(max_workers=settings.research_workers, thread_name_prefix="research-agent") as pool:
        futures: dict[ResearchDomain, Future[EvidencePack]] = {}
        for task in tasks:
            context = contextvars.copy_context()
            futures[task.domain] = pool.submit(context.run, lambda t=task: run_research(t))
        for domain, future in futures.items():
            try:
                packs[domain] = future.result()
            except Exception as exc:
                logger.warning("research agent %s failed: %s", domain, exc)
                packs[domain] = EvidencePack(
                    domain=domain, confidence=0.0, rounds=0, gaps=[f"研究失败：{exc}"], degraded=True
                )
    return packs


def _trip_end_date(req: GenerateRequest) -> str | None:
    """行程末日期（含首尾）；无出发日期返回 None（天气窗口无法确定）。"""
    if not req.start_date:
        return None
    try:
        start = date.fromisoformat(str(req.start_date)[:10])
    except ValueError:
        return None
    return (start + timedelta(days=max(req.days, 1) - 1)).isoformat()


def _flight_quotes(req: GenerateRequest) -> list[dict]:
    """往返航班报价（L14）：与 consumption/weather 同类的 **trip 级非点位证据**。

    为什么不做成研究域：研究域那套机制是给"LLM 多轮检索点位池"用的（计划 →
    检索 → LLM 评估充分性 → 补关键词重查），而航班报价是一次确定性 API 取数，
    套进研究域等于为一次 HTTP 调用付 LLM 评估与重查的钱。consumption/weather
    已经确立了同类证据"直取"的先例，本函数沿用。

    只在有 origin_city 时外呼；缺席的每一种原因（无出发地/城市未映射/无 token/
    上游无价）都由工具层的 `_quote_absence` 记日志与轨迹事件，这里不编造兜底。
    无出发地的短路同样留痕（恢复保真 P0-3）：恢复重建的命令此前丢过 origin_city，
    若这条分支静默 return []，"缺席都留痕"就在自己身上失效。
    """
    if not str(req.origin_city or "").strip():
        logger.warning("flight quotes absent: no_origin (city=%s)", req.city)
        record_event("tool", "flight.search_quotes", status="degraded", error="no_origin", metadata={"city": req.city})
        return []
    return (
        flight_quotes.search_flight_quotes(
            str(req.origin_city),
            req.city,
            req.start_date,
            _trip_end_date(req),
        )
        or []
    )


def synthesize(packs: dict[ResearchDomain, EvidencePack], req: GenerateRequest) -> dict:
    """证据整合：映射回旧上下文契约并产出 research_report。"""
    attraction = packs.get("attraction") or EvidencePack(domain="attraction")
    food = packs.get("food") or EvidencePack(domain="food")
    hotel = packs.get("hotel") or EvidencePack(domain="hotel")
    consumption = tools.get_consumption(req.city)
    # 城市级天气（C3.1）：与 consumption 同类的非点位证据，走同一通道；
    # 失败静默（None），不进 research_report、不发事件，研究/生成形状零漂移。
    weather = weather_module.get_weather_forecast(req.city, req.start_date, _trip_end_date(req))
    research_report = {
        "mode": "supervisor",
        "agents": {
            domain: (packs[domain].to_dict() if domain in packs else None) for domain in ("attraction", "food", "hotel")
        },
    }
    record_event("decision", "research_summary", metadata=research_report)
    # 研究阶段汇总指标（证据规模/推理轮次/降级包数）由 Supervisor 统一上报。
    metrics.record_research(
        items=sum(len(p.items) for p in packs.values()),
        rounds=sum(p.rounds for p in packs.values()),
        degraded=sum(1 for p in packs.values() if p.degraded),
    )
    return {
        "candidates": attraction.items,
        "foods": food.items,
        "hotels": hotel.items,
        "consumption": consumption,
        "weather": weather,
        # L14 往返报价：无出发地时为空列表（不进 research_report，与 weather
        # 同口径——研究报告仍只描述三域点位证据，形状零漂移）
        "flight_quotes": _flight_quotes(req),
        "research_report": research_report,
    }


def run_research_context(req: GenerateRequest) -> dict:
    """Supervisor 研究一站式入口：分解 → 并行 → 整合。"""
    packs = run_research_parallel(decompose(req))
    return synthesize(packs, req)


def run_refill(domain: ResearchDomain, req: GenerateRequest) -> EvidencePack:
    """Supervisor 缺口补查：对指定域发起一次更激进的证据研究。

    用于 reflect 校验发现"证据型缺口"（如未安排任何景点）时，只重派发该域
    研究 Agent 补查证据，而不是整体重生成行程。
    """
    task = ResearchTask(
        domain=domain,
        city=req.city,
        preferences=[*(req.preferences or []), "热门", "必游"],
        budget=req.budget,
        hotel_tier=req.hotel_tier,
        limit=60,
    )
    return run_research(task)


def merge_candidates(existing: list[dict], supplement: list[dict]) -> list[dict]:
    """合并补查证据：按名称去重，已有候选优先（不覆盖权威字段）。"""
    by_name = {str(p.get("name")): p for p in existing if p.get("name")}
    merged = list(existing)
    for poi in supplement or []:
        name = str(poi.get("name") or "").strip()
        if name and name not in by_name:
            by_name[name] = poi
            merged.append(poi)
    return merged
