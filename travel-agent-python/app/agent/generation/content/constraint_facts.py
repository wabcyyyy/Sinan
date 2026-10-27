"""研究候选 → 带原值/来源的求解快照；不把模型文字当成已核实事实。"""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation

from pydantic import ValidationError

from app.agent.core.poi_identity import norm_ws_key
from app.agent.generation.content.constraint_problem import PlanningCandidate, minute, money_minor
from app.agent.generation.rules.opening_rules import parse_opening_rules
from app.schemas.planning import ConstraintPlanningOptions, PlanningFactContext
from app.schemas.trip import FactEvidence, TripItem


@dataclass
class PreparedCandidate:
    item: TripItem
    raw: dict
    fact: PlanningFactContext
    gaps: list[str]


def _amount(raw: dict, item_type: str) -> tuple[float | None, str | None]:
    value = raw.get("cost", raw.get("ticket_price" if item_type == "attraction" else "avg_price"))
    currency = str(raw.get("cost_currency") or raw.get("price_currency") or raw.get("currency") or "").upper()
    try:
        parsed = Decimal(str(value))
        money_minor(parsed)
        amount = float(parsed) if parsed <= 1_000_000 else None
    except (ValueError, InvalidOperation):
        amount = None
    return amount, currency if len(currency) == 3 and currency.isascii() and currency.isalpha() else None


def _evidence(raw: dict) -> dict[str, FactEvidence]:
    result = {}
    for key, value in (raw.get("fact_evidence") or {}).items():
        try:
            result[key] = value if isinstance(value, FactEvidence) else FactEvidence.model_validate(value)
        except (ValidationError, TypeError):
            continue  # 不合法字段证据不得升级；相应字段按 unknown 处理。
    return result


def prepare_candidates(
    context: dict, options: ConstraintPlanningOptions, place_inputs=(), *, limit=20, preferred_names=()
) -> list[PreparedCandidate]:
    rows = [
        (row, kind)
        for key, kind in (("candidates", "attraction"), ("foods", "food"))
        for row in context.get(key) or []
        if isinstance(row, dict)
    ]
    # 用户明确选定的实体优先占用有限候选池；截图时间/金额不升级为官方事实。
    imported = []
    for place in place_inputs:
        matching = next(
            (
                raw
                for raw, _kind in rows
                if str(raw.get("poi_id") or raw.get("place_id") or raw.get("id") or "") == place.entity_id
                and raw.get("country_code") == place.country_code
            ),
            {},
        )
        # Explicit confirmation binds this exact name to the chosen entity. A same-name research hit
        # must not satisfy its fixed/required constraint with a different entity ID.
        rows = [
            (raw, kind)
            for raw, kind in rows
            if raw is not matching
            and norm_ws_key(str(raw.get("name") or raw.get("poi_name") or "")) != norm_ws_key(place.name)
        ]
        imported.append(
            (
                {
                    **matching,
                    "name": place.name,
                    "poi_id": place.entity_id,
                    "source": matching.get("source") or place.identity_source,
                    "aliases": list(
                        dict.fromkeys([place.local_name, *place.aliases, *(matching.get("aliases") or [])])
                    ),
                    "country_code": place.country_code,
                    "latitude": place.latitude,
                    "longitude": place.longitude,
                    "address": place.address or matching.get("address") or place.city,
                    "source_url": place.source_url,
                    "source_fetched_at": place.retrieved_at,
                    "source_language": "multilingual",
                    **(
                        {
                            "cost": float(place.original_amount) if place.original_amount is not None else None,
                            "cost_currency": place.original_currency,
                        }
                        if not matching
                        else {}
                    ),
                },
                place.item_type,
            )
        )
    rows = imported + sorted(
        rows, key=lambda pair: str(pair[0].get("name") or pair[0].get("poi_name") or "") not in preferred_names
    )
    prepared, identities = [], set()
    for raw, kind in rows:
        name = str(raw.get("name") or raw.get("poi_name") or "").strip()
        country = str(raw.get("country_code") or "").upper()
        if not name or (country and country != options.country_code):
            continue
        # 显式实体 ID 才去重；同名不同馆区/实体保留并要求消歧。
        identity = (str(raw.get("source") or ""), str(raw.get("poi_id") or raw.get("place_id") or raw.get("id") or ""))
        if identity[1] and identity in identities:
            continue
        identities.add(identity)
        amount, currency = _amount(raw, kind)
        source = str(raw.get("source") or "unknown")
        evidence = _evidence(raw)
        duration = raw.get("duration_min")
        gaps = []
        if not isinstance(duration, int) or not 1 <= duration <= 480:
            duration = 90 if kind == "attraction" else 60
            gaps.append("访问时长采用规划假设，待确认")
        elif not evidence.get("duration_min") or evidence["duration_min"].verification_status == "unverified":
            gaps.append("访问时长为规划假设，尚未核实")
        if not country:
            gaps.append("地点国家未由来源确认")
        if not evidence.get("cost") or evidence["cost"].verification_status == "unverified":
            gaps.append("费用尚未核实")
        if not evidence.get("open_time") or evidence["open_time"].verification_status == "unverified":
            gaps.append("营业资料尚未核实")
        key = f"cp-{len(prepared)}"
        item = TripItem(
            item_type=kind,
            poi_name=name,
            poi_id=key,
            address=raw.get("address"),
            latitude=raw.get("latitude"),
            longitude=raw.get("longitude"),
            duration_min=duration,
            cost=amount,
            cost_currency=currency,
            open_time=raw.get("open_time"),
            source=source,
            image=raw.get("image"),
            fact_evidence=evidence,
        )
        fact = PlanningFactContext(
            candidate_id=key,
            name=name,
            aliases=[str(value) for value in raw.get("aliases") or []],
            source=source,
            source_entity_id=identity[1] or None,
            source_url=raw.get("source_url"),
            source_language=raw.get("source_language"),
            retrieved_at=raw.get("source_fetched_at"),
            country_code=country or None,
            original_currency=str(raw.get("cost_currency") or raw.get("price_currency") or raw.get("currency") or "")
            or None,
            original_amount=amount,
        )
        prepared.append(PreparedCandidate(item, raw, fact, gaps))
        if len(prepared) == limit:
            break
    return prepared


def _opening_windows(candidate: PreparedCandidate, local_date: date, duration_min: int | None = None):
    fact = candidate.item.fact_evidence.get("open_time")
    hours = fact.opening_hours if fact else None
    if hours is None and candidate.item.open_time:
        hours = parse_opening_rules(candidate.item.open_time)
    if hours is None or hours.status != "known":
        return None
    if (hours.valid_from and local_date < hours.valid_from) or (hours.valid_to and local_date > hours.valid_to):
        return None
    rules = [r for r in hours.rules if r.weekdays is None or local_date.weekday() in r.weekdays]
    if any(r.exceptions for r in rules):
        return None
    if any(r.closed and not r.intervals for r in rules):
        return ()
    # 部分时段关闭和跨午夜尚未精确编译，保留未知而非漏掉闭馆条件。
    if any(r.closed or any(i.end < i.start for i in r.intervals) for r in rules):
        return None
    duration = duration_min or candidate.item.duration_min or 90
    windows = []
    for rule in rules:
        for interval in rule.intervals:
            end = 1440 if interval.end == "24:00" else minute(interval.end)
            if rule.last_admission:
                end = min(end, minute(rule.last_admission) - 1 + duration)
            if end > minute(interval.start):
                windows.append((minute(interval.start), end))
    # 未匹配星期不代表全天关闭：来源可能只覆盖部分日期。
    return tuple(windows) if windows else None


def planning_candidate(
    candidate: PreparedCandidate,
    options: ConstraintPlanningOptions,
    local_date: date,
    persons: int,
    *,
    duration_min: int | None = None,
) -> PlanningCandidate:
    item = candidate.item
    # 费用约束用全体出行人的已列活动费用；正式条目仍保存每人原单价。
    price = money_minor(Decimal(str(item.cost)) * persons) if item.cost is not None else None
    categories = candidate.raw.get("categories")
    return PlanningCandidate(
        candidate_id=candidate.fact.candidate_id,
        name=item.poi_name,
        duration_min=duration_min or item.duration_min or 90,
        price_minor=price,
        currency=item.cost_currency or "UNKNOWN",
        opening_windows=_opening_windows(candidate, local_date, duration_min),
        item_type=item.item_type,
        categories=tuple(categories) if isinstance(categories, (list, tuple)) else None,
        utility=max(0, min(int(candidate.raw.get("utility", 1)), 1000)),
        source=item.source or "unknown",
        aliases=tuple(candidate.fact.aliases),
    )


def align_requirement_names(requirements, candidates: list[PreparedCandidate]):
    """只投影到唯一名称/别名，统一排版撇号；不剥馆区后缀或猜跨脚本同一性。"""
    req = requirements.model_copy(deep=True)
    aliases: dict[str, set[str]] = {}

    def key(name):
        return norm_ws_key(str(name).translate(str.maketrans({"’": "'", "‘": "'", "ʼ": "'"})))

    for candidate in candidates:
        for name in [candidate.item.poi_name, *(candidate.raw.get("aliases") or [])]:
            aliases.setdefault(key(name), set()).add(candidate.item.poi_name)

    def align(name):
        found = aliases.get(key(name), set())
        if len(found) != 1:
            return name
        canonical = next(iter(found))
        # 终检消费原始需求；将已唯一对齐的排版变体带入身份别名，避免CP有解而终检误报缺失。
        for candidate in candidates:
            if candidate.item.poi_name == canonical and name not in candidate.fact.aliases:
                candidate.fact.aliases.append(name)
        return canonical

    for place in req.required_places:
        place.name = align(place.name)
    for fixed in req.fixed_arrangements:
        fixed.name = align(fixed.name)
    for relation in req.place_relations:
        relation.place_names = [align(name) for name in relation.place_names]
    req.excluded_places = [align(name) for name in req.excluded_places]
    return req
