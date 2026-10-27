"""P0 独立参考：只读问题/最终安排，不导入 CP 模型或编译器。

固定顺序下，非负转场 + 固定时长 + 多段可用窗的最早安排具有支配性；因此
穷举全部子集排列，再前向取最早时刻，足以核对本原型的可行性和分层目标。
最多 8 候选，不能用于规模实验。
"""

from itertools import pairwise, permutations

from app.agent.generation.content.constraint_problem import DayPlanningProblem, ScheduledVisit


def _minute(value):
    return int(value[:2]) * 60 + int(value[3:])


def _name(value):
    return "".join(value.casefold().split())


def _bounds(problem):
    windows = [w for w in problem.requirements.day_windows if w.day_no == problem.day_no]
    return (
        max([_minute(w.not_before) if w.not_before else 480 for w in windows] or [480]),
        min([_minute(w.finish_by) if w.finish_by else 1200 for w in windows] or [1200]),
    )


def _relation_holds(relation, visits, candidates):
    matches = [
        [v for v in visits if _name(candidates[v.candidate_id].name) == _name(name)] for name in relation.place_names
    ]
    if relation.kind == "one_of":
        return any(matches)
    return bool(all(matches) and matches[0][0].end_min <= matches[1][0].start_min)


def schedule_violations(problem: DayPlanningProblem, visits: list[ScheduledVisit]) -> list[str]:
    candidates = {c.candidate_id: c for c in problem.candidates}
    req, errors = problem.requirements, []
    ids = [v.candidate_id for v in visits]
    if len(set(ids)) != len(ids) or any(key not in candidates for key in ids):
        return ["重复或不存在的候选"]
    low, high = _bounds(problem)
    names = {_name(candidates[key].name) for key in ids}
    for visit in visits:
        candidate = candidates[visit.candidate_id]
        if (
            visit.end_min - visit.start_min != candidate.duration_min
            or not low <= visit.start_min < visit.end_min <= high
        ):
            errors.append("日窗/时长")
        if candidate.opening_windows is not None and not any(
            start <= visit.start_min and visit.end_min <= end for start, end in candidate.opening_windows
        ):
            errors.append("营业窗")
        if _name(candidate.name) in {_name(name) for name in req.excluded_places}:
            errors.append("排除地点")
        if any(cat in (candidate.categories or ()) for cat in req.excluded_categories):
            errors.append("排除类别")
    for first, second in pairwise(visits):
        duration = problem.route_matrix.get((first.candidate_id, second.candidate_id), {}).get("duration_min", 0)
        if first.end_min + duration > second.start_min:
            errors.append("有向转场/顺序")
    for place in req.required_places:
        if place.day_no == problem.day_no and _name(place.name) not in names:
            errors.append("必去")
    for fixed in req.fixed_arrangements:
        if fixed.day_no == problem.day_no and not any(
            _name(candidates[v.candidate_id].name) == _name(fixed.name)
            and candidates[v.candidate_id].item_type == fixed.item_type
            and v.start_min == _minute(fixed.start_time)
            and v.end_min == _minute(fixed.end_time)
            for v in visits
        ):
            errors.append("固定预约")
    for relation in req.place_relations:
        if (
            relation.day_no == problem.day_no
            and relation.strength == "hard"
            and not _relation_holds(relation, visits, candidates)
        ):
            errors.append(f"关系:{relation.constraint_id}")
    if (
        req.max_attractions_per_day is not None
        and sum(candidates[key].item_type == "attraction" for key in ids) > req.max_attractions_per_day
    ):
        errors.append("景点上限")
    if (
        req.budget_policy
        and req.budget_policy.mode == "hard_cap"
        and problem.budget is not None
        and _cost(problem, visits) > int(problem.budget * 100)
    ):
        errors.append("硬预算")
    return errors


def _cost(problem, visits):
    candidates = {c.candidate_id: c for c in problem.candidates}
    total = 0
    for visit in visits:
        candidate = candidates[visit.candidate_id]
        price = candidate.price_minor if candidate.currency == problem.currency else None
        for fixed in problem.requirements.fixed_arrangements:
            if (
                fixed.day_no == problem.day_no
                and _name(fixed.name) == _name(candidate.name)
                and fixed.item_type == candidate.item_type
                and fixed.cost is not None
            ):
                price = int(fixed.cost * 100)
        total += price or 0
    return total


def objective(problem, visits):
    candidates = {c.candidate_id: c for c in problem.candidates}
    soft = [r for r in problem.requirements.place_relations if r.day_no == problem.day_no and r.strength == "soft"]
    priorities = sorted({r.priority for r in soft}, reverse=True)
    score = [
        sum(_relation_holds(r, visits, candidates) for r in soft if r.priority == priority) for priority in priorities
    ]
    travel = sum(
        problem.route_matrix.get((a.candidate_id, b.candidate_id), {}).get("duration_min", 0)
        for a, b in pairwise(visits)
    )
    return (*score, sum(candidates[v.candidate_id].utility for v in visits), -travel, -sum(v.start_min for v in visits))


def _earliest(problem, order):
    low, high = _bounds(problem)
    visits = []
    for candidate in order:
        earliest = low
        if visits:
            previous = visits[-1]
            earliest = previous.end_min + problem.route_matrix.get(
                (previous.candidate_id, candidate.candidate_id), {}
            ).get("duration_min", 0)
        fixed = [
            f
            for f in problem.requirements.fixed_arrangements
            if f.day_no == problem.day_no
            and _name(f.name) == _name(candidate.name)
            and f.item_type == candidate.item_type
        ]
        if fixed:
            starts = [_minute(fixed[0].start_time)]
        else:
            starts = [max(earliest, start) for start, _ in candidate.opening_windows or ((low, high),)]
        valid = [
            start
            for start in starts
            if start >= earliest
            and start + candidate.duration_min <= high
            and (
                candidate.opening_windows is None
                or any(
                    begin <= start and start + candidate.duration_min <= end for begin, end in candidate.opening_windows
                )
            )
        ]
        if not valid:
            return None
        start = min(valid)
        visits.append(ScheduledVisit(candidate.candidate_id, start, start + candidate.duration_min))
    return visits


def exhaustive_day(problem: DayPlanningProblem):
    if len(problem.candidates) > 8:
        raise ValueError("独立穷举仅允许最多 8 候选")
    best, best_score, examined = None, None, 0
    for count in range(len(problem.candidates) + 1):
        for order in permutations(problem.candidates, count):
            examined += 1
            visits = _earliest(problem, order)
            if visits is None or schedule_violations(problem, visits):
                continue
            score = objective(problem, visits)
            if best_score is None or score > best_score:
                best, best_score = visits, score
    return best, best_score, examined
