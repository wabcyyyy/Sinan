"""三段正式页面演示：同夜真实意图回放+显式确认表单+冻结资料，无新模型/网络。"""

import argparse
import json
from pathlib import Path


def build_app(run_dir):
    from scripts.serve_screenshot_browser import build_app as base

    app = base(run_dir)
    patcher = app.state.p3_patcher
    from app.agent.data.route_service import RouteService
    from app.agent.generation.orchestration import constraint_planning
    from app.schemas.trip import ClarifyResponse
    from app.schemas.trip_requirements import IntakeState
    from app.services import itinerary_city, itinerary_generation
    from tests.p5_trajectory_cases import frozen_matrix, setup

    source = Path("tests/fixtures/simulation/p5_cross_day_real_20261025.json")
    recorded = json.loads(source.read_text("utf-8"))
    requirements, context = setup(recorded["profile"], recorded["intake"])
    state = IntakeState.model_validate(recorded["intake"]["state"])
    state.requirements = requirements

    def clarify(body):
        current = body.state.model_copy(deep=True) if body.state else state.model_copy(deep=True)
        for key, value in body.slots.items():
            if key in {"city", "days", "persons", "budget", "start_date", "stay_nights"} and value is not None:
                setattr(current, key, value)
        return ClarifyResponse(
            state=current,
            ready=True,
            next="confirm",
            slots={k: getattr(current, k) for k in ("city", "days", "persons", "budget", "start_date", "stay_nights")},
            reply="请核对控制器预填的固定预约与第2天先后要求；地点与路段均为自制冻结资料，意图复用本夜真实输出。",
        )

    patcher.setattr(itinerary_city, "run_clarify", clarify)
    patcher.setattr(itinerary_generation, "run_plan_context", lambda *_a, **_kw: context)
    patcher.setattr(constraint_planning, "get_route_matrix", frozen_matrix)
    patcher.setattr(RouteService, "matrix", staticmethod(frozen_matrix))
    (run_dir / "scenario.json").write_text(
        json.dumps(
            {
                "method": "formal UI/API/worker/SQLite/solver; real intake replay; confirmed form; synthetic routes",
                "source": str(source),
                "confirmed_form": requirements.model_dump(mode="json", by_alias=True),
                "context": context,
            },
            ensure_ascii=False,
            indent=2,
        ),
        "utf-8",
    )
    return app


if __name__ == "__main__":
    import uvicorn

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", required=True, type=Path)
    args = parser.parse_args()
    uvicorn.run(build_app(args.run_dir.resolve()), host="127.0.0.1", port=18075, lifespan="off", log_level="warning")
