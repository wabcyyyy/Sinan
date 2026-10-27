from types import SimpleNamespace

import pytest

from app.common.envelope import ApiError
from app.schemas.planning import ConstraintPlanningOptions
from app.schemas.trip_requirements import FixedArrangement, TripRequirements
from app.services.itinerary_plan_apply import _retain_fixed


@pytest.mark.parametrize("has_old", [False, True])
def test_only_confirmed_replan_appointment_can_add_fixed_requirement(has_old):
    old = FixedArrangement(day_no=1, name="原预约", item_type="attraction", start_time="09:00", end_time="10:00")
    new = FixedArrangement(day_no=1, name="新预约", item_type="attraction", start_time="14:00", end_time="15:00")
    injected = new.model_copy(update={"name": "未确认的额外预约"})
    current = TripRequirements(
        constraint_planning=ConstraintPlanningOptions(country_code="FR", time_zone="Europe/Paris", currency="EUR"),
        fixed_arrangements=[old] if has_old else [],
    )
    main = SimpleNamespace(requirements_json=current.model_dump(mode="json", by_alias=True))
    proposed = current.model_copy(update={"fixed_arrangements": [new, injected]}, deep=True)
    plain = _retain_fixed(main, [{}], proposed.model_copy(deep=True))
    assert plain is not None
    assert plain.fixed_arrangements == current.fixed_arrangements
    proof = {
        "operation": {"kind": "appointment", "item_id": 1, "day_no": 1, "start_time": "14:00", "end_time": "15:00"},
        "before": [{"items": [{"id": 1, "poi_name": "新预约", "item_type": "attraction"}]}],
    }
    result = _retain_fixed(main, [{"_replanProof": proof}], proposed)
    assert result is not None
    assert result.fixed_arrangements == ([old, new] if has_old else [new])
    with pytest.raises(ApiError, match="预约要求"):
        _retain_fixed(main, [{"_replanProof": proof}], current.model_copy(deep=True))
