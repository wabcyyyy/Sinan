"""Anonymous weekly product proof; development and real NL evidence stay separate."""

import argparse
import json
import time
from unittest.mock import patch

from app.agent.generation.orchestration.complete_travel import plan_complete_travel
from app.schemas.travel_plan import TravelRequirements
from app.schemas.trip_requirements import IntakeState, TripRequirements
from scripts.chinatravel_human import OUTPUT
from scripts.chinatravel_original_data import save
from scripts.eval_chinatravel_original import request_for, shared_final

TEXT = (
    "当前位置上海。我们两个人去杭州玩七天，总预算20000元，住六晚，需要一间双床房。"
    "希望行程包含景点和餐饮，最后一天坐火车返回上海。请安排完整七天，不用指定实际日历日期。"
)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["development", "intake"])
    action = parser.parse_args().action
    if action == "intake":
        from scripts import eval_chinatravel_original as runner
        from scripts.chinatravel_human import authorize, initialize_worker

        authorize()
        initialize_worker()
        state = runner.intake("anonymous7d", TEXT, "week7-proof-v1", time.monotonic() + 78)
        if state.days != 7 or state.persons != 2 or state.stay_nights != 6:
            raise ValueError("actual NL extraction did not preserve the anonymous seven-day request")
        save(OUTPUT / "week7-intake.json", {"original_text": TEXT, "state": state.model_dump(mode="json")})
        print("actual NL intake preserved; no Gold and no model-generated plan substituted")
        return
    state = IntakeState(
        city="杭州",
        origin_city="上海",
        days=7,
        persons=2,
        budget=20000,
        stay_nights=6,
        requirements=TripRequirements(
            fact_source="chinatravel",
            travel_planner="cp",
            travel_constraints=TravelRequirements(rooms=1, room_beds=2),
        ),
    )
    request = request_for(state, TEXT)
    started = time.monotonic()
    result = {"measurement": "synthetic structured development request; frozen WorldEnv, no model calls"}
    try:
        with patch("app.agent.generation.content.day_prompts.get_llm_client", side_effect=AssertionError("no model")):
            days, issues = plan_complete_travel(request)
        result.update(issues=issues, plan=shared_final(days))
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
    result.update(elapsed_s=time.monotonic() - started, trace=request.context.get("travel_solver_trace", []))
    destination = OUTPUT / "week7-development.json"
    if destination.exists():
        destination = OUTPUT / f"week7-development-{time.time_ns()}.json"
    save(destination, result)
    print(json.dumps({k: v for k, v in result.items() if k not in {"plan", "trace"}}, ensure_ascii=False))
    if "error" in result or result["issues"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
