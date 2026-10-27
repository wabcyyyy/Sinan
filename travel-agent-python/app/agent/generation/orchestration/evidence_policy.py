"""Decision-sensitive query choice: certify selected facts and probe joint unknowns without adopting assumptions."""

from dataclasses import replace
from itertools import pairwise

from app.agent.generation.content.evidence_gaps import collect_gaps, decision_signature, with_assumption
from app.schemas.active_evidence import EvidenceProbe


def certificate_complete(problems, results, prepared):
    if any(result.status not in {"FEASIBLE", "OPTIMAL"} for result in results):
        return False
    selected = {v.candidate_id for result in results for v in result.visits}
    if any(candidate.gaps for candidate in prepared if candidate.fact.candidate_id in selected):
        return False
    return all(
        problem.route_matrix.get((a.candidate_id, b.candidate_id), {}).get("source") in {"osrm", "openrouteservice"}
        for problem, result in zip(problems, results, strict=True)
        for a, b in pairwise(result.visits)
    )


def _selected(gap, problems, results):
    for problem, result in zip(problems, results, strict=True):
        if problem.day_no != gap.day_no:
            continue
        if gap.kind == "route":
            return (gap.first_id, gap.second_id) in {
                (a.candidate_id, b.candidate_id) for a, b in pairwise(result.visits)
            }
        return gap.first_id in {v.candidate_id for v in result.visits}
    return False


def _joint(problems, gaps, high, budget):
    current = problems
    for gap in gaps:
        current = with_assumption(current, gap, high, budget=budget)
    # Cover unselected optional edges too; absence must not make the low envelope restrictive.
    bounded = []
    for problem in current:
        matrix = dict(problem.route_matrix)
        for first in problem.candidates:
            for second in problem.candidates:
                if first.candidate_id == second.candidate_id:
                    continue
                key = first.candidate_id, second.candidate_id
                original = next(p for p in problems if p.day_no == problem.day_no).route_matrix.get(key)
                if not original or original.get("source") not in {"osrm", "openrouteservice"}:
                    matrix[key] = {"duration_min": 1440 if high else 0, "source": "joint-assumption"}
        bounded.append(replace(problem, route_matrix=matrix))
    return bounded


def infeasibility_proof(problems, result, prepared, run, budget):
    gaps = collect_gaps(problems, result, prepared, limit=10000)
    if any(gap.kind == "duration" for gap in gaps):
        return False  # Fixed-duration extremes are not a relaxation of the activity-minimum constraint.
    low = run(_joint(problems, gaps, False, budget))
    return decision_signature(low)[0] == "INFEASIBLE"


def choose_decision_gap(problems, result, prepared, affordable, run, report, budget):
    all_gaps = collect_gaps(problems, result, prepared, limit=10000)
    low, high = run(_joint(problems, all_gaps, False, budget)), run(_joint(problems, all_gaps, True, budget))
    low_sig, high_sig = decision_signature(low), decision_signature(high)
    if len(report.probes) < 48 and affordable:
        first = affordable[0]
        report.probes.append(
            EvidenceProbe(
                gap_id="joint:remaining-unknowns",
                kind=first.kind,
                targets=sorted({name for gap in all_gaps for name in gap.names}),
                importance=first.importance,
                cost_units=sum(g.cost_units for g in all_gaps),
                fact_state="hypothesis_only",
                low_status=low_sig[0],
                high_status=high_sig[0],
                sensitivity=int(low_sig != high_sig),
                score=0,
                reason="联合未知界探测；不采纳假设；时长界与充实度耦合时不作无解证明",
            )
        )
    # Duration bounds interact with the ordinary-day activity minimum; they are not a proof envelope.
    if low_sig[0] == "INFEASIBLE" and not any(g.kind == "duration" for g in all_gaps):
        report.stop_reason = "optimistic_infeasible"
        return None
    scores = []
    for gap in affordable:
        a = decision_signature(run(with_assumption(problems, gap, False, budget=budget)))
        b = decision_signature(run(with_assumption(problems, gap, True, budget=budget)))
        undecided = "UNKNOWN" in {a[0], b[0]}
        sensitivity = 0 if undecided else 3 if a[0] != b[0] else int(a[1] != b[1])
        certificate = _selected(gap, problems, result)
        joint_sensitive = low_sig != high_sig
        value = max(sensitivity, 2 if certificate else 1 if joint_sensitive else 0)
        score = value * gap.importance / gap.cost_units
        if len(report.probes) < 48:
            report.probes.append(
                EvidenceProbe(
                    gap_id=gap.gap_id,
                    kind=gap.kind,
                    targets=list(gap.names),
                    importance=gap.importance,
                    cost_units=gap.cost_units,
                    fact_state=gap.fact_state,
                    low_status=a[0],
                    high_status=b[0],
                    sensitivity=value,
                    score=score,
                    reason="选中安排的交付证据缺口" if certificate else "单字段/联合未知决策比较；假设不作证据",
                )
            )
        if value:
            scores.append((score, certificate, gap.importance, gap.gap_id, gap))
    if not scores:
        report.stop_reason = "probe_unknown" if "UNKNOWN" in {low_sig[0], high_sig[0]} else "decision_stable"
        return None
    return max(scores, key=lambda row: row[:4])[-1]
