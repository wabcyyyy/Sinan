"""P1 自制冻结事实；与在线探针报告分开，不冒充真实景点价格/营业资料。"""

from app.schemas.planning import ConstraintPlanningOptions
from app.schemas.trip import GenerateRequest
from app.schemas.trip_requirements import BudgetPolicy, FixedArrangement, PlaceRelation, RequiredPlace, TripRequirements


def planning_request(*, days=2, currency="EUR", zone="Europe/Paris", country="FR"):
    return GenerateRequest(
        city="Paris",
        days=days,
        persons=2,
        budget=200,
        start_date="2026-10-25",
        requirements_struct=TripRequirements(
            constraint_planning=ConstraintPlanningOptions(country_code=country, time_zone=zone, currency=currency),
            max_attractions_per_day=3,
            budget_policy=BudgetPolicy(mode="hard_cap"),
            required_places=[RequiredPlace(constraint_id="must", name="地点0")],
            fixed_arrangements=[
                FixedArrangement(day_no=1, name="地点0", item_type="attraction", start_time="10:00", end_time="11:40")
            ],
            place_relations=[
                PlaceRelation(constraint_id="sequence", kind="before", place_names=["地点4", "地点5"], day_no=days)
            ],
        ),
    )


def frozen_context(currency="EUR", country="FR"):
    def evidence(key):
        row: dict = {
            "provider": "frozen-fixture",
            "source_url": "https://example.com/fixture",
            "retrieved_at": "2026-10-24T08:00:00+00:00",
            "verification_status": "partially_verified",
            "value_kind": "observed",
        }
        if key == "open_time":
            row["opening_hours"] = {
                "status": "known",
                "rules": [
                    {"intervals": [{"start": "09:00", "end": "18:00"}], "source_text": "自制冻结窗口，不是在线营业数据"}
                ],
            }
        return row

    return {
        "candidates": [
            {
                "name": f"地点{i}",
                "id": f"p{i}",
                "source": "frozen-fixture",
                "country_code": country,
                "source_url": "https://example.com/fixture",
                "source_language": "fr",
                "source_fetched_at": "2026-10-24T08:00:00+00:00",
                "latitude": 48.86 + i * 0.001,
                "longitude": 2.33 + i * 0.001,
                "cost": 10 + i,
                "cost_currency": currency,
                "duration_min": 100,
                "fact_evidence": {key: evidence(key) for key in ("cost", "open_time")},
                "categories": [],
            }
            for i in range(6)
        ],
        "foods": [],
        "hotels": [],
        "research_report": {},
    }
