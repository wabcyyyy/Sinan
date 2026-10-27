"""Three-policy frozen-tool experiment. No HTTP or model calls; hidden world reaches only tool/oracle."""

import hashlib
import json
from dataclasses import asdict
from decimal import Decimal

from app.agent.generation.content.constraint_trip_solver import solve_trip
from app.agent.generation.content.evidence_observations import FactObservation
from app.agent.generation.orchestration.active_evidence import drive_evidence
from app.agent.generation.orchestration.evidence_policy import certificate_complete
from app.agent.runtime.run_limits import current_limits
from app.schemas.trip import FactEvidence
from scripts.night_evidence_cases import environment, evidence, specifications
from scripts.run_p4_acceptance import save
from scripts.sinan_nl_freeze import REPO
from tests.constraint_planner_oracle import exhaustive_day, schedule_violations

ARMS = ("fixed", "all", "decision")
POLICY_FILES = ("active_evidence.py", "evidence_policy.py")


def fingerprint(root):
    paths = [REPO / "travel-agent-python/app/agent/generation/orchestration" / name for name in POLICY_FILES]
    paths += [
        REPO / "travel-agent-python/scripts/night_evidence_cases.py",
        REPO / "travel-agent-python/scripts/eval_night_evidence.py",
        REPO / "travel-agent-python/app/agent/generation/content/evidence_gaps.py",
        REPO / "travel-agent-python/app/agent/generation/content/evidence_observations.py",
        REPO / "travel-agent-python/app/agent/generation/content/constraint_trip_solver.py",
        root / "design.json",
    ]
    return {str(path.resolve()): hashlib.sha256(path.read_bytes()).hexdigest() for path in paths}


def prepare(root):
    root.mkdir(parents=True, exist_ok=True)
    if (root / "design.json").exists():
        raise FileExistsError("policy tasks already frozen")
    rows = []
    for case in specifications():
        _, _, truth = environment(case, truth=True)
        witness, score, examined = exhaustive_day(truth[0])
        case["oracle"] = {
            "feasible": witness is not None,
            "examined": examined,
            "visits": [v.__dict__ for v in witness or []],
            "score": score,
        }
        rows.append(case)
    save(
        root / "design.json",
        {
            "rows": rows,
            "arms": ARMS,
            "scope": (
                "6 development recipes;12 distinct confirmation instances within6 composite layouts; "
                "no blind human or real API"
            ),
            "queries": "same max6/tool-unit cap per task; full policy enumerates all facts within same cap",
            "actual_paid_cny": "0",
            "model_calls": 0,
            "http_calls": 0,
        },
    )
    return len(rows)


def freeze(root):
    if (root / "freeze.json").exists():
        raise FileExistsError("confirmation already frozen")
    save(root / "freeze.json", {"fingerprint": fingerprint(root)})


def run_case(case, strategy):
    request, prepared, problems = environment(case)
    _, _, truth = environment(case, truth=True)
    hidden = {row["id"]: row for row in case["hidden_candidates"]}
    true_routes = {(a, b): minutes for a, b, minutes in case["truth_routes"]}
    calls = []

    def observe(gap, _problems, current, _req, _options):
        limits = current_limits()
        if limits:
            limits.record_retrieval()
        ids = tuple(
            next(p.fact.source_entity_id for p in current if p.fact.candidate_id == key)
            for key in (gap.first_id, gap.second_id)
            if key is not None
        )
        row = hidden[gap.first_id]
        fact = FactEvidence.model_validate(evidence(row["opening"] if gap.kind == "opening" else None))
        calls.append({"gap": gap.gap_id, "kind": gap.kind, "units": gap.cost_units})
        if gap.kind == "route":
            return FactObservation(
                "route",
                ids,
                fact=fact,
                tool="frozen_route",
                route={
                    "from_poi_id": gap.first_id,
                    "to_poi_id": gap.second_id,
                    "duration_min": true_routes[gap.first_id, gap.second_id],
                    "source": "osrm",
                    "source_url": fact.source_url,
                    "time_dependent": False,
                    "measurement_scope": "frozen_tool_world",
                },
            )
        return FactObservation(
            gap.kind,
            ids,
            fact=fact,
            tool="frozen_" + gap.kind,
            value=row.get(gap.kind),
            currency="CNY" if gap.kind == "price" else None,
        )

    requirements = request.requirements_struct
    assert requirements is not None and requirements.constraint_planning is not None
    options = requirements.constraint_planning.active_evidence
    assert options is not None
    results, report = drive_evidence(
        problems,
        prepared,
        request,
        options=options,
        solve=lambda current, seconds: solve_trip(current, budget=Decimal(case["budget"]), time_limit_s=seconds),
        observe=observe,
        budget=Decimal(case["budget"]),
        strategy=strategy,
    )
    expected_feasible = case["oracle"]["feasible"]
    valid = bool(results[0].visits) and not schedule_violations(truth[0], results[0].visits)
    certified = certificate_complete(problems, results, prepared)
    confirmed_status = "FEASIBLE" if certified else "INFEASIBLE" if results[0].status == "INFEASIBLE" else "UNKNOWN"
    correct = (confirmed_status == "FEASIBLE" and valid) if expected_feasible else confirmed_status == "INFEASIBLE"
    return {
        "id": case["id"],
        "split": case["split"],
        "family": case["family"],
        "strategy": strategy,
        "expected": "FEASIBLE" if expected_feasible else "INFEASIBLE",
        "status": confirmed_status,
        "correct": correct,
        "false_feasible": confirmed_status == "FEASIBLE" and not valid,
        "false_infeasible": confirmed_status == "INFEASIBLE" and expected_feasible,
        "conditional_plan_valid": valid,
        "actual_tool_calls": len(calls),
        "calls": calls,
        "actual_model_calls": 0,
        "actual_tokens": 0,
        "actual_http_calls": 0,
        "actual_paid_cny": "0",
        "report": report.model_dump(mode="json"),
        "results": [asdict(r) for r in results],
    }


def execute(root, split, start, stop):
    design = json.loads((root / "design.json").read_text("utf8"))
    if split == "confirm" and fingerprint(root) != json.loads((root / "freeze.json").read_text("utf8"))["fingerprint"]:
        raise ValueError("frozen policy/input changed")
    cases = [r for r in design["rows"] if r["split"] == split]
    if not 0 <= start < stop <= len(cases) or stop - start > 2:
        raise ValueError("at most2 tasks per verification batch")
    output = root / ("development-fixed" if split == "dev" else "results")
    output.mkdir(exist_ok=True)
    rows = []
    for case in cases[start:stop]:
        for arm in ARMS:
            path = output / f"{case['id']}-{arm}.json"
            if path.exists():
                rows.append(json.loads(path.read_text("utf8")))
                continue
            result = run_case(case, arm)
            save(path, result)
            rows.append(result)
    return rows
