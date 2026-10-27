"""字段证据更新的单一应用器：身份、币种、日期与来源不通过则维持未知。"""

from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from app.agent.generation.content.constraint_facts import _opening_windows
from app.agent.generation.content.constraint_problem import money_minor
from app.common import clock
from app.schemas.trip import FactEvidence


@dataclass
class FactObservation:
    kind: str
    entity_ids: tuple[str, ...]
    value: object = None
    currency: str | None = None
    fact: FactEvidence | None = None
    route: dict | None = None
    question: str | None = None
    tool: str = "unknown"
    binding_basis: str = "provider_entity_id"


def apply_observation(problems, prepared, gap, observation, request):
    targets = [
        next((p for p in prepared if p.fact.candidate_id == identifier), None)
        for identifier in (gap.first_id, gap.second_id)
        if identifier is not None
    ]
    if any(p is None for p in targets):
        return False, "缺少可核对实体的原候选"
    targets = [p for p in targets if p is not None]
    expected = tuple(p.fact.source_entity_id for p in targets)
    if not all(expected) or observation.entity_ids != expected or observation.kind != gap.kind:
        return False, "证据实体与本次候选不一致，拒绝更新"
    fact = observation.fact
    if fact is None or not fact.source_url or not fact.retrieved_at or fact.value_kind != "observed":
        return False, "来源、观测时间或已观测字段缺失"
    try:
        observed = datetime.fromisoformat(fact.retrieved_at.replace("Z", "+00:00"))
        if observed.tzinfo is None or observed > datetime.now(UTC) + timedelta(minutes=5):
            return False, "观测时间无时区或在未来"
    except ValueError:
        return False, "观测时间无效"
    if fact.expires_at:
        try:
            expiry = datetime.fromisoformat(fact.expires_at.replace("Z", "+00:00"))
            if expiry.tzinfo is None or expiry <= datetime.now(UTC):
                return False, "证据已过期或缺少到期时区"
        except ValueError:
            return False, "到期时间无效"
    if fact.verification_status == "unverified" or fact.freshness_status == "stale":
        return False, "证据未核实或已过期，不能解除未知"
    if gap.kind == "route":
        route = observation.route or {}
        duration = route.get("duration_min")
        if (
            route.get("source") not in {"osrm", "openrouteservice"}
            or type(duration) is not int
            or not 0 <= duration <= 1440
            or route.get("time_dependent")
        ):
            return False, "路线不是可用的静态路网来源"
        if route.get("from_poi_id") != gap.first_id or route.get("to_poi_id") != gap.second_id:
            return False, "有向路线端点不匹配"
        for i, problem in enumerate(problems):
            matrix = dict(problem.route_matrix)
            matrix[(gap.first_id, gap.second_id)] = dict(route)
            problems[i] = replace(problem, route_matrix=matrix)
        return True, "已更新有来源的有向路段"
    target = targets[0]
    start = request.start_date
    base = (
        date.fromisoformat(start)
        if start
        else clock.destination_today(request.requirements_struct.constraint_planning.time_zone)
    )
    if gap.kind == "opening" and (not fact.opening_hours or fact.opening_hours.status != "known"):
        return False, "营业证据仍不能解释为适用日期的规则"
    if gap.kind == "price" and (
        not observation.currency
        or len(observation.currency) != 3
        or not observation.currency.isascii()
        or not observation.currency.isupper()
        or not observation.currency.isalpha()
        or observation.value is None
    ):
        return False, "价格缺少原币金额"
    if gap.kind == "duration" and (type(observation.value) is not int or not 1 <= observation.value <= 480):
        return False, "访问时长缺少有效的整数分钟"
    if gap.kind == "opening":
        # 验证所有适用日后再原子采用，不让过期证据覆盖旧事实。
        probe = target.item.model_copy(deep=True)
        probe.fact_evidence["open_time"] = fact
        windows = {
            p.day_no: _opening_windows(replace(target, item=probe), base + timedelta(days=p.day_no - 1))
            for p in problems
        }
        if windows[gap.day_no] is None:
            local_date = base + timedelta(days=gap.day_no - 1)
            hours = fact.opening_hours
            assert hours is not None  # 上方已拒绝缺少营业规则的观测。
            if (hours.valid_from and local_date < hours.valid_from) or (hours.valid_to and local_date > hours.valid_to):
                return False, "营业证据不覆盖本次当地日期"
            matching = [r for r in hours.rules if r.weekdays is None or local_date.weekday() in r.weekdays]
            if any(r.exceptions for r in matching):
                return False, "本次当地日期涉及尚未确认的节假日或临时例外，营业状态仍未知"
            return False, "营业规则无法确定本次当地日期的可用时段，营业状态仍未知"
        target.item.fact_evidence["open_time"] = fact
    elif gap.kind == "price":
        amount = Decimal(str(observation.value))
        money_minor(amount)
        if amount > 1_000_000:
            return False, "金额超过候选字段上限，未采用"
        target.item.cost, target.item.cost_currency = float(amount), observation.currency
        target.fact.original_amount, target.fact.original_currency = float(amount), observation.currency
        target.item.fact_evidence["cost"] = fact
    else:
        target.item.duration_min = observation.value
        target.item.fact_evidence["duration_min"] = fact
    for i, problem in enumerate(problems):
        candidates = []
        for candidate in problem.candidates:
            if candidate.candidate_id == gap.first_id:
                if gap.kind == "opening":
                    candidate = replace(candidate, opening_windows=windows[problem.day_no])
                elif gap.kind == "price":
                    candidate = replace(
                        candidate, price_minor=money_minor(amount * request.persons), currency=observation.currency
                    )
                else:
                    fixed = [
                        f
                        for f in problem.requirements.fixed_arrangements
                        if f.day_no == problem.day_no and f.name == candidate.name
                    ]
                    if not fixed:
                        candidate = replace(candidate, duration_min=observation.value)
            candidates.append(candidate)
        problems[i] = replace(problem, candidates=tuple(candidates))
    labels = {"opening": "营业", "price": "费用", "duration": "时长"}
    target.gaps = [text for text in target.gaps if labels[gap.kind] not in text]
    return True, "字段证据已更新；其余未知与硬要求保持"
