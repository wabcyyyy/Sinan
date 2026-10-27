"""自制冻结事实；地点为抽象候选，不宣称城市事实或线上路线。"""

from decimal import Decimal
from random import Random

from app.agent.generation.content.constraint_problem import DayPlanningProblem, PlanningCandidate
from app.schemas.trip_requirements import (
    BudgetPolicy,
    DayWindow,
    FixedArrangement,
    PlaceRelation,
    RequiredPlace,
    TripRequirements,
)


def frozen_problem(size=5, seed=7, *, infeasible=False):
    rng = Random(seed)
    candidates = tuple(
        PlanningCandidate(
            candidate_id=f"p{i}",
            name=f"地点{i}",
            duration_min=60 if i == 0 else rng.choice([30, 45, 60, 90]),
            price_minor=1000 if i == 0 else rng.choice([500, 1000, 1500, 2500]),
            currency="CNY",
            opening_windows=((540, 1020),) if i % 3 else ((540, 720), (780, 1020)),
            utility=rng.randint(1, 9),
            categories=("museum",) if i % 4 == 0 else (),
        )
        for i in range(size)
    )
    matrix = {
        (a.candidate_id, b.candidate_id): {
            "from_poi_id": a.candidate_id,
            "to_poi_id": b.candidate_id,
            "duration_min": rng.randint(5, 35),
            "source": "frozen-directed-fixture",
            "degraded": False,
        }
        for a in candidates
        for b in candidates
        if a != b
    }
    req = TripRequirements(
        day_windows=[DayWindow(day_no=1, not_before="09:00", finish_by="17:00", source_turn_id="turn-1")],
        required_places=[RequiredPlace(constraint_id="must-1", name="地点1", day_no=1, source_turn_id="turn-2")],
        fixed_arrangements=[
            FixedArrangement(day_no=1, name="地点0", item_type="attraction", start_time="10:00", end_time="11:00")
        ],
        place_relations=[
            PlaceRelation(
                constraint_id="order",
                kind="before",
                place_names=["地点0", "地点1"],
                day_no=1,
                source_text="先去地点0再去地点1",
                source_turn_id="turn-3",
            ),
            PlaceRelation(constraint_id="choice", kind="one_of", place_names=["地点2", "地点3"], day_no=1),
        ],
        budget_policy=BudgetPolicy(mode="hard_cap"),
        max_attractions_per_day=5,
    )
    return DayPlanningProblem(req, candidates, matrix, "CNY", budget=Decimal("0" if infeasible else "65"))
