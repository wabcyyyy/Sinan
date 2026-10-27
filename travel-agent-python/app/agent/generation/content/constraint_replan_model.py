"""在同一 CP 模型上添加冻结、操作与扰动目标；不保留旧搜索树。"""

from dataclasses import dataclass, field
from itertools import combinations

from app.agent.generation.content.constraint_problem import minute
from app.schemas.replanning import ReplanChange


@dataclass(frozen=True)
class PreviousVisit:
    item_id: int
    key: str
    day_no: int
    start: int
    end: int
    order: int
    protected: bool = False


@dataclass
class ReplanModel:
    previous: list[PreviousVisit]
    change: ReplanChange
    movable: set[int]
    strategy: str = "minimal"
    cross_day: bool = False
    bounds: dict = field(default_factory=dict)
    terms: dict = field(default_factory=dict)
    removable: set[int] = field(default_factory=set)
    use_hints: bool = True


def describe_operation(event, name, anchor=""):
    if event.kind == "move":
        order = f"，放在「{anchor}」{'之前' if event.placement == 'before' else '之后'}" if anchor else ""
        return f"把「{name}」移到第{event.day_no}天" + (f" {event.start_time}" if event.start_time else "") + order
    if event.kind == "delay":
        return f"本轮将「{name}」至少推迟 {event.delay_minutes} 分钟"
    if event.kind == "close":
        return f"按用户声明移除当前关闭/取消的「{name}」；未在线核实闭馆事实"
    if event.kind == "finish_by":
        return f"第{event.day_no}天需在 {event.end_time} 前结束，保留原有更严格的时窗"
    if event.kind == "appointment":
        return f"新增「{name}」第{event.day_no}天 {event.start_time}–{event.end_time} 固定预约"
    return f"软偏好优先保留「{name}」，不能覆盖既有硬约束"


def _variables(days):
    return {(c.candidate_id, d.problem.day_no): (d, i) for d in days for i, c in enumerate(d.problem.candidates)}


def _freeze(days, repair, variables):
    for old in repair.previous:
        frozen = old.protected or old.item_id not in repair.movable
        for day in days:
            d, i = variables[old.key, day.problem.day_no]
            # 旧解只是提示；固定与已执行项仍是硬条件。
            if repair.use_hints:
                d.model.add_hint(d.selected[i], int(d.problem.day_no == old.day_no))
                d.model.add_hint(d.starts[i], old.start if d.problem.day_no == old.day_no else 0)
            allowed_day = (
                repair.change.day_no
                if old.item_id == repair.change.item_id and repair.change.kind in {"move", "appointment"}
                else old.day_no
            )
            if not frozen and not repair.cross_day and d.problem.day_no != allowed_day:
                d.model.add(d.selected[i] == 0)
            if frozen:
                guard = d.hard(
                    f"freeze:{old.item_id}:{d.problem.day_no}",
                    "locked_item",
                    "保留已执行/锁定或当前邻域外的条目",
                    [d.problem.candidates[i].name],
                )
                d.model.add(d.selected[i] == int(d.problem.day_no == old.day_no)).only_enforce_if(guard)
                if d.problem.day_no == old.day_no:
                    d.model.add(d.starts[i] == old.start).only_enforce_if(guard)
                    d.model.add(d.starts[i] + d.problem.candidates[i].duration_min == old.end).only_enforce_if(guard)


def _operation(days, repair, variables):
    event, model = repair.change, days[0].model
    old = next((v for v in repair.previous if v.item_id == event.item_id), None)
    if old is None and event.kind not in {"finish_by", "prefer"}:
        raise ValueError("修改目标不在原行程中")
    target_day = event.day_no or (old.day_no if old else 1)
    target = next(d for d in days if d.problem.day_no == target_day)
    name = next((c.name for c in target.problem.candidates if old and c.candidate_id == old.key), "")
    anchor_name = next(
        (c.name for c in target.problem.candidates if c.candidate_id == f"item:{event.anchor_item_id}"), ""
    )
    guard = target.hard(
        "change:requested", "place_relation", describe_operation(event, name, anchor_name), [name] if name else []
    )
    if event.kind == "close":
        assert old is not None
        for day in days:
            d, i = variables[old.key, day.problem.day_no]
            model.add(d.selected[i] == 0).only_enforce_if(guard)
    elif event.kind == "finish_by":
        for i, candidate in enumerate(target.problem.candidates):
            model.add(target.starts[i] + candidate.duration_min <= minute(event.end_time)).only_enforce_if(
                [guard, target.selected[i]]
            )
    elif event.kind != "prefer":
        assert old is not None
        d, i = variables[old.key, target_day]
        model.add(d.selected[i] == 1).only_enforce_if(guard)
        if event.kind == "delay":
            model.add(d.starts[i] >= old.start + event.delay_minutes).only_enforce_if(guard)
        if event.start_time:
            model.add(d.starts[i] == minute(event.start_time)).only_enforce_if(guard)
        if event.anchor_item_id:
            anchor = next(v for v in repair.previous if v.item_id == event.anchor_item_id)
            a, j = variables[anchor.key, target_day]
            model.add(a.selected[j] == 1).only_enforce_if(guard)
            first, second = (i, j) if event.placement == "before" else (j, i)
            model.add(d.starts[first] + d.problem.candidates[first].duration_min <= d.starts[second]).only_enforce_if(
                guard
            )


def _disturbance(days, repair, variables):
    model = days[0].model
    removed, moved, changed, shifts, inverted = [], [], [], [], []
    for old in repair.previous:
        choices = [variables[old.key, d.problem.day_no] for d in days]
        selected = sum(d.selected[i] for d, i in choices)
        removed.append(1 - selected)
        moved.extend(d.selected[i] for d, i in choices if d.problem.day_no != old.day_no)
        same_day, index = variables[old.key, old.day_no]
        stable = model.new_bool_var(f"unchanged:{old.item_id}")
        same_start = model.new_bool_var(f"same-start:{old.item_id}")
        model.add(same_day.starts[index] == old.start).only_enforce_if(same_start)
        model.add(same_day.starts[index] != old.start).only_enforce_if(same_start.Not())
        if same_day.problem.candidates[index].duration_min != old.end - old.start:
            model.add(stable == 0)
        else:
            model.add_bool_and([same_day.selected[index], same_start]).only_enforce_if(stable)
            model.add_bool_or([same_day.selected[index].Not(), same_start.Not()]).only_enforce_if(stable.Not())
        changed.append(1 - stable)
        for d, i in choices:
            absolute = model.new_int_var(0, 1440, f"shift:{old.item_id}:{d.problem.day_no}")
            model.add_abs_equality(absolute, d.starts[i] - old.start)
            paid = model.new_int_var(0, 1440, f"active-shift:{old.item_id}:{d.problem.day_no}")
            model.add(paid == absolute).only_enforce_if(d.selected[i])
            model.add(paid == 0).only_enforce_if(d.selected[i].Not())
            shifts.append(paid)
    for first, second in combinations(repair.previous, 2):
        if first.day_no != second.day_no:
            continue
        for day in days:
            d, i = variables[first.key, day.problem.day_no]
            _, j = variables[second.key, day.problem.day_no]
            flip = model.new_bool_var(f"invert:{first.item_id}:{second.item_id}:{d.problem.day_no}")
            model.add_bool_and([d.selected[i], d.selected[j]]).only_enforce_if(flip)
            model.add(d.starts[i] > d.starts[j]).only_enforce_if(flip)
            model.add(d.starts[i] < d.starts[j]).only_enforce_if([d.selected[i], d.selected[j], flip.Not()])
            inverted.append(flip)
    return {
        "removed": sum(removed),
        "cross_day": sum(moved),
        "changed": sum(changed),
        "shift": sum(shifts),
        "order": sum(inverted),
        "travel": -sum(d.objectives[-2] for d in days),
    }


def apply_replan(days, repair):
    variables = _variables(days)
    for old in repair.previous:
        if old.item_id not in repair.removable and not (
            repair.change.kind == "close" and old.item_id == repair.change.item_id
        ):
            guard = days[0].hard(
                f"keep:{old.item_id}", "required_place", "未授权移除的已有安排持续保留", [str(old.item_id)]
            )
            days[0].model.add(
                sum(d.selected[i] for day in days for d, i in [variables[old.key, day.problem.day_no]]) == 1
            ).only_enforce_if(guard)
    _freeze(days, repair, variables)
    _operation(days, repair, variables)
    repair.terms = _disturbance(days, repair, variables)
    for key, value in repair.bounds.items():
        days[0].model.add(repair.terms[key] <= value)


def replan_objectives(days, repair, standard):
    if repair.strategy == "global":
        return standard
    terms = repair.terms
    if repair.strategy == "time":
        return [-terms[k] for k in ("removed", "cross_day", "shift", "changed", "order")] + standard
    order = (
        ["removed", "cross_day", "travel", "changed", "order", "shift"]
        if repair.strategy == "route"
        else ["removed", "cross_day", "changed", "order", "shift"]
    )
    return [-terms[key] for key in order] + standard
