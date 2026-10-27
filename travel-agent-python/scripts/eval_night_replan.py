"""Recheck only changed scoped arm on the same 60 frozen events; reuse unchanged global evidence."""

import json
from pathlib import Path
from unittest.mock import patch

from scripts import eval_replan_ablation as ablation
from scripts.align_p4_scenario import local_only
from scripts.benchmark_expansion_cases import replan_environment, replan_event
from scripts.run_p4_acceptance import save


def execute(root, indexes):
    root = Path(root)
    old = root.parents[1] / "sinan-expanded-20261025-v2"
    if not old.exists():
        raise ValueError("original fixed benchmark missing")
    design = json.loads((old / "design.json").read_text("utf8"))
    previous = {
        row["id"]: row for path in old.glob("replan-??-??.json") for row in json.loads(path.read_text("utf8"))["rows"]
    }
    root.mkdir(parents=True, exist_ok=True)
    results = []
    with local_only(), patch.object(ablation, "environment", replan_environment):
        for index in indexes:
            case = design["replan"][index]
            identifier = case["spec"]["id"]
            path = root / f"{identifier}.json"
            if path.exists():
                results.append(json.loads(path.read_text("utf8")))
                continue
            request, before, *_ = replan_environment(case["spec"])
            assert request.model_dump(mode="json") == case["request"] and before == case["before"]
            scoped = ablation.run_arm(case["spec"], replan_event(case["spec"]), "scoped")
            row = {
                "id": identifier,
                "kind": case["event"]["kind"],
                "arms": {
                    "global_standard": previous[identifier]["arms"]["global_standard"],
                    "global_disturbance": previous[identifier]["arms"]["global_disturbance"],
                    "scoped": scoped,
                },
                "old_scoped": previous[identifier]["arms"]["scoped"],
                "same_total_seconds_per_arm": ablation.ARM_SECONDS,
                "baseline_reused": True,
                "note": "unchanged global default; old measured timing not contemporaneous",
            }
            save(path, row)
            results.append(row)
    return results
