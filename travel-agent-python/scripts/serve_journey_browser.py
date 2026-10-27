"""三条用户旅程的隔离活栈；只回放模型边界，真实执行需求应用、队列及求解。"""

import argparse
import json
import threading
import time
from pathlib import Path

from scripts.serve_screenshot_browser import build_app as base_app

LOUVRE = "Musée du Louvre"
ORSAY = "Musée d'Orsay"


def build_app(run_dir, scenario):
    app = base_app(run_dir, inline_worker=False)
    patcher = app.state.p3_patcher
    from app.agent.data.route_service import RouteService
    from app.agent.editing import clarify
    from app.agent.generation.orchestration import constraint_planning
    from app.common.config import settings
    from app.services import itinerary_city, itinerary_generation, planning_worker, screenshot
    from tests.constraint_planning_cases import frozen_context

    settings.registration_invite_required = False
    settings.planning_worker_concurrency = 1
    names = [LOUVRE, ORSAY, "Eiffel Tower", "Jardin du Luxembourg"]
    context = frozen_context()
    context["candidates"] = context["candidates"][:4]
    for row, name in zip(context["candidates"], names, strict=True):
        row.update(name=name, duration_min=90, cost=20)
    if scenario == "manual":
        context["candidates"] = [context["candidates"][1]]
        patcher.setattr(screenshot, "resolve_screenshot_place", lambda *_: [])

    def extract(request, *_):
        if request.state is not None:
            return "{}"  # 复核浏览器表单时不得重复施加初轮意图。
        patches = [
            {"op": "set", "target": "max_attractions_per_day", "value": 2, "source_text": "每天最多2个景点"},
            {"op": "set", "target": "budget_policy_mode", "value": "hard_cap", "source_text": "预算200欧元硬上限"},
            {"op": "set", "target": "day_not_before", "day_no": 1, "value": "14:00", "source_text": "14:00以后开始"},
            {"op": "set", "target": "day_finish_by", "day_no": 1, "value": "18:00", "source_text": "18:00前结束"},
            {"op": "add", "target": "excluded_place", "value": "Eiffel Tower", "source_text": "不去Eiffel Tower"},
        ]
        for name in [ORSAY] if scenario == "manual" else [LOUVRE, ORSAY]:
            patches.append({"op": "add", "target": "required_place", "name": name, "source_text": f"必须去{name}"})
        if scenario == "conflict":
            for key, first, second in (("forward", LOUVRE, ORSAY), ("backward", ORSAY, LOUVRE)):
                quote = f"第1天必须先去{first}再去{second}"
                patches.append(
                    {
                        "op": "add",
                        "target": "place_relation",
                        "source_text": quote,
                        "relation": {
                            "constraint_id": key,
                            "kind": "before",
                            "place_names": [first, second],
                            "day_no": 1,
                            "strength": "hard",
                            "source_text": quote,
                        },
                    }
                )
        return json.dumps(
            {
                "city": "Paris",
                "days": 1,
                "persons": 1,
                "stay_nights": 0,
                "start_date": "2026-10-25",
                "budget": 200,
                "preferences": ["艺术"],
                "patches": patches,
            }
        )

    def matrix(points, **_):
        time.sleep(0.6)  # 给浏览器观察投机候选留出窗口，不用作延迟指标。
        return {
            (a["poi_id"], b["poi_id"]): {"duration_min": 10, "source": "frozen-fixture"}
            for a in points
            for b in points
            if a["poi_id"] != b["poi_id"]
        }

    patcher.setattr(clarify, "_ask", extract)
    patcher.setattr(itinerary_city, "run_clarify", clarify.run_clarify)
    patcher.setattr(itinerary_generation, "run_plan_context", lambda *_a, **_kw: context.copy())
    patcher.setattr(constraint_planning, "get_route_matrix", matrix)
    patcher.setattr(RouteService, "matrix", staticmethod(matrix))
    stop = threading.Event()

    def poll():
        while not stop.is_set():
            try:
                planning_worker.poll_once()
            except Exception as error:
                (run_dir / "worker-error.txt").write_text(str(error), encoding="utf-8")
                stop.set()
            stop.wait(0.1)

    app.state.journey_stop = stop
    app.state.journey_worker = threading.Thread(target=poll, daemon=True)
    return app


if __name__ == "__main__":
    import uvicorn

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", required=True, type=Path)
    parser.add_argument("--scenario", choices=("plan", "conflict", "manual"), required=True)
    args = parser.parse_args()
    application = build_app(args.run_dir.resolve(), args.scenario)
    application.state.journey_worker.start()
    try:
        uvicorn.run(application, host="127.0.0.1", port=18077, lifespan="off", log_level="warning")
    finally:
        application.state.journey_stop.set()
