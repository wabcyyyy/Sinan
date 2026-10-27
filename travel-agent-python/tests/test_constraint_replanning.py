import copy
import json
from itertools import permutations, product

from app.agent.editing.replan_intent import parse_replan_change
from app.agent.generation.content.constraint_replan_check import (
    item_positions,
    schedule_metrics,
    validate_replan_schedule,
)
from app.agent.generation.content.constraint_replan_model import ReplanModel
from app.agent.generation.content.constraint_trip_solver import solve_trip
from app.agent.generation.orchestration.constraint_replanning import _proposal, run_constraint_replan
from app.agent.generation.orchestration.replan_snapshot import create_snapshot
from app.schemas.replanning import ReplanChange
from app.schemas.trip_requirements import FixedArrangement, RequiredPlace
from tests.replanning_cases import case


def test_small_repair_matches_independent_start_time_and_order_enumeration():
    request, plans, routes = case()
    assert request.requirements_struct is not None
    event = ReplanChange(kind="delay", item_id=2, delay_minutes=20)
    result, proposals = run_constraint_replan(request, plans, event, [1], routes)
    option = next(o for o in result.options if o.key == "minimal")
    best = None
    for order in permutations((1, 2, 3)):
        for starts in product(range(540, 701, 10), repeat=3):
            by_id: dict[int, int] = dict(zip(order, starts, strict=True))
            if by_id[1] != 540 or by_id[2] < 600 or any(starts[i] + 40 > starts[i + 1] for i in (0, 1)):
                continue
            originals: dict[int, int] = {1: 540, 2: 580, 3: 620}
            score = (
                sum(by_id[k] != originals[k] for k in originals),
                sum(order.index(a) > order.index(b) for a, b in ((1, 2), (1, 3), (2, 3))),
                sum(abs(by_id[k] - originals[k]) for k in originals),
            )
            best = min(best, score) if best is not None else score
    m = option.metrics
    assert len(result.options) >= 2
    assert (m.changed_items, m.order_changes, m.start_shift_minutes) == best == (1, 1, 80)
    assert m.locked_violations == m.removed_items == m.cross_day_moves == 0
    assert proposals[option.key][0]["items"][0]["start_time"] == "09:00"


def test_local_infeasibility_expands_day_without_dropping_unapproved_items():
    request, plans, routes = case(count=5)
    assert request.requirements_struct is not None
    event = ReplanChange(kind="move", item_id=5, day_no=1, start_time="09:00")
    result, proposals = run_constraint_replan(request, plans, event, [], routes)
    assert result.attempts[0].scope == "local" and result.attempts[0].status == "INFEASIBLE"
    assert result.attempts[1].scope == "day" and result.options
    for option in result.options:
        assert option.metrics.removed_items == 0
        assert len(proposals[option.key][0]["items"]) == 5
        assert item_positions(proposals[option.key])[5][2]["start_time"] == "09:00"


def test_cross_day_expansion_recovers_feasibility_and_preserves_old_hard_constraint():
    request, plans, routes = case(count=2, days=2)
    assert request.requirements_struct is not None
    request.requirements_struct.day_windows[0].finish_by = "10:40"
    # P1 日窗同时保留，只能把非指定日必去 P2 以外的活动换到第二天。
    request.requirements_struct.required_places = [RequiredPlace(constraint_id="old-hard", name="P2", day_no=1)]
    event = ReplanChange(kind="appointment", item_id=2, day_no=1, start_time="09:20", end_time="10:20")
    result, proposals = run_constraint_replan(request, plans, event, [], routes)
    assert [a.scope for a in result.attempts] == ["local", "trip"]
    assert result.options and result.options[0].metrics.cross_day_moves == 1
    positions = item_positions(proposals[result.options[0].key])
    assert positions[2][0] == 1 and positions[1][0] == 2
    result_no_cross, _ = run_constraint_replan(request, plans, event, [], routes, allow_cross_day=False)
    assert not result_no_cross.options and result_no_cross.conflicts


def test_closure_of_locked_required_or_completed_activity_is_not_silently_relaxed():
    request, plans, routes = case()
    assert request.requirements_struct is not None
    request.requirements_struct.fixed_arrangements = [
        FixedArrangement(day_no=1, name="P2", item_type="attraction", start_time="09:40", end_time="10:10")
    ]
    result, _ = run_constraint_replan(request, plans, ReplanChange(kind="close", item_id=2), [], routes)
    assert not result.options and result.conflicts
    request.requirements_struct.fixed_arrangements = []
    result, _ = run_constraint_replan(request, plans, ReplanChange(kind="close", item_id=2), [2], routes)
    assert not result.options and result.conflicts
    result, _ = run_constraint_replan(
        request,
        plans,
        ReplanChange(kind="appointment", item_id=2, day_no=1, start_time="09:40", end_time="10:20"),
        [2],
        routes,
    )
    assert not result.options and result.conflicts


def test_independent_checker_rejects_tampering_and_unauthorized_removal():
    _, before, _ = case()
    after = copy.deepcopy(before)
    after[0]["items"].pop()
    errors = validate_replan_schedule(before, after, ReplanChange(kind="prefer", item_id=1), [], {})
    assert "移除了未经用户允许舍弃的安排" in errors
    after = copy.deepcopy(before)
    after[0]["items"][0]["start_time"] = "09:01"
    assert validate_replan_schedule(before, after, ReplanChange(kind="prefer", item_id=1), [1], {})


def test_early_return_uses_departure_policy_and_only_removes_explicit_optional_items():
    request, plans, routes = case(count=4)
    assert request.requirements_struct is not None
    request.requirements_struct.day_windows[0].kind = "full"
    for i, item in enumerate(plans[0]["items"]):
        start = 540 + i * 70
        item.update(
            start_time=f"{start // 60:02d}:{start % 60:02d}",
            end_time=f"{(start + 60) // 60:02d}:{(start + 60) % 60:02d}",
            duration_min=60,
        )
    event = ReplanChange(kind="finish_by", day_no=1, end_time="11:20")
    result, proposals = run_constraint_replan(request, plans, event, [], routes, optional_item_ids=[3, 4])
    assert result.options
    selected = proposals[result.options[0].key]
    assert selected[0]["_proposedRequirements"]["dayWindows"][0]["kind"] == "departure"
    assert {p["id"] for p in selected[0]["items"]} == {1, 2}
    assert all(p["end_time"] <= "11:20" for p in selected[0]["items"])


def test_natural_language_uses_same_ids_and_clarifies_negation_and_same_names():
    _, plans, _ = case()
    rows = [{**p, "day_no": 1} for p in plans[0]["items"]]
    parsed = parse_replan_change("推迟20分钟", rows, item_id=2)
    assert parsed.change == ReplanChange(kind="delay", item_id=2, delay_minutes=20)
    assert parse_replan_change("不要取消P2", rows, item_id=2).change is None
    rows.append({**rows[1], "id": 22})
    assert parse_replan_change("P2关闭", rows).change is None
    finish = parse_replan_change("第1天16:00结束", rows).change
    assert finish is not None and finish.kind == "finish_by"


def test_multi_turn_frozen_trajectory_reports_global_baseline_and_failures(tmp_path):
    request, plans, routes = case(count=4, days=2)
    assert request.requirements_struct is not None
    events = [
        ReplanChange(kind="delay", item_id=2, delay_minutes=20),
        ReplanChange(kind="appointment", item_id=3, day_no=1, start_time="11:30", end_time="12:00"),
        ReplanChange(kind="prefer", item_id=4),
        ReplanChange(kind="close", item_id=3),
        ReplanChange(kind="finish_by", day_no=2, end_time="11:40"),
    ]
    observations = []
    for event in events:
        result, proposals = run_constraint_replan(request, plans, event, [1], routes)
        row: dict = {"event": event.model_dump(mode="json"), "result": result.model_dump(mode="json", by_alias=True)}
        snapshot = create_snapshot(request, plans, event, [1], routes)
        problems, prepared, previous, protected, req, matrix, start = snapshot
        global_results = solve_trip(
            problems,
            budget=request.budget,
            time_limit_s=2,
            repair=ReplanModel(previous, event, {v.item_id for v in previous}, strategy="global", cross_day=True),
        )
        row["global_status"] = [r.status for r in global_results]
        if result.options and any(r.visits for r in global_results) and global_results[0].total_tiers:
            global_plan = _proposal(request, plans, global_results, prepared, matrix, start, req, [1])
            row["global_metrics"] = schedule_metrics(plans, global_plan, matrix, protected)[0].model_dump(mode="json")
        if result.options:
            chosen = next((o for o in result.options if o.key == "minimal"), result.options[0])
            plans = proposals[chosen.key]
            request.requirements_struct = req
        observations.append(row)
    assert observations[3]["result"]["options"] == []  # 后来的关闭不能覆盖前轮固定预约。
    assert all(o["metrics"]["lockedViolations"] == 0 for r in observations for o in r["result"]["options"])
    (tmp_path / "p2-frozen-trajectory.json").write_text(
        json.dumps(
            {"method": "synthetic frozen trajectory; no LLM or live tools", "observations": observations},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
