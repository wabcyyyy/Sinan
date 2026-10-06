"""生成上下文构建：研究证据收集与进度事件（G-2.2 自 day_stream 拆出）。

职责：run_plan_context 经 Supervisor 并行派发三域研究 Agent 产出证据，
返回 {candidates, foods, hotels, consumption}；携带 itinerary_id 时发布
研究进度事件（research_start / research_done / degraded）与 runId 轨迹。
与整段生成的 search 节点共用同一条研究链路。

依赖：research.run_research_context、common.event_publisher、trace。
"""

from datetime import date

from app.agent.research import run_research_context
from app.agent.runtime.memory import WorkingMemory
from app.agent.runtime.trace import current_run_id, record_event
from app.common.event_publisher import (
    publish_degraded,
    publish_research_done,
    publish_research_start,
)
from app.schemas.trip import GenerateRequest

# 进度事件里的研究领域清单：与 Supervisor.decompose 派发的三域一致
# （attraction/hotel/food），顺序沿用协议示例（attraction 在前）。
_RESEARCH_EVENT_DOMAINS = ("attraction", "food", "hotel")


def _research_event_stats(context: dict) -> tuple[int, bool, list[dict], str | None]:
    """从研究上下文如实统计事件字段，供 research_done/degraded 事件使用。

    真实来源（不编造）：run_research_context → synthesize 写入的
    research_report.agents，即各域 EvidencePack.to_dict()——count 为该域
    证据条数、degraded 为单域降级标志、gaps 为降级缺口描述；证据总量取
    实际返回的 candidates/foods/hotels 三列表长度之和。研究报告缺失时
    （如测试 stub）退回列表长度口径，且无法确认降级则按未降级处理。

    返回 (evidence_count, degraded, domains, degraded_reason)。
    """
    candidates = context.get("candidates") or []
    foods = context.get("foods") or []
    hotels = context.get("hotels") or []
    evidence_count = len(candidates) + len(foods) + len(hotels)
    report = context.get("research_report")
    agents = report.get("agents") if isinstance(report, dict) else None
    agents = agents if isinstance(agents, dict) else {}
    fallback_counts = {"attraction": len(candidates), "food": len(foods), "hotel": len(hotels)}
    domains: list[dict] = []
    degraded = False
    gaps: list[str] = []
    for domain in _RESEARCH_EVENT_DOMAINS:
        info = agents.get(domain)
        count = fallback_counts[domain]
        if isinstance(info, dict):
            try:
                count = int(info.get("count", count))
            except (TypeError, ValueError):
                count = fallback_counts[domain]
            if info.get("degraded"):
                degraded = True
            gaps.extend(str(g) for g in (info.get("gaps") or []) if g)
        domains.append({"domain": domain, "count": count})
    # 缺口描述可能很长，截断到前几条，避免单个事件塞爆 SSE 帧
    reason = "；".join(gaps[:5]) if degraded else None
    return evidence_count, degraded, domains, reason


def run_plan_context(
    city: str,
    preferences: list[str],
    itinerary_id: int | None = None,
    *,
    start_date: str | None = None,
    days: int = 1,
    origin_city: str | None = None,
) -> dict:
    """构建单日生成上下文：Supervisor 并行派发三个研究 Agent 产出证据。

    与整段生成的 search 节点共用同一条研究链路（多 Agent 编排），
    返回形状保持 {candidates, foods, hotels, consumption, weather} 不变。

    start_date（行程首日）+ days（总天数）用于城市级天气（C3.1）：一次取
    整趟预报窗，逐日消费按 day_no 挑行；缺省（调试端点、chat 重排）不带
    日期则无天气。

    itinerary_id 可选：携带时向 Redis 发布研究进度事件（research_start /
    research_done / degraded），由业务侧 SSE（event_hub）转发前端；事件是尽力而为
    通知，缺失或 Redis 故障都不影响本函数返回。当前 trace 上下文的 run_id
    （M5）会随事件 data（key=runId）下发并落轨迹，无 trace 上下文时退化为
    旧形态。
    """
    # M5 三向关联：事件 ↔ 行程 ↔ 轨迹。run_id 在函数入口取一次，
    # 保证同一轮研究的三类事件关联到同一条轨迹。
    run_id = current_run_id()
    publish_research_start(itinerary_id, list(_RESEARCH_EVENT_DOMAINS), run_id=run_id)
    try:
        req = GenerateRequest(
            city=city,
            days=max(days, 1),
            persons=1,
            preferences=preferences,
            start_date=start_date,
            # L14：有出发地才查航班（研究阶段由 synthesize 直取报价）
            origin_city=origin_city,
        )
        context = run_research_context(req)
    except Exception as exc:
        # 兜底口径：研究失败时 HTTP 层会转错误信封，由编排器决定重试
        publish_degraded(
            itinerary_id, "research", f"研究失败：{exc}", "该日返回错误信封，等待编排器重试", run_id=run_id
        )
        raise
    evidence_count, degraded, domains, reason = _research_event_stats(context)
    publish_research_done(itinerary_id, evidence_count, degraded, domains, run_id=run_id)
    if degraded:
        # 单域研究走了降级包（Supervisor 捕获域异常后不阻塞其它域）：
        # research_done 已带 degraded 标志，这里补充 reason/fallback 细节
        publish_degraded(itinerary_id, "research", reason or "部分研究域降级", "以现有证据继续生成", run_id=run_id)
    return {
        "candidates": context["candidates"],
        "foods": context["foods"],
        "hotels": context["hotels"],
        "consumption": context["consumption"],
        "weather": context.get("weather"),
        # L14：往返报价（无出发地/未映射/无报价时为空列表，缺席已由工具层留痕）
        "flight_quotes": context.get("flight_quotes") or [],
    }


def filter_used(items: list[dict], used: set[str]) -> list[dict]:
    """过滤已用点位；空集即「候选耗尽」。

    不回退已用点位（回退会破坏跨天去重）；空集时记录遥测并让下游以
    空参考资料降级（开放模式模型自选 + unverified 标记）。
    """
    kept = WorkingMemory(used_names=set(used)).filter_unused(items)
    if items and not kept:
        record_event("decision", "candidates_exhausted", metadata={"input": len(items), "used": len(used)})
    return kept


def parse_date(s: str | None) -> date | None:
    if not s:
        return None
    try:
        return date.fromisoformat(s)
    except ValueError:
        return None
