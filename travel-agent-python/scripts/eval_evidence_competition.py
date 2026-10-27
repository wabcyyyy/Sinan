"""两个约束模板的完整2×2隐藏状态；竞争缺口配平预算，不按结果调策略。"""

import itertools
import json
import sys
import time
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.agent.generation.content.constraint_trip_solver import solve_trip
from app.agent.generation.content.evidence_gaps import decision_signature
from app.agent.generation.content.evidence_observations import FactObservation
from app.agent.generation.orchestration.active_evidence import drive_evidence
from app.schemas.trip import FactEvidence
from scripts.align_p4_scenario import local_only
from scripts.run_p4_acceptance import save
from tests.active_evidence_templates import environment, evidence


def cases():
    rows = []
    for closed, expensive in itertools.product((False, True), repeat=2):
        rows.append(
            {
                "id": f"choice-{int(closed)}-{int(expensive)}",
                "template": "choice",
                "split": "development",
                "hidden": {"a_closed": closed, "price_a": 120 if expensive else 20},
            }
        )
    for closed, long_route in itertools.product((False, True), repeat=2):
        rows.append(
            {
                "id": f"departure-{int(closed)}-{int(long_route)}",
                "template": "departure",
                "split": "evaluation",
                "hidden": {"a_closed": closed, "route_minutes": 90 if long_route else 15},
            }
        )
    return rows


def observed_environment(template):
    # 不接收隐藏状态；只加一个 A 营业缺口，与已有价格/路线缺口竞争同一预算。
    request, prepared, problems = environment(
        {
            "kind": template,
            "country": "PT" if template == "choice" else "TH",
            "city": "Lisboa" if template == "choice" else "Bangkok",
            "zone": "Europe/Lisbon" if template == "choice" else "Asia/Bangkok",
            "currency": "EUR" if template == "choice" else "THB",
        }
    )
    target = prepared[0]
    target.item.fact_evidence.pop("open_time")
    target.gaps.append("营业资料尚未核实")
    problems[0] = replace(
        problems[0],
        candidates=tuple(replace(c, opening_windows=None) if c.name == "A" else c for c in problems[0].candidates),
    )
    return request, prepared, problems


def truth_check(case, results, names):
    hidden = case["hidden"]
    visits = results[0].visits
    chosen = [names[v.candidate_id] for v in visits]
    numerical = decision_signature(results)[0]
    if case["template"] == "choice":
        expected = ["B"] if hidden["a_closed"] or hidden["price_a"] > 100 else ["A"]
        valid = (
            bool(visits)
            and len(visits) == 1
            and all(v.start_min >= 540 and v.end_min <= 780 and v.end_min - v.start_min == 180 for v in visits)
        )
        valid = valid and not ("A" in chosen and (hidden["a_closed"] or hidden["price_a"] > 100))
        return {
            "correct": valid and chosen == expected,
            "plan_valid": valid,
            "false_feasible_trial": numerical == "FEASIBLE" and not valid,
            "expected": expected,
            "expected_feasible": True,
        }
    feasible = not hidden["a_closed"] and 120 + 120 + 60 + hidden["route_minutes"] <= 360
    correct = (
        numerical == "INFEASIBLE"
        if not feasible
        else chosen == ["A", "B", "C"] and all(v.start_min >= 540 and v.end_min <= 900 for v in visits)
    )
    if feasible and correct:
        correct = (
            visits[1].start_min >= visits[0].end_min
            and visits[2].start_min >= visits[1].end_min + hidden["route_minutes"]
        )
    return {
        "correct": correct,
        "plan_valid": feasible and correct,
        "false_feasible_trial": numerical == "FEASIBLE" and not (feasible and correct),
        "expected": "FEASIBLE" if feasible else "INFEASIBLE",
        "expected_feasible": feasible,
    }


def evaluate(output):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    frozen = cases()
    save(
        output / "design.json",
        {
            "cases": frozen,
            "max_queries": 2,
            "budget_units": 2,
            "arms": {"fixed": [0], "random": [0, 1, 2], "confidence": [0], "bottleneck": [0]},
            "split": "choice vs departure templates; full factorial hidden states; author-visible, no current tuning",
            "scope": "frozen-only; conditional trial error is not a claim that product confirmed unknown facts",
        },
    )
    rows = []
    started = time.monotonic()
    with local_only():
        for case in frozen:
            for strategy, seeds in (("fixed", [0]), ("random", [0, 1, 2]), ("confidence", [0]), ("bottleneck", [0])):
                for seed in seeds:
                    if time.monotonic() - started > 45:
                        raise TimeoutError("本批45秒实验预算已耗尽，禁止当作通过")
                    request, prepared, problems = observed_environment(case["template"])
                    assert request.requirements_struct is not None
                    assert request.requirements_struct.constraint_planning is not None
                    assert request.requirements_struct.constraint_planning.active_evidence is not None
                    names = {c.candidate_id: c.name for c in problems[0].candidates}
                    initial = solve_trip(problems, budget=Decimal(100), time_limit_s=0.25)
                    initial_truth = truth_check(case, initial, names)
                    calls = []

                    def observe(gap, current, candidates, req, options, *, case=case, calls=calls):
                        ids = tuple(
                            next(p.fact.source_entity_id for p in candidates if p.fact.candidate_id == key)
                            for key in (gap.first_id, gap.second_id)
                            if key
                        )
                        fact = FactEvidence.model_validate(evidence())
                        calls.append({"kind": gap.kind, "targets": list(gap.names)})
                        if gap.kind == "price":
                            return FactObservation(
                                "price",
                                ids,
                                value=case["hidden"]["price_a"],
                                currency=req.requirements_struct.constraint_planning.currency,
                                fact=fact,
                                tool="frozen_price",
                            )
                        if gap.kind == "opening":
                            fact = FactEvidence.model_validate(evidence(opening=True))
                            if case["hidden"]["a_closed"]:
                                assert fact.opening_hours is not None
                                fact.opening_hours.rules[0].closed = True
                                fact.opening_hours.rules[0].intervals = []
                            return FactObservation("opening", ids, fact=fact, tool="frozen_opening")
                        route = {
                            "from_poi_id": gap.first_id,
                            "to_poi_id": gap.second_id,
                            "source": "osrm",
                            "duration_min": case["hidden"]["route_minutes"],
                            "time_dependent": False,
                            "source_url": "https://example.com/frozen-template-route",
                        }
                        return FactObservation("route", ids, route=route, fact=fact, tool="frozen_route")

                    tick = time.monotonic()
                    results, report = drive_evidence(
                        problems,
                        prepared,
                        request,
                        options=request.requirements_struct.constraint_planning.active_evidence,
                        solve=lambda current, seconds: solve_trip(current, budget=Decimal(100), time_limit_s=seconds),
                        observe=observe,
                        budget=Decimal(100),
                        initial=initial,
                        strategy=strategy,
                        seed=seed,
                    )
                    truth = truth_check(case, results, names)
                    rows.append(
                        {
                            "case_id": case["id"],
                            "template": case["template"],
                            "split": case["split"],
                            "strategy": strategy,
                            "seed": seed,
                            "initial_truth": initial_truth,
                            "truth": truth,
                            "fact_status": results[0].status,
                            "decision": decision_signature(results),
                            "selected": [names[v.candidate_id] for v in results[0].visits],
                            "calls": calls,
                            "report": report.model_dump(mode="json"),
                            "elapsed_seconds": time.monotonic() - tick,
                        }
                    )
    summary = []
    for template, strategy in itertools.product(
        ("choice", "departure"), ("fixed", "random", "confidence", "bottleneck")
    ):
        group = [r for r in rows if r["template"] == template and r["strategy"] == strategy]
        summary.append(
            {
                "template": template,
                "strategy": strategy,
                "states": 4,
                "runs": len(group),
                "correct": sum(r["truth"]["correct"] for r in group),
                "false_feasible_trials": sum(r["truth"]["false_feasible_trial"] for r in group),
                "corrected_initial_error": sum(
                    not r["initial_truth"]["correct"] and r["truth"]["correct"] for r in group
                ),
                "queries": sum(len(r["calls"]) for r in group),
                "units": sum(r["report"]["spent_units"] for r in group),
                "changed_decisions": sum(q["decision_changed"] for r in group for q in r["report"]["queries"]),
                "seconds": sum(r["elapsed_seconds"] for r in group),
                "model_calls": 0,
                "tokens": 0,
                "paid_cny": "0",
            }
        )
    result = {
        "rows": rows,
        "summary": summary,
        "elapsed_seconds": time.monotonic() - started,
        "scope": "two templates, eight truth states; repeats/derived states are not independent tasks",
    }
    save(output / "report.json", result)
    return result


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    print(json.dumps(evaluate(args.output)["summary"], ensure_ascii=False))
