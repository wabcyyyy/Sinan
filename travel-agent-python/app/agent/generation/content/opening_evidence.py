"""优先为必去地点和博物馆取开放证据；HTTP 在落库事务外，失败保留 unknown。"""

from __future__ import annotations

import re
import unicodedata

from app.agent.data.web_search import web_search_enabled, web_search_sources
from app.agent.generation.rules.opening_rules import parse_opening_rules
from app.agent.grounding.grounding_evidence import issue_evidence
from app.agent.runtime.run_limits import RunLimitExceeded, phase_scope
from app.agent.runtime.trace import record_event
from app.schemas.opening_hours import OpeningHours
from app.schemas.trip import DailyPlan, FactEvidence


def _name(value: str) -> str:
    # 不去掉分馆/分店注记：总馆的营业时间不能替另一个分馆背书。
    return "".join(unicodedata.normalize("NFKC", value).split()).casefold()


def _unknown(reason: str) -> FactEvidence:
    return FactEvidence(provider="web.search", opening_hours=OpeningHours(reason=reason))


def opening_evidence(city: str, name: str) -> FactEvidence:
    """只从实体与城市均匹配的网页正文提取；不采信搜索摘要/模型推断的时刻。"""
    if not city.strip() or not name.strip():
        return _unknown("缺少城市或地点名，无法确定来源实体")
    rows = web_search_sources(f"{city} {name} 官方 开放时间 闭馆日 停止入馆")
    candidates = []
    observed = []
    for row in rows:
        title, body = str(row.get("title") or ""), str(row.get("content") or "")
        if not body or _name(name) not in _name(title) or _name(city) not in _name(title + body):
            continue
        if re.search(r"周边|攻略|合集|大全|榜单|汇总|附近", title):
            continue
        try:
            hours = parse_opening_rules(body)
        except ValueError:
            continue
        observed.append((row, hours))
        if hours.status != "known":
            continue
        # 原句与规则一起保留；来源不具备可追溯 URL/观测时间就不能升级。
        if not row.get("url") or not row.get("retrieved_at"):
            continue
        candidates.append((row, hours))
    if not candidates:
        if observed:
            row, hours = observed[0]
            return FactEvidence(
                provider="web.search",
                source_url=row.get("url"),
                retrieved_at=row.get("retrieved_at"),
                excerpt=str(row.get("content") or "")[:1200],
                opening_hours=hours if hours.status == "unknown" else OpeningHours(reason="来源缺少 URL 或观测时间"),
            )
        return _unknown("未找到实体、城市和开放规则均可确认的网页正文")
    return _source_fact(candidates, city, name)


def _source_fact(candidates: list, city: str, name: str) -> FactEvidence:
    signatures = {
        (
            hours.valid_from,
            hours.valid_to,
            tuple(sorted({str(rule.model_dump(exclude={"source_text"})) for rule in hours.rules})),
        )
        for _, hours in candidates
    }
    row, hours = candidates[0]
    excerpt = "\n".join(dict.fromkeys(rule.source_text for rule in hours.rules))
    if len(signatures) > 1:
        return FactEvidence(
            provider="web.search",
            source_url=row["url"],
            retrieved_at=row["retrieved_at"],
            excerpt=excerpt,
            opening_hours=OpeningHours(
                reason="检索来源的开放规则不一致，需人工复核：" + "；".join(r["url"] for r, _ in candidates)
            ),
        )
    ticket_row = {
        "name": name,
        "city": city,
        "source": "web.search",
        "source_url": row["url"],
        "source_fetched_at": row["retrieved_at"],
        "open_time": excerpt,
        "opening_hours": hours.model_dump(mode="json"),
    }
    ticket = issue_evidence(ticket_row)
    return FactEvidence(
        source_ref=ticket.evidence_id if ticket else None,
        provider="web.search",
        source_url=row["url"],
        retrieved_at=row["retrieved_at"],
        excerpt=excerpt,
        verification_status="partially_verified",
        value_kind="observed",
        opening_hours=hours,
    )


def enrich_opening_hours(plan: DailyPlan, *, city: str, required_names: list[str]) -> None:
    """每次最多三个优先地点、共用 12 秒子时限和既有检索额度，不追加模型调用。"""
    if not web_search_enabled():
        return
    museums = [
        item.poi_name
        for item in plan.items
        if item.item_type == "attraction" and re.search(r"博物[馆院]|纪念馆", item.poi_name)
    ]
    present = {_name(item.poi_name) for item in plan.items}
    targets = list(dict.fromkeys([*(name for name in required_names if _name(name) in present), *museums]))
    selected = targets[:3]
    evidence = {}
    try:
        with phase_scope(12):
            for name in selected:
                evidence[_name(name)] = opening_evidence(city, name)
    except RunLimitExceeded:
        pass  # 已收集字段保留，其余明确记 unknown；总任务失权/取消不在此捕获。
    target_keys = {_name(name) for name in targets}
    for item in plan.items:
        if _name(item.poi_name) not in target_keys:
            continue
        fact = evidence.get(_name(item.poi_name)) or _unknown("本次开放取证未完成或超出优先查询额度")
        # 只写开放字段，坐标/身份/价格的证据不变。
        item.fact_evidence["open_time"] = fact
        if fact.opening_hours and fact.opening_hours.status == "known":
            item.open_time = fact.excerpt
    record_event(
        "retrieval",
        "opening.enrich",
        metadata={
            "selected": len(selected),
            "known": sum(f.opening_hours.status == "known" for f in evidence.values()),
            "unknown": sum(f.opening_hours.status == "unknown" for f in evidence.values()),
        },
    )
