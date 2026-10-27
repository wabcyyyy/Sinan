"""有限取证工具选择；复用路线、营业证据与官方正文适配器，不自动调用付费搜索。"""

from app.agent.core.poi_identity import norm_ws_key
from app.agent.data.route_service import default_route_service
from app.agent.generation.content.evidence_observations import FactObservation
from app.agent.generation.content.opening_evidence import opening_evidence
from app.agent.generation.rules.opening_rules import parse_opening_rules
from app.agent.grounding.official_pages import fetch_official_page
from app.agent.runtime.run_limits import RunLimitExceeded, TaskStopped, current_limits
from app.schemas.trip import FactEvidence


def query_evidence(gap, problems, prepared, request, options):
    limits = current_limits()
    if limits:
        limits.check("retrieval")
    targets = [
        next(p for p in prepared if p.fact.candidate_id == identifier)
        for identifier in (gap.first_id, gap.second_id)
        if identifier is not None
    ]
    ids = tuple(p.fact.source_entity_id or "" for p in targets)
    if not all(ids):
        return FactObservation(
            gap.kind, ids, tool="ask_user", question=f"请先确认「{' → '.join(gap.names)}」的具体地点实体。"
        )
    if gap.kind == "route":
        points = [
            {"poi_id": p.fact.candidate_id, "latitude": p.item.latitude, "longitude": p.item.longitude} for p in targets
        ]
        route = default_route_service.get_route(
            *points, mode=request.requirements_struct.transport_preference or "walking"
        )
        if limits:
            limits.check("deadline")  # 路线适配器的降级不能吞掉取消/失权。
        if route and route.get("source") in {"osrm", "openrouteservice"}:
            fact = FactEvidence(
                source_url=route.get("source_url"),
                retrieved_at=route.get("fetched_at"),
                provider=route["source"],
                verification_status="partially_verified",
                value_kind="observed",
            )
            return FactObservation("route", ids, route=route, fact=fact, tool="route_query")
    elif gap.kind in {"price", "opening"}:
        source = targets[0].fact.source_url
        if source:
            try:
                page = fetch_official_page(source)
            except (TaskStopped, RunLimitExceeded):
                raise
            except Exception:
                page = None
            if (
                page
                and norm_ws_key(targets[0].item.poi_name) in norm_ws_key(page.excerpt)
                and norm_ws_key(request.city) in norm_ws_key(page.excerpt)
            ):
                if gap.kind == "opening":
                    hours = parse_opening_rules(page.excerpt)
                    fact = FactEvidence(
                        source_url=page.url,
                        retrieved_at=page.observed_at,
                        excerpt=page.excerpt,
                        provider="official_page",
                        verification_status="partially_verified",
                        value_kind="observed",
                        opening_hours=hours,
                    )
                    return FactObservation(
                        "opening", ids, fact=fact, tool="official_source_page", binding_basis="source_name_city"
                    )
                value = page.fields.get("ticket", "")
                amount = 0 if value == "free" else value.removeprefix("paid:") if value.startswith("paid:") else None
                if amount is not None and request.requirements_struct.constraint_planning.currency == "CNY":
                    fact = FactEvidence(
                        source_url=page.url,
                        retrieved_at=page.observed_at,
                        excerpt=page.excerpt,
                        provider="official_page",
                        verification_status="partially_verified",
                        value_kind="observed",
                    )
                    return FactObservation(
                        "price",
                        ids,
                        value=amount,
                        currency="CNY",
                        fact=fact,
                        tool="official_source_page",
                        binding_basis="source_name_city",
                    )
        if gap.kind == "opening" and options.allow_source_search:
            fact = opening_evidence(request.city, targets[0].item.poi_name)
            return FactObservation("opening", ids, fact=fact, tool="opening_sources", binding_basis="source_name_city")
    labels = {
        "opening": "适用出行日期的开放时段",
        "price": "原币费用",
        "duration": "计划停留分钟数",
        "route": "该方向路段所需时间",
    }
    return FactObservation(
        gap.kind,
        ids,
        tool="ask_user",
        question=(
            f"「{' → '.join(gap.names)}」的{labels[gap.kind]}会影响本轮选择，当前来源未确认。"
            "请提供可核对来源或在需求中明确安排；未确认前保留未知。"
        ),
    )
