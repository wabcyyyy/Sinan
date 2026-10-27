"""Remaining formal journeys: this night's real outputs replayed, actual product mutations and isolated DB."""

import argparse
import json
from copy import deepcopy
from pathlib import Path

from scripts.run_p4_acceptance import save
from scripts.serve_screenshot_browser import build_app as base_app


def build_app(run_dir, scenario):
    app = base_app(run_dir)
    patcher = app.state.p3_patcher
    from app.agent.data.route_service import RouteService
    from app.agent.editing import screenshot as vision
    from app.agent.generation.orchestration import constraint_planning
    from app.common.config import settings
    from app.schemas.trip import ClarifyResponse
    from app.schemas.trip_requirements import IntakeState, TripRequirements
    from app.services import itinerary_city, itinerary_generation, screenshot
    from app.services.image_intent import _to_data_uri
    from scripts.eval_sinan_nl import context_for

    settings.registration_invite_required = False
    root = Path(__file__).resolve().parents[2]
    night = root / "docs/benchmarks/sinan-night-20261026"
    if scenario == "conflict":
        repair = root / "docs/benchmarks/sinan-nl-01-20261025/repair-20261026"
        design = json.loads((repair / "design.json").read_text("utf8"))
        case = next(row for row in design["regressions"] if row["base_id"] == "diag-00")
        recorded = json.loads((repair / "results" / f"{case['id']}-C.json").read_text("utf8"))
        state = IntakeState.model_validate(recorded["result"]["final"]["state"])
        context = context_for(case)
        raw = case["nl"]
        matrix = {
            (a, b): {"duration_min": minutes, "source": "benchmark-synthetic"}
            for a, b, minutes in case["facts"]["routes"]
        }
        reverse = state.requirements.place_relations[1].constraint_id
        image = None
    else:
        live = night / "continuous-live-confirmed-source"
        browser = json.loads((live / "frontend/browser.json").read_text("utf8"))
        initial = browser["initial"]
        state = IntakeState(
            city=initial["city"],
            days=initial["days"],
            persons=initial["persons"],
            budget=initial["budget"],
            start_date=initial["startDate"],
            stay_nights=initial["stayNights"],
            preferences=[initial["preferences"]] if initial["preferences"] else [],
            requirements=TripRequirements.model_validate(initial["requirementsStruct"]),
        )
        context = json.loads((live / "research.json").read_text("utf8"))["context"]
        raw = browser["rawInput"]
        matrix = {
            (f"cp-{a}", f"cp-{b}"): {"duration_min": 10, "source": "frozen-journey-fixture"}
            for a in range(2)
            for b in range(2)
            if a != b
        }
        image = night / "vision/zero-paris.png"
        extracted = json.loads((night / "vision/results/zero-paris.json").read_text("utf8"))["extraction"]
        uri = _to_data_uri(image.read_bytes())

        class RecordedVision:
            model = "deepseek-flash"
            base_url = "https://api.deepseek.com"

            def chat(self, messages, **_kwargs):
                if messages[0]["content"][0]["image_url"]["url"] != uri:
                    raise ValueError("only this night's authorized anonymous zero-price screenshot")
                save(
                    run_dir / "vision-replay.json",
                    {"source": str(night / "vision/results/zero-paris.json"), "new_model_calls": 0},
                )
                return json.dumps(extracted, ensure_ascii=False)

        patcher.setattr(vision, "get_llm_client", lambda *_: RecordedVision())
        patcher.setattr(screenshot, "resolve_screenshot_place", lambda *_: [])
        reverse = None

    def clarify(body):
        current = body.state.model_copy(deep=True) if body.state else state.model_copy(deep=True)
        for key, value in body.slots.items():
            if key in {"city", "days", "persons", "budget", "start_date", "stay_nights"} and value is not None:
                setattr(current, key, value)
        return ClarifyResponse(
            state=current,
            ready=True,
            next="confirm",
            slots={
                key: getattr(current, key) for key in ("city", "days", "persons", "budget", "start_date", "stay_nights")
            },
            reply="本夜真实模型输出回放；请核对要求，确认前不写行程。",
        )

    patcher.setattr(itinerary_city, "run_clarify", clarify)
    patcher.setattr(itinerary_generation, "run_plan_context", lambda *_a, **_kw: deepcopy(context))

    def routes(points, **_kwargs):
        if scenario == "conflict":
            return matrix
        return {
            (a["poi_id"], b["poi_id"]): {"duration_min": 10, "source": "frozen-journey-fixture"}
            for a in points
            for b in points
            if a["poi_id"] != b["poi_id"]
        }

    patcher.setattr(constraint_planning, "get_route_matrix", routes)
    patcher.setattr(RouteService, "matrix", staticmethod(routes))
    save(
        run_dir / "scenario.json",
        {
            "scenario": scenario,
            "raw": raw,
            "state": state.model_dump(mode="json"),
            "reverse_id": reverse,
            "image": str(image) if image else None,
            "mode": "same-night real output replay; zero new model/HTTP; product writes real",
        },
    )
    return app


if __name__ == "__main__":
    import uvicorn

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--scenario", choices=("conflict", "screenshot"), required=True)
    args = parser.parse_args()
    uvicorn.run(
        build_app(args.run_dir.resolve(), args.scenario),
        host="127.0.0.1",
        port=18078,
        lifespan="off",
        log_level="warning",
    )
