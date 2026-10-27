"""Complete-plan product adapter; shares the existing whole-trip generation entry and public fact checks."""

from app.agent.data.chinatravel import common_issues, measure_calls
from app.agent.generation.content.complete_travel_checks import request_issues
from app.agent.generation.content.day_prompts import llm_open_trip
from app.schemas.travel_plan import TravelPlan
from app.schemas.trip import DailyPlan


def plan_complete_travel(request):
    with measure_calls() as usage:
        try:
            raw_days, _suggestions = llm_open_trip(request)
            plan = TravelPlan.model_validate(request.context["travel_last_output"])
            issues = request_issues(plan, request) + common_issues(plan.official())
            return [DailyPlan.model_validate(day) for day in raw_days], issues
        finally:
            request.context["travel_tool_usage"] = dict(usage)
