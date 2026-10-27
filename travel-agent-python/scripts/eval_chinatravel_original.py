"""Bounded serial original-NL product generation, shared A first output, ordinary B feedback and official scoring."""

import argparse
import json
import subprocess
import sys
import time
from unittest.mock import patch

from app.agent.editing import clarify
from app.agent.generation.content import day_prompts
from app.agent.generation.orchestration.complete_travel import plan_complete_travel
from app.agent.research.supervisor import run_research_context
from app.schemas.travel_plan import TravelPlan
from app.schemas.trip import ClarifyRequest, GenerateDayRequest, GenerateRequest
from app.schemas.trip_requirements import IntakeState, TripRequirements
from scripts.chinatravel_original_client import OriginalClient
from scripts.chinatravel_original_data import OUTPUT, prepare_evaluator, public_query, queries, save


def configure_suite(*, output, query_loader, evaluator, client_factory):
    """Bind one suite per isolated worker process; never use this runner concurrently in threads."""
    global OUTPUT, queries, prepare_evaluator, OriginalClient
    OUTPUT, queries, prepare_evaluator, OriginalClient = output, query_loader, evaluator, client_factory


def shared_final(days):
    if not days or days[0].travel_header is None:
        raise ValueError("producer did not return a shared complete business object")
    header = days[0].travel_header
    if any(day.travel_header != header for day in days):
        raise ValueError("shared business headers differ")
    return TravelPlan.model_validate(
        {**header.model_dump(), "itinerary": [{"day": day.day_no, "activities": day.travel_activities} for day in days]}
    ).official()


def intake(uid, text, phase, deadline):
    path = OUTPUT / f"{phase}/intake/{uid}.json"
    shared = OUTPUT / f"intake-v5/{uid}.json"
    if shared.exists():
        observed = json.loads(shared.read_text("utf8"))
        if observed["original_text"] != text:
            raise ValueError("cached intake original text changed")
        return IntakeState.model_validate(observed["response"]["state"])
    previous = OUTPUT / f"intake-v4/{uid}.json"
    if not previous.exists():
        previous = OUTPUT / f"intake-v3/{uid}.json"
    if previous.exists():
        from app.agent.editing.complete_travel_intake import extract

        observed = json.loads(previous.read_text("utf8"))
        state = IntakeState.model_validate(observed["response"]["state"])
        if all(getattr(state, key) is not None for key in ("city", "origin_city", "days", "persons")):
            if state.requirements.travel_constraints is None:
                client = OriginalClient(f"{phase}-{uid}-attribute-v5", call_limit=1, deadline=deadline)
                state.requirements.travel_constraints = extract(text, state, client)
                state.requirements.unresolved_requests = [
                    s for s in state.requirements.unresolved_requests if not s.startswith("官方属性绑定未完成：")
                ]
            if not state.requirements.travel_constraints.unresolved:
                observed["response"]["state"] = state.model_dump(mode="json", by_alias=True)
                observed["reused_basic_intake_from"] = str(previous.relative_to(OUTPUT))
                save(shared, observed)
                return state
    legacy = OUTPUT / f"intake/{uid}.json"
    if legacy.exists():
        from app.agent.editing.complete_travel_intake import extract

        observed = json.loads(legacy.read_text("utf8"))
        if observed["original_text"] != text:
            raise ValueError("legacy original text differs")
        state = IntakeState.model_validate(observed["response"]["state"])
        client = OriginalClient(f"{phase}-{uid}-attribute-v5", call_limit=1, deadline=deadline)
        state.requirements.travel_constraints = extract(text, state, client)
        observed["response"]["state"] = state.model_dump(mode="json", by_alias=True)
        observed["attribute_binding_phase"] = phase
        save(shared, observed)
        return state
    if path.exists():
        response = json.loads(path.read_text("utf8"))
        save(shared, {"original_text": text, "source_phase": phase, "response": response})
        return IntakeState.model_validate(response["state"])
    client = OriginalClient(f"{phase}-{uid}-intake", call_limit=4, deadline=deadline)
    initial = IntakeState(requirements=TripRequirements(fact_source="chinatravel"))
    with patch.object(clarify, "get_llm_client", return_value=client):
        response = clarify.run_clarify(ClarifyRequest(message=text, state=initial))
    save(path, response.model_dump(mode="json", by_alias=True))
    save(
        shared,
        {"original_text": text, "source_phase": phase, "response": response.model_dump(mode="json", by_alias=True)},
    )
    state = response.state
    if state.requirements.travel_constraints is None:
        raise ValueError("source-specific NL attribute binding incomplete")
    if any(getattr(state, key) is None for key in ("city", "origin_city", "days", "persons")):
        raise ValueError("original natural-language intake incomplete; no Gold injection")
    if state.start_date is not None:
        raise ValueError("intake invented a calendar date absent from the official task")
    return state


def request_for(state, text):
    if state.start_date is not None:
        raise ValueError("unexpected calendar date")
    state.requirements.fact_source = "chinatravel"
    req = GenerateRequest(
        city=state.city,
        origin_city=state.origin_city,
        days=state.days,
        persons=state.persons,
        budget=state.budget,
        preferences=state.preferences,
        start_date=None,
        intent=text,
        requirements=text,
        requirements_struct=state.requirements,
    )
    context = run_research_context(req)
    context["requested_stay_nights"] = state.stay_nights if state.stay_nights is not None else max(state.days - 1, 0)
    return GenerateDayRequest(
        city=state.city,
        day_no=1,
        days=state.days,
        persons=state.persons,
        budget=state.budget,
        start_date=None,
        intent=text,
        requirements=text,
        requirements_struct=state.requirements,
        context=context,
        needs_hotel=context["requested_stay_nights"] > 0,
    )


def one(uid, phase="dev-v1", arms="AB", *, deadline=None, reuse_from=None):
    work = prepare_evaluator()
    manifest = json.loads((OUTPUT / "manifest.json").read_text("utf8"))
    if phase.startswith("dev") and uid not in manifest["dev"]:
        raise ValueError("development cannot inspect frozen confirmation outcomes")
    public = public_query(next(row for row in queries() if row["uid"] == uid))
    deadline = deadline if deadline is not None else time.monotonic() + 78
    try:
        state = intake(uid, public["nature_language"], phase, deadline)
        if state.requirements.travel_constraints is None or any(
            getattr(state, key) is None for key in ("city", "origin_city", "days", "persons")
        ):
            raise ValueError("cached or fresh original NL intake incomplete; no Gold injection")
        if state.start_date is not None:
            raise ValueError("intake invented a calendar date absent from the official task")
    except Exception as exc:
        # Intake is part of the real NL chain. Preserve its failures for every predeclared arm.
        failures = []
        for arm in arms:
            result = {
                "uid": uid,
                "arm": arm,
                "phase": phase,
                "original_text": public["nature_language"],
                "plan": {},
                "business_plans": [],
                "issues": [f"intake: {type(exc).__name__}: {exc}"],
                "elapsed_s": 0,
                "new_calls": 0,
                "gold_used_by_producer": False,
                "human_review": "not performed",
            }
            save(OUTPUT / f"{phase}/results/{uid}-{arm}.json", result)
            save(work / f"results/SinanOriginal-{phase}-{arm}/{uid}.json", {})
            failures.append(result)
        return failures
    completed = []
    for arm in arms:
        path = OUTPUT / f"{phase}/results/{uid}-{arm}.json"
        if path.exists():
            completed.append(json.loads(path.read_text("utf8")))
            continue
        if deadline - time.monotonic() < 8:
            break
        request = request_for(state, public["nature_language"])
        request.context["run_deadline_monotonic"] = deadline
        request.requirements_struct.travel_planner = "cp" if arm == "C" else "generate"
        inherited = None
        if arm == "B":
            first = OUTPUT / f"{phase}/results/{uid}-A.json"
            if not first.exists():
                raise ValueError("B requires the preserved A first output")
            inherited = json.loads(first.read_text("utf8"))
            if not inherited["issues"]:
                save(path, {**inherited, "arm": "B", "shared_A_first_output": True, "new_calls": 0})
                save(work / f"results/SinanOriginal-{phase}-B/{uid}.json", inherited["plan"])
                completed.append(json.loads(path.read_text("utf8")))
                continue
            request.feedback = json.dumps(
                {
                    "issues": inherited["issues"][:32],
                    "previous_plan": inherited.get("failed_candidate") or inherited["plan"],
                    "actual_route_receipts": inherited.get("routes", []),
                },
                ensure_ascii=False,
            )
        if arm in "AB" and reuse_from:
            from app.agent.generation.content.complete_travel import prompt

            old_file = OUTPUT / f"{reuse_from}/results/{uid}-{arm}.json"
            old_request = OUTPUT / f"models/{reuse_from}-{uid}-{arm}-0-request.json"
            if old_file.exists() and old_request.exists():
                system, user = prompt(request)
                wanted = [{"role": "system", "content": system}, {"role": "user", "content": user}]
                if json.loads(old_request.read_text("utf8"))["messages"] == wanted:
                    result = {
                        **json.loads(old_file.read_text("utf8")),
                        "phase": phase,
                        "new_calls": 0,
                        "reused_model_output_from": str(old_file.relative_to(OUTPUT)),
                    }
                    save(path, result)
                    save(work / f"results/SinanOriginal-{phase}-{arm}/{uid}.json", result["plan"])
                    completed.append(result)
                    continue
        client = None if arm == "C" else OriginalClient(f"{phase}-{uid}-{arm}", call_limit=1, deadline=deadline)
        started = time.monotonic()
        failed_candidate = None
        try:
            with patch.object(day_prompts, "get_llm_client", return_value=client):
                days, issues = plan_complete_travel(request)
            plan = shared_final(days)
            if plan != request.context["travel_last_output"]:
                raise ValueError("shared business final differs from generated representation")
            business_plans = [day.model_dump(mode="json", by_alias=True) for day in days]
        except Exception as exc:
            failed_candidate = request.context.get("travel_last_output", {})
            plan = {}  # A failed producer has no shared final output; candidates are diagnostic only.
            business_plans, issues = [], [f"{type(exc).__name__}: {exc}"]
        result = {
            "uid": uid,
            "arm": arm,
            "phase": phase,
            "original_text": public["nature_language"],
            "plan": plan,
            "failed_candidate": failed_candidate,
            "raw_bound_output": request.context.get("travel_raw_bound_output"),
            "business_plans": business_plans,
            "issues": issues,
            "routes": request.context.get("travel_route_receipts", []),
            "solver_trace": request.context.get("travel_solver_trace", []),
            "tool_usage": request.context.get("travel_tool_usage", {}),
            "elapsed_s": time.monotonic() - started,
            "new_calls": client.calls if client is not None else 0,
            "shared_A_first_output": arm == "B",
            "gold_used_by_producer": False,
            "human_review": "not performed",
        }
        save(path, result)
        save(work / f"results/SinanOriginal-{phase}-{arm}/{uid}.json", plan)
        completed.append(result)
    return completed


def score(uids, phase, arms="AB", *, tag=""):
    work = prepare_evaluator()
    split = f"sinan_original_{phase.replace('-', '_')}{'_' + tag if tag else ''}"
    (work / f"chinatravel/evaluation/default_splits/{split}.txt").write_text("\n".join(uids) + "\n", encoding="utf8")
    for arm in arms:
        method = f"SinanOriginal-{phase}-{arm}"
        result = subprocess.run(
            [sys.executable, "eval_exp.py", "--splits", split, "--method", method, "--lang", "zh"],
            cwd=work,
            capture_output=True,
            timeout=70,
        )
        path = OUTPUT / f"{phase}/official-score-{arm}{'-' + tag if tag else ''}.log"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(result.stdout + result.stderr)
        print(f"{arm}: exit={result.returncode}", result.stdout.decode("utf8", errors="replace")[-1200:])
        if result.returncode:
            raise RuntimeError(f"official scoring failed: {path}")
    return split


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--uid", required=True)
    parser.add_argument("--phase", default="dev-v1")
    parser.add_argument("--arms", default="AB", choices=("A", "B", "AB", "C", "ABC"))
    parser.add_argument("--score-only", action="store_true")
    parser.add_argument("--reuse-from")
    args = parser.parse_args()
    if not args.score_only:
        rows = one(args.uid, args.phase, args.arms, reuse_from=args.reuse_from)
        print(
            json.dumps(
                [
                    {
                        "uid": row["uid"],
                        "arm": row["arm"],
                        "issue_count": len(row["issues"]),
                        "elapsed_s": row["elapsed_s"],
                    }
                    for row in rows
                ],
                ensure_ascii=True,
            )
        )
    else:
        score([args.uid], args.phase, args.arms)
