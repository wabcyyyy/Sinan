import json

from scripts.eval_planning_baselines import assess, problem_for, serialize
from tests.constraint_planner_oracle import exhaustive_day


def test_baseline_checker_rejects_wrong_schedule_and_blanket_refusal():
    problem = problem_for("booking", 41)
    case = {"facts": serialize(problem), "expected": "FEASIBLE"}
    assert not assess(case, '{"status":"INFEASIBLE","visits":[]}')["correct"]
    bad = assess(case, '{"status":"FEASIBLE","visits":[]}')
    assert not bad["correct"] and not bad["greedy_correct"] and bad["false_confirmed_feasible"]
    visits, _, _ = exhaustive_day(problem)
    assert visits is not None
    data = {"status": "FEASIBLE", "visits": [vars(v) for v in visits]}
    assert assess(case, json.dumps(data))["correct"]


def test_unknown_facts_cannot_be_counted_as_confirmed_feasible():
    case = {"facts": serialize(problem_for("unknown_price", 137)), "expected": "UNKNOWN"}
    assert assess(case, '{"status":"UNKNOWN","visits":[]}')["correct"]
    assert not assess(case, '{"status":"INFEASIBLE","visits":[]}')["correct"]
    assert assess(case, '{"status":"FEASIBLE","visits":[]}')["false_confirmed_feasible"]
