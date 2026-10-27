"""P2 合成冻结场景；数值用于验证算法，不冒充真实旅行数据。"""

from app.schemas.planning import ConstraintPlanningOptions
from app.schemas.trip import GenerateRequest
from app.schemas.trip_requirements import DayWindow, TripRequirements


def case(count=3, days=1):
    req = TripRequirements(
        constraint_planning=ConstraintPlanningOptions(country_code="FR", time_zone="Europe/Paris", currency="EUR"),
        day_windows=[
            DayWindow(day_no=d, kind="arrival", not_before="09:00", finish_by="18:00") for d in range(1, days + 1)
        ],
    )
    request = GenerateRequest(
        city="Paris", days=days, persons=1, budget=1000, start_date="2026-10-25", requirements_struct=req
    )
    plans = []
    for day in range(1, days + 1):
        rows = []
        for i in range(count):
            key = (day - 1) * count + i + 1
            start = 540 + i * 40
            rows.append(
                {
                    "id": key,
                    "poi_name": f"P{key}",
                    "item_type": "attraction",
                    "source": "synthetic-fixture",
                    "country_code": "FR",
                    "cost": 10,
                    "cost_currency": "EUR",
                    "start_time": f"{start // 60:02d}:{start % 60:02d}",
                    "end_time": f"{(start + 30) // 60:02d}:{(start + 30) % 60:02d}",
                    "duration_min": 30,
                }
            )
        plans.append({"day_no": day, "items": rows, "note": "合成冻结场景"})
    matrix = [
        {"fromItemId": a["id"], "toItemId": b["id"], "route": {"duration_min": 10, "source": "synthetic-fixture"}}
        for d in plans
        for a in d["items"]
        for other in plans
        for b in other["items"]
        if a["id"] != b["id"]
    ]
    return request, plans, matrix
