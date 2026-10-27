"""固定事实内核对照：真实单次LLM排程/同输出贪心修时/CP-SAT，独立穷举判真值。"""

import argparse
import json
import time
from dataclasses import asdict, replace
from decimal import Decimal
from pathlib import Path

from app.agent.generation.content.constraint_problem import DayPlanningProblem, PlanningCandidate, ScheduledVisit
from app.agent.generation.content.constraint_solver import solve_day
from app.schemas.trip_requirements import PlaceRelation, TripRequirements
from scripts.run_p4_acceptance import save
from scripts.sinan_night_budget import NightBudget, NightClient
from tests.constraint_planner_cases import frozen_problem
from tests.constraint_planner_oracle import exhaustive_day, objective, schedule_violations

SPECS = [
    ("booking", 41, "development"),
    ("choice", 43, "development"),
    ("split_opening", 101, "evaluation"),
    ("asymmetric_route", 103, "evaluation"),
    ("tight_budget", 107, "evaluation"),
    ("excluded_choice", 109, "evaluation"),
    ("fixed_conflict", 113, "evaluation"),
    ("order_cycle", 127, "evaluation"),
    ("zero_budget", 131, "evaluation"),
    ("unknown_price", 137, "evaluation"),
    ("unknown_opening", 139, "evaluation"),
    ("unknown_route", 149, "evaluation"),
]
SYSTEM = (
    "Schedule a single local day using only the supplied frozen candidate facts and hard requirements. "
    "This is a synthetic kernel task, not a full travel day product. All money is minor units in the stated currency. "
    "Visit durations are fixed; use directed travel minutes between successive visits; honor all opening intervals, "
    "day bounds, fixed bookings, required/excluded places, before/one_of relations, max attractions and hard budget. "
    "Optional candidates may be omitted. Optimize lexicographically: soft relations by priority, then total utility, "
    "then less travel, then earlier starts. Missing mandatory price/opening/route facts cannot be assumed verified. "
    'Return JSON only: {"status":"FEASIBLE|INFEASIBLE|UNKNOWN","visits":['
    '{"candidate_id":"p0","start_min":600,"end_min":660}],"reason":"short reason"}. '
    "Minutes are integers after midnight. FEASIBLE needs a valid complete schedule; INFEASIBLE means impossible "
    "within this candidate pool; UNKNOWN means important facts are missing. Never invent facts or claim optimality."
)


def problem_for(name, seed):
    p = frozen_problem(size=5, seed=seed)
    req = p.requirements.model_copy(deep=True)
    candidates = list(p.candidates)
    matrix = dict(p.route_matrix)
    budget = p.budget
    if name == "split_opening":
        candidates[1] = replace(candidates[1], opening_windows=((700, 780), (840, 1020)))
    elif name == "asymmetric_route":
        matrix["p0", "p1"] = {**matrix["p0", "p1"], "duration_min": 55}
        matrix["p1", "p0"] = {**matrix["p1", "p0"], "duration_min": 5}
    elif name == "tight_budget":
        candidates = [replace(c, price_minor=v) for c, v in zip(candidates, (1000, 1000, 500, 1500, 1000), strict=True)]
        budget = Decimal("25")
    elif name == "excluded_choice":
        req.excluded_places = ["地点3"]
    elif name == "fixed_conflict":
        candidates[0] = replace(candidates[0], opening_windows=((540, 585),))
    elif name == "order_cycle":
        req.place_relations.append(
            PlaceRelation(constraint_id="cycle", kind="before", day_no=1, place_names=["地点1", "地点0"])
        )
    elif name == "zero_budget":
        budget = Decimal(0)
    elif name == "unknown_price":
        candidates[0] = replace(candidates[0], price_minor=None)
    elif name == "unknown_opening":
        candidates[0] = replace(candidates[0], opening_windows=None)
    elif name == "unknown_route":
        matrix = {}
    return replace(p, requirements=req, candidates=tuple(candidates), route_matrix=matrix, budget=budget)


def serialize(p):
    return {
        "requirements": p.requirements.model_dump(mode="json", by_alias=True),
        "candidates": [asdict(c) for c in p.candidates],
        "currency": p.currency,
        "budget_minor": int(p.budget * 100),
        "day_no": p.day_no,
        "directed_routes": [{"from": a, "to": b, "minutes": v["duration_min"]} for (a, b), v in p.route_matrix.items()],
    }


def deserialize(value):
    return DayPlanningProblem(
        requirements=TripRequirements.model_validate(value["requirements"]),
        candidates=tuple(PlanningCandidate(**c) for c in value["candidates"]),
        route_matrix={
            (r["from"], r["to"]): {"duration_min": r["minutes"], "source": "frozen-directed-fixture"}
            for r in value["directed_routes"]
        },
        currency=value["currency"],
        day_no=value["day_no"],
        budget=Decimal(value["budget_minor"]) / 100,
    )


def prepare(root):
    target = root / "planning-baseline-design.json"
    if target.exists():
        raise ValueError("已冻结，禁止重制题目或覆盖标签")
    rows = []
    for name, seed, split in SPECS:
        p = problem_for(name, seed)
        visits, score, examined = exhaustive_day(p)
        expected = "UNKNOWN" if name.startswith("unknown_") else "INFEASIBLE" if visits is None else "FEASIBLE"
        result = solve_day(p, time_limit_s=1)
        errors = schedule_violations(p, result.visits) if result.visits else []
        assert not errors and (
            result.status == "UNKNOWN"
            if expected == "UNKNOWN"
            else result.status == "INFEASIBLE"
            if expected == "INFEASIBLE"
            else result.status in {"FEASIBLE", "OPTIMAL"}
        ), (name, result, errors)
        rows.append(
            {
                "id": name,
                "split": split,
                "seed": seed,
                "facts": serialize(p),
                "expected": expected,
                "oracle": {"examined": examined, "schedule": [asdict(v) for v in visits or []], "objective": score},
                "cp_sat": {
                    "status": result.status,
                    "visits": [asdict(v) for v in result.visits],
                    "checker_errors": errors,
                    "elapsed_s": result.elapsed_s,
                    "conflicts": result.conflict_ids,
                },
            }
        )
    save(
        target,
        {
            "task": "fixed-fact kernel baseline, not old deployed product or full-day business gate",
            "system_prompt": SYSTEM,
            "repetitions": 3,
            "temperature": 0.3,
            "max_tokens": 1536,
            "human_reference_review": "not performed",
            "cases": rows,
        },
    )
    print(json.dumps({r["id"]: r["expected"] for r in rows}))


def greedy_schedule(p, visits):
    """只修给定顺序的最早时刻，不搜索子集，不知道真值或调用CP/穷举。"""
    by_id = {c.candidate_id: c for c in p.candidates}
    windows = [w for w in p.requirements.day_windows if w.day_no == p.day_no]

    def minute(t):
        return int(t[:2]) * 60 + int(t[3:])

    current = max([minute(w.not_before) for w in windows if w.not_before] or [480])
    result, previous = [], None
    for visit in visits:
        c = by_id[visit.candidate_id]
        if previous:
            current += p.route_matrix.get((previous, c.candidate_id), {}).get("duration_min", 0)
        fixed = next((f for f in p.requirements.fixed_arrangements if f.day_no == p.day_no and f.name == c.name), None)
        if fixed:
            start = minute(fixed.start_time)
            if start < current:
                return []
        else:
            possible = [
                max(current, low)
                for low, high in c.opening_windows or ((0, 1440),)
                if max(current, low) + c.duration_min <= high
            ]
            if not possible:
                return []
            start = min(possible)
        result.append(ScheduledVisit(c.candidate_id, start, start + c.duration_min))
        current, previous = start + c.duration_min, c.candidate_id
    return result


def assess(case, raw):
    p = deserialize(case["facts"])
    try:
        data = json.loads(raw)
        status = data["status"]
        visits = [ScheduledVisit(**v) for v in data.get("visits", [])]
        if status not in {"FEASIBLE", "INFEASIBLE", "UNKNOWN"} or any(
            type(v.start_min) is not int or type(v.end_min) is not int for v in visits
        ):
            raise ValueError("无效状态或时间类型")
        errors = schedule_violations(p, visits) if status == "FEASIBLE" else []
        repaired = (
            greedy_schedule(p, visits)
            if status == "FEASIBLE"
            and not any(v.candidate_id not in {c.candidate_id for c in p.candidates} for v in visits)
            else visits
        )
        greedy_errors = schedule_violations(p, repaired) if status == "FEASIBLE" else []
        correct_status = status == case["expected"]
        return {
            "status": status,
            "visits": [asdict(v) for v in visits],
            "checker_errors": errors,
            "correct": correct_status and not errors,
            "greedy_visits": [asdict(v) for v in repaired],
            "greedy_checker_errors": greedy_errors,
            "greedy_correct": correct_status and not greedy_errors,
            "objective": objective(p, visits) if status == "FEASIBLE" and not errors else None,
            "reason": data.get("reason", ""),
            "false_confirmed_feasible": status == "FEASIBLE" and (case["expected"] != "FEASIBLE" or bool(errors)),
        }
    except (ValueError, TypeError, KeyError, AttributeError) as exc:
        return {"status": "INVALID", "correct": False, "greedy_correct": False, "error": str(exc)}


def execute(root, repetition):
    from app.common.model_registry import binding

    design = json.loads((root / "planning-baseline-design.json").read_text("utf-8"))
    output = root / f"planning-baseline-repeat-{repetition}.json"
    client = NightClient(NightBudget(root / "budget.json"), root / "models", binding("main"))
    report = {"repetition": repetition, "results": [], "mode": "real model with frozen synthetic kernel facts"}
    started = time.monotonic()
    if output.exists():
        raise ValueError("该批已完成，禁止覆盖")
    for case in design["cases"]:
        if time.monotonic() - started > 64:
            raise TimeoutError("本批64秒已达，仅保存进度，不再发请求")
        client.identifier = f"planning-baseline-{case['id']}-{repetition}"
        raw = client.complete(
            json.dumps(case["facts"], ensure_ascii=False),
            system_prompt=design["system_prompt"],
            temperature=design["temperature"],
            max_tokens=design["max_tokens"],
        )
        report["results"].append(
            {
                "case": case["id"],
                "expected": case["expected"],
                "split": case["split"],
                "request_id": client.identifier,
                "assessment": assess(case, raw),
            }
        )
        save(root / f"planning-baseline-repeat-{repetition}-partial.json", report)
    report["elapsed_s"] = round(time.monotonic() - started, 3)
    report["total_peak_upper_cny"] = str(client.ledger.total())
    save(output, report)
    print(
        json.dumps(
            {
                "repetition": repetition,
                "correct": sum(r["assessment"]["correct"] for r in report["results"]),
                "greedy_correct": sum(r["assessment"]["greedy_correct"] for r in report["results"]),
                "elapsed_s": report["elapsed_s"],
                "cost_upper": report["total_peak_upper_cny"],
            }
        )
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--prepare", action="store_true")
    parser.add_argument("--repetition", type=int, choices=range(3))
    parser.add_argument("--approved", action="store_true")
    args = parser.parse_args()
    if args.prepare:
        prepare(args.root)
    elif args.approved and args.repetition is not None:
        execute(args.root, args.repetition)
    else:
        raise SystemExit("真实调用需要授权和明确重复编号")
