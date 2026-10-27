"""Predeclared eight anonymous NL checks with current low thinking and the one cumulative ledger."""

import argparse
import hashlib
import json
import time
from datetime import UTC, datetime
from unittest.mock import patch

from app.agent.editing import clarify
from app.agent.generation.orchestration.complete_travel import plan_complete_travel
from app.common.llm_client import _apply_provider_options
from app.schemas.trip import ClarifyRequest
from app.schemas.trip_requirements import IntakeState, TripRequirements
from scripts.chinatravel_original_client import OriginalClient
from scripts.chinatravel_original_data import ROOT, SOURCE, save
from scripts.eval_chinatravel_original import request_for, shared_final
from scripts.sinan_budget_lock import atomic_ledger_save, ledger_lock

OUTPUT = ROOT / "docs/resume-optimization/20261026/nl-low-v1"
PREFIX = "resume-D-nl-low-v1"
PRODUCERS = (
    "app/agent/editing/complete_travel_intake.py",
    "app/agent/editing/clarify.py",
    "app/agent/editing/clarify_contract.py",
    "app/agent/generation/content/complete_travel_checks.py",
)


def cases():
    common = {"days": 2, "persons": 2, "budget": 6000, "hotel_cap": None, "required_name": None}
    return [
        {
            "id": "dev-hotel",
            "phase": "dev",
            "text": "我们2人从上海去北京玩2天1晚，整趟预算6000元。酒店每人每晚不超过350元，订1间双床房。",
            "expected": {**common, "city": "北京", "hotel_cap": 350, "rooms": 1, "room_beds": 2},
            "cp": True,
        },
        {
            "id": "dev-room-price",
            "phase": "dev",
            "text": "2人上海出发去杭州2天1晚，总预算6000元。酒店每间每晚不超过500元，住一间双床房。",
            "expected": {**common, "city": "杭州", "rooms": 1, "room_beds": 2},
            "cp": False,
        },
        {
            "id": "dev-entity",
            "phase": "dev",
            "text": "我们两个人从上海去北京，两天一晚，整趟预算6000元，必须去北京海洋馆，住1间双床房。",
            "expected": {**common, "city": "北京", "required_name": "北京海洋馆", "rooms": 1, "room_beds": 2},
            "cp": False,
        },
        {
            "id": "dev-count-conflict",
            "phase": "dev",
            "text": "旅行人数4。我和4个同学从上海去杭州玩2天1晚，整趟预算6000元。",
            "expected": {"conflicting_counts": [4, 5]},
            "cp": False,
        },
        {
            "id": "final-hotel",
            "phase": "final",
            "text": (
                "上海到杭州，2位，游玩2天住宿1晚，全程总费用上限6000元；住宿按每人每晚最多300元安排，房型要一间双床房。"
            ),
            "expected": {**common, "city": "杭州", "hotel_cap": 300, "rooms": 1, "room_beds": 2},
            "cp": True,
        },
        {
            "id": "final-total-only",
            "phase": "final",
            "text": (
                "两个人从上海出发，在北京玩两天住一晚，整趟合计6000元以内，只要一间双床房，没有另外的住宿人均预算要求。"
            ),
            "expected": {**common, "city": "北京", "rooms": 1, "room_beds": 2},
            "cp": False,
        },
        {
            "id": "final-entity",
            "phase": "final",
            "text": "上海出发，两人去杭州安排两天一晚，总预算6000元，杭州动物园是必须去的，住一间双床房。",
            "expected": {**common, "city": "杭州", "required_name": "杭州动物园", "rooms": 1, "room_beds": 2},
            "cp": False,
        },
        {
            "id": "final-count-conflict",
            "phase": "final",
            "text": "出行人数3；我和3个朋友从上海出发到北京，2天1晚，整趟预算6000元。",
            "expected": {"conflicting_counts": [3, 4]},
            "cp": False,
        },
    ]


def freeze():
    options = {}
    _apply_provider_options(options, "https://api.deepseek.com", False)
    if options != {"thinking": {"type": "enabled"}, "reasoning_effort": "low"}:
        raise ValueError("new experiment requires current project thinking enabled/low")
    path = OUTPUT / "protocol.json"
    if path.exists():
        return json.loads(path.read_text("utf8"))
    protocol = {
        "frozen_utc": datetime.now(UTC).isoformat(),
        "cases": cases(),
        "model_options": options,
        "supplier": "existing configured DeepSeek Flash",
        "calls_per_case_max": 4,
        "case_deadline_s": 78,
        "CP_deadline_s": 30,
        "source": "anonymous author-created NL; not official Human, real travelers or independent generalization",
        "evaluation": (
            "exact declared slots/typed fields; conflicts must block; "
            "two predeclared hotel cases also run shared CP and business gate"
        ),
        "final_rule": "one attempt per case; no final tuning, no retries, failures retained",
        "ledger": str((SOURCE / "budget.json").relative_to(ROOT)),
        "producer_sha256_at_freeze": {
            relative: hashlib.sha256((ROOT / "travel-agent-python" / relative).read_bytes()).hexdigest()
            for relative in PRODUCERS
        },
    }
    save(path, protocol)
    return protocol


def authorize():
    path = SOURCE / "budget.json"
    with ledger_lock(path):
        state = json.loads(path.read_text("utf8"))
        if state["cap_cny"] is not None or state.get("cap_mode") != "unlimited":
            raise ValueError("existing unlimited authorization required; no cap or ledger reset")
        scope = state["active_authorization"]
        if PREFIX not in scope["proof_prefixes"]:
            scope["proof_prefixes"].append(PREFIX)
            extension = {
                "id": PREFIX,
                "approved_at_utc": datetime.now(UTC).isoformat(),
                "scope": (
                    "D resume evidence reinforcement: frozen4 development+4 final anonymous NL; "
                    "max4 calls/case; currentlow"
                ),
                "authorization": (
                    "current user explicitly authorized actual code/evaluation/API experiments with unlimited budget; "
                    "parent delegated bounded8-case scope"
                ),
                "protocol": str((OUTPUT / "protocol.json").relative_to(ROOT)),
                "prior_peak_upper_cny": state["total_peak_upper_cny"],
                "records_preserved": len(state["records"]),
            }
            scope.setdefault("scope_extensions", []).append(extension)
            state.setdefault("authorization_history", []).append(extension)
            atomic_ledger_save(path, state)


def evaluate(response, expected):
    state = response.state
    if "conflicting_counts" in expected:
        passed = (
            response.blocked
            and not response.ready
            and state.persons is None
            and state.person_count_options == expected["conflicting_counts"]
        )
        return {"extraction_pass": passed, "checks": {"conflict_requires_confirmation": passed}}
    constraints = state.requirements.travel_constraints
    checks = {name: getattr(state, name) == expected[name] for name in ("city", "days", "persons", "budget")}
    checks["origin_city"] = state.origin_city == "上海"
    checks["attribute_binding_available"] = constraints is not None
    if constraints:
        checks["hotel_cap"] = constraints.max_hotel_cost_per_person_per_night == expected["hotel_cap"]
        checks["rooms"] = constraints.rooms == expected["rooms"]
        checks["room_beds"] = constraints.room_beds == expected["room_beds"]
        checks["required_name"] = (
            (expected["required_name"] in constraints.required_names)
            if expected["required_name"]
            else not constraints.required_names
        )
    return {"extraction_pass": all(checks.values()), "checks": checks}


def execute(phase):
    protocol = freeze()
    selected = [case for case in protocol["cases"] if case["phase"] == phase]
    if phase == "final" and any(
        not (OUTPUT / f"results/{case['id']}.json").exists() for case in protocol["cases"] if case["phase"] == "dev"
    ):
        raise ValueError("finish development before final acceptance")
    entry = OUTPUT / f"{phase}-entry.json"
    if entry.exists():
        raise ValueError("one entry per frozen phase; no repeated acceptance run")
    save(
        entry,
        {
            "started_utc": datetime.now(UTC).isoformat(),
            "phase": phase,
            "producer_sha256": {
                relative: hashlib.sha256((ROOT / "travel-agent-python" / relative).read_bytes()).hexdigest()
                for relative in PRODUCERS
            },
            "development_changes": (
                "raise attribute max output1200→4096 for low reasoning; "
                "explicit digit companion-count conflict guard; no change after final begins"
            )
            if phase == "final"
            else None,
        },
    )
    authorize()
    for case in selected:
        path = OUTPUT / f"results/{case['id']}.json"
        if path.exists():
            raise ValueError("single attempt per case; do not repeat or select best result")
        started = time.monotonic()
        client = OriginalClient(f"{PREFIX}-{case['id']}", call_limit=4, deadline=started + 78, output=OUTPUT)
        result = {
            "id": case["id"],
            "phase": phase,
            "text": case["text"],
            "expected": case["expected"],
            "extraction_pass": False,
            "new_model_calls": 0,
        }
        try:
            with patch.object(clarify, "get_llm_client", return_value=client):
                response = clarify.run_clarify(
                    ClarifyRequest(
                        message=case["text"],
                        state=IntakeState(requirements=TripRequirements(fact_source="chinatravel")),
                    )
                )
            result["response"] = response.model_dump(mode="json", by_alias=True)
            result.update(evaluate(response, case["expected"]))
            if case["cp"] and result["extraction_pass"]:
                state = response.state
                state.requirements.travel_planner = "cp"
                request = request_for(state, case["text"])
                request.context["run_deadline_monotonic"] = time.monotonic() + 30
                with patch(
                    "app.agent.generation.content.day_prompts.get_llm_client",
                    side_effect=AssertionError("CP must not call generation model"),
                ):
                    plans, issues = plan_complete_travel(request)
                plan = shared_final(plans)
                result["business"] = {
                    "plan": plan,
                    "issues": issues,
                    "deliverable": bool(plan) and not issues,
                    "solver_trace": request.context.get("travel_solver_trace", []),
                }
        except Exception as exc:
            result["error"] = f"{type(exc).__name__}: {exc}"
        result.update(new_model_calls=client.calls, elapsed_s=time.monotonic() - started)
        save(path, result)
        print(
            json.dumps(
                {key: result.get(key) for key in ("id", "extraction_pass", "new_model_calls", "elapsed_s", "error")},
                ensure_ascii=True,
            ),
            flush=True,
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phase", choices=("freeze", "dev", "final"), required=True)
    args = parser.parse_args()
    if args.phase == "freeze":
        freeze()
        print("protocol frozen; no API call")
    else:
        execute(args.phase)
