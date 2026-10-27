"""Bounded free replay of already-seen failures; original Human first-pass artifacts remain untouched."""

import argparse
import json
import time
from unittest.mock import patch

from app.agent.generation.orchestration.complete_travel import plan_complete_travel
from app.schemas.trip_requirements import IntakeState
from scripts.chinatravel_human import OUTPUT
from scripts.chinatravel_original_data import save
from scripts.eval_chinatravel_original import request_for, shared_final


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("uids", nargs="+")
    args = parser.parse_args()
    if len(args.uids) > 2:
        raise ValueError("one free replay batch supports at most2x30 seconds, under the90-second validation budget")
    allowed = {case["uid"] for case in json.loads((OUTPUT / "posthoc-null-fares.json").read_text("utf8"))["cases"]}
    for uid in args.uids:
        if uid not in allowed:
            raise ValueError("only the previously registered null-fare failures are in this replay")
        destination = OUTPUT / f"repair-v1-free-replay/{uid}-C.json"
        if destination.exists():
            print(json.dumps({"uid": uid, "reused": True}))
            continue
        intake = json.loads((OUTPUT / f"intake-v5/{uid}.json").read_text("utf8"))
        state = IntakeState.model_validate(intake["response"]["state"])
        state.requirements.travel_planner = "cp"
        started = time.monotonic()
        request = request_for(state, intake["original_text"])
        request.context["run_deadline_monotonic"] = started + 30
        result = {
            "uid": uid,
            "measurement": "seen failure replay; cached NL and frozen WorldEnv",
            "new_model_calls": 0,
            "gold_used": False,
            "human_review": "not performed",
            "fresh_NL_for_new_extractor": False,
            "original_phase": "human-v1",
        }
        try:
            with patch(
                "app.agent.generation.content.day_prompts.get_llm_client",
                side_effect=AssertionError("free replay forbids model calls"),
            ):
                days, issues = plan_complete_travel(request)
            result.update(plan=shared_final(days), issues=issues)
        except Exception as exc:
            result.update(plan={}, issues=[f"{type(exc).__name__}: {exc}"])
        result.update(elapsed_s=time.monotonic() - started, trace=request.context.get("travel_solver_trace", []))
        save(destination, result)
        print(
            json.dumps(
                {
                    "uid": uid,
                    "shared_final": bool(result["plan"]),
                    "issues": result["issues"],
                    "elapsed_s": result["elapsed_s"],
                },
                ensure_ascii=True,
            )
        )


if __name__ == "__main__":
    main()
