"""Single fixed replay of all three arms, saving complete inputs and proposals for independent audit."""

import argparse
import json
from pathlib import Path
from unittest.mock import patch

from app.schemas.trip import GenerateRequest
from scripts import eval_replan_ablation as ablation
from scripts.align_p4_scenario import local_only
from scripts.benchmark_expansion_cases import replan_environment, replan_event
from scripts.chinatravel_original_data import ROOT, save

ARMS = ("global_standard", "global_disturbance", "scoped")
DEFAULT_OUTPUT = ROOT / "docs/resume-optimization/20261026/replan-replay"


def execute(output, start, stop):
    if not 0 <= start < stop <= 60 or stop - start > 15:
        raise ValueError("one batch is at most 15 of the fixed 60 events")
    design = json.loads((ROOT / "docs/benchmarks/sinan-expanded-20261025-v2/design.json").read_text("utf8"))
    output.mkdir(parents=True, exist_ok=True)
    protocol = {
        "source": "docs/benchmarks/sinan-expanded-20261025-v2/design.json",
        "scope": "single replay of 60 already-seen synthetic events; 12 generated layout families",
        "arms": ARMS,
        "arm_seconds": ablation.ARM_SECONDS,
        "repeats": 1,
        "no_tuning_or_best_of": True,
        "arm_order": "rotate once by event index; no model/network calls",
        "failed_or_unknown_remain_in_denominator": True,
        "timing": (
            "0.6-second planning-search deadline begins before snapshot; snapshot and solver compilation "
            "consume it, but non-preemptible work may overrun. Recorded seconds include proposal/checker "
            "after search, exclude audit serialization and disk writes; not a hard wall-time guarantee."
        ),
    }
    protocol_path = output / "protocol.json"
    if protocol_path.exists() and json.loads(protocol_path.read_text("utf8")) != json.loads(json.dumps(protocol)):
        raise ValueError("replay protocol already frozen")
    if not protocol_path.exists():
        save(protocol_path, protocol)
    for case in design["replan"][start:stop]:
        if (output / f"{case['spec']['id']}.json").exists():
            raise ValueError("single replay forbids rerunning an already issued event")
    with local_only(), patch.object(ablation, "environment", replan_environment):
        for index in range(start, stop):
            case = design["replan"][index]
            request, before, routes, completed, optional = replan_environment(case["spec"])
            event = replan_event(case["spec"])
            if any(
                actual != case[key]
                for key, actual in (
                    ("before", before),
                    ("routes", routes),
                    ("completed", completed),
                    ("optional", optional),
                )
            ):
                raise ValueError("frozen replay inputs changed")
            # Loading the old request through today's schema adds only today's defaults.
            if request != GenerateRequest.model_validate(case["request"]):
                raise ValueError("frozen replay request changed after schema normalization")
            if event.model_dump(mode="json") != case["event"]:
                raise ValueError("frozen event changed")
            path = output / f"{case['spec']['id']}.json"
            row = {
                "id": case["spec"]["id"],
                "kind": event.kind,
                "arms": {},
                "same_total_seconds_per_arm": ablation.ARM_SECONDS,
                "frozen_case": case,
                "new_model_calls": 0,
                "new_http_requests": 0,
            }
            ordered = ARMS[index % 3 :] + ARMS[: index % 3]
            row["arm_order"] = ordered
            save(path, row)
            for arm in ordered:
                try:
                    row["arms"][arm] = ablation.run_arm(case["spec"], event, arm, include_audit=True)
                except Exception as exc:
                    row["arms"][arm] = {"status": "ERROR", "feasible_checked": False, "error": repr(exc)}
                save(path, row)
            print(json.dumps({"id": row["id"], "arms": {a: v["status"] for a, v in row["arms"].items()}}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--start", type=int, required=True)
    parser.add_argument("--stop", type=int, required=True)
    args = parser.parse_args()
    execute(args.output, args.start, args.stop)
