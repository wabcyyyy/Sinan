"""Regressions from the predeclared low-thinking development run; no paid calls."""

from app.agent.editing.clarify_contract import reconcile_person_count
from app.schemas.trip_requirements import IntakeState


def test_direct_count_does_not_hide_conflicting_companion_total():
    state = IntakeState(persons=4)
    reconcile_person_count(state, "旅行人数4。我和4个同学从上海去杭州。")
    assert state.persons is None and state.person_count_options == [4, 5]
    reconcile_person_count(state, "直接开始")
    assert state.persons is None and state.person_count_options == [4, 5]
    reconcile_person_count(state, "确认4人")
    assert state.persons == 4 and state.person_count_options == []


def test_matching_companion_total_is_not_a_conflict():
    state = IntakeState(persons=4)
    reconcile_person_count(state, "出行人数4，我和3个朋友一起。")
    assert state.persons == 4 and state.person_count_options == []
