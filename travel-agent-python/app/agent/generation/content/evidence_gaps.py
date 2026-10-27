"""从现有求解快照构造字段缺口与单字段假设，不改用户硬约束。"""

from dataclasses import dataclass, replace
from datetime import UTC, datetime
from itertools import pairwise

from app.agent.core.poi_identity import norm_ws_key


@dataclass(frozen=True)
class EvidenceGap:
    gap_id: str
    kind: str
    day_no: int
    first_id: str
    second_id: str | None
    names: tuple[str, ...]
    importance: int
    cost_units: int
    fact_state: str = "missing"


def _stale(fact):
    if fact and fact.freshness_status == "stale":
        return True
    if fact and fact.expires_at:
        try:
            expiry = datetime.fromisoformat(fact.expires_at.replace("Z", "+00:00"))
            return expiry.tzinfo is None or expiry <= datetime.now(UTC)
        except ValueError:
            return True
    return False


def invalidate_expired(problems, prepared):
    """只撤销过期字段的数值可用性，保留原始金额和来源供核对。"""
    changed = False
    for target in prepared:
        for field, label in (("open_time", "营业"), ("cost", "费用")):
            if not _stale(target.item.fact_evidence.get(field)):
                continue
            changed = True
            target.gaps.append(f"{label}证据已过期，原值保留供核对")
            for index, problem in enumerate(problems):
                candidates = tuple(
                    replace(c, **({"opening_windows": None} if field == "open_time" else {"price_minor": None}))
                    if c.candidate_id == target.fact.candidate_id
                    else c
                    for c in problem.candidates
                )
                problems[index] = replace(problem, candidates=candidates)
    return changed


def _importance(problem, candidate):
    req = problem.requirements
    names = {norm_ws_key(n) for n in (candidate.name, *candidate.aliases)}
    if any(norm_ws_key(f.name) in names for f in req.fixed_arrangements):
        return 5
    if any(norm_ws_key(p.name) in names for p in req.required_places):
        return 4
    if any(any(norm_ws_key(n) in names for n in r.place_names) and r.strength == "hard" for r in req.place_relations):
        return 3
    return 1


def collect_gaps(problems, results, prepared=(), *, limit=8, excluded=()):
    gaps = {}
    assumed_duration = {
        p.fact.candidate_id for p in prepared if any("时长" in gap and "用户调整" not in gap for gap in p.gaps)
    }
    unconfirmed = {p.fact.candidate_id: p.gaps for p in prepared}
    facts = {p.fact.candidate_id: p.item.fact_evidence for p in prepared}
    for problem, result in zip(problems, results, strict=True):
        selected = {v.candidate_id for v in result.visits}
        for candidate in problem.candidates:
            importance = _importance(problem, candidate)
            fields = []
            missing = unconfirmed.get(candidate.candidate_id, ())
            booked_price = any(
                f.name == candidate.name and f.cost is not None for f in problem.requirements.fixed_arrangements
            )
            if candidate.opening_windows is None or any("营业" in g for g in missing):
                fields.append(("opening", 2))
            if (
                (
                    candidate.price_minor is None
                    or candidate.currency != problem.currency
                    or any("费用" in g for g in missing)
                )
                and problem.requirements.budget_policy
                and problem.requirements.budget_policy.mode == "hard_cap"
                and not booked_price
            ):
                fields.append(("price", 1))
            if candidate.candidate_id in assumed_duration and importance != 5:
                fields.append(("duration", 1))
            for kind, cost in fields:
                key = f"{kind}:{problem.day_no}:{candidate.candidate_id}"
                fact = facts.get(candidate.candidate_id, {}).get(
                    {"opening": "open_time", "price": "cost", "duration": "duration_min"}[kind]
                )
                state = "stale" if _stale(fact) else "unverified" if fact else "missing"
                if kind == "price" and candidate.currency != problem.currency:
                    state = "foreign_currency"
                gaps[key] = EvidenceGap(
                    key,
                    kind,
                    problem.day_no,
                    candidate.candidate_id,
                    None,
                    (candidate.name,),
                    importance + int(candidate.candidate_id in selected),
                    cost,
                    state,
                )
        # 先取当前路线上的边；尚无解时取与硬要求有关的边，最多检查有限候选。
        pairs = list(pairwise(result.visits))
        edges = [(a.candidate_id, b.candidate_id) for a, b in pairs]
        for first in problem.candidates:
            for second in problem.candidates:
                if (
                    first.candidate_id != second.candidate_id
                    and max(_importance(problem, first), _importance(problem, second)) > 1
                ):
                    edges.append((first.candidate_id, second.candidate_id))
        by_id = {c.candidate_id: c for c in problem.candidates}
        for first, second in dict.fromkeys(edges):
            route = problem.route_matrix.get((first, second))
            if route and route.get("source") in {"osrm", "openrouteservice"}:
                continue
            key = f"route:{problem.day_no}:{first}:{second}"
            gaps[key] = EvidenceGap(
                key,
                "route",
                problem.day_no,
                first,
                second,
                (by_id[first].name, by_id[second].name),
                max(_importance(problem, by_id[first]), _importance(problem, by_id[second])) + 1,
                1,
            )
    return sorted(
        (g for g in gaps.values() if g.gap_id not in excluded), key=lambda g: (-g.importance, g.cost_units, g.gap_id)
    )[:limit]


def with_assumption(problems, gap, high, *, budget=None):
    changed = []
    for problem in problems:
        if problem.day_no != gap.day_no:
            changed.append(problem)
            continue
        matrix = dict(problem.route_matrix)
        candidates = []
        for candidate in problem.candidates:
            if candidate.candidate_id == gap.first_id:
                if gap.kind == "opening":
                    candidate = replace(candidate, opening_windows=() if high else ((0, 1440),))
                elif gap.kind == "price":
                    ceiling = int(float(budget or problem.budget or 10000) * 100) + 1
                    candidate = replace(candidate, price_minor=ceiling if high else 0, currency=problem.currency)
                elif gap.kind == "duration":
                    candidate = replace(candidate, duration_min=240 if high else 15)
            candidates.append(candidate)
        if gap.kind == "route":
            matrix[(gap.first_id, gap.second_id)] = {
                "duration_min": 240 if high else 0,
                "source": "sensitivity-assumption",
            }
        changed.append(replace(problem, candidates=tuple(candidates), route_matrix=matrix))
    return changed


def decision_signature(results):
    feasible = any(r.solver_status in {"FEASIBLE", "OPTIMAL"} or r.visits for r in results)
    status = "FEASIBLE" if feasible else results[0].status
    return status, tuple(tuple(v.candidate_id for v in r.visits) for r in results)
