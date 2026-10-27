"""正式页面/路由/SQLite/worker，复用本轮真实意图与来源快照；浏览器阶段禁止模型和外网。"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def build_app(run_dir, source):
    from scripts.serve_screenshot_browser import build_app as base

    app = base(run_dir)
    patcher = app.state.p3_patcher
    from app.agent.generation.content import opening_evidence
    from app.schemas.trip import ClarifyResponse
    from app.schemas.trip_requirements import IntakeState, TripRequirements
    from app.services import itinerary_city, itinerary_generation

    recorded = json.loads((source / "result.json").read_text("utf-8"))

    def clarify(body):
        state = (
            body.state.model_copy(deep=True) if body.state else IntakeState.model_validate(recorded["clarify_state"])
        )
        if body.state is None:
            state.requirements = TripRequirements.model_validate(recorded["form_requirements"])
            assert state.requirements.constraint_planning is not None
            state.requirements.constraint_planning.active_evidence = None
        return ClarifyResponse(
            state=state,
            ready=True,
            next="confirm",
            slots={k: getattr(state, k) for k in ("city", "days", "persons", "budget", "start_date", "stay_nights")},
            reply="请核对本地匿名验收的09–13固定安排；使用本轮真实模型与来源快照。",
        )

    patcher.setattr(itinerary_city, "run_clarify", clarify)
    patcher.setattr(itinerary_generation, "run_plan_context", lambda *_a, **_kw: recorded["context"])
    patcher.setattr(opening_evidence, "web_search_sources", lambda *_a, **_kw: recorded["opening_source_rows"])
    return app


if __name__ == "__main__":
    import uvicorn

    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--run-dir", type=Path, required=True)
    p.add_argument("--source", type=Path, required=True)
    a = p.parse_args()
    uvicorn.run(
        build_app(a.run_dir.resolve(), a.source.resolve()),
        host="127.0.0.1",
        port=18074,
        lifespan="off",
        log_level="warning",
    )
