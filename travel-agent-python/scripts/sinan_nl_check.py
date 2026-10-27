"""Independent output/semantic checks. Runtime feedback takes no hidden labels."""

import unicodedata
from decimal import Decimal

from scripts.sinan_nl_cases import minutes


def key(value):
    return unicodedata.normalize("NFKC", str(value)).strip().casefold()


def atoms(state):
    req = state.get("requirements") or {}
    result = set()
    for field in ("city", "days", "persons", "budget", "start_date"):
        if state.get(field) is not None:
            value = (
                str(Decimal(str(state[field])).normalize())
                if field in {"days", "persons", "budget"}
                else key(state[field])
            )
            result.add((field, value))
    for row in req.get("required_places", []):
        day = 1 if row.get("day_no") is None and state.get("days") == 1 else row.get("day_no")
        result.add(("required", key(row["name"]), day))
    for name in req.get("excluded_places", []):
        result.add(("excluded", key(name)))
    for row in req.get("day_windows", []):
        for field in ("not_before", "finish_by"):
            if row.get(field):
                result.add((field, row["day_no"], row[field]))
    if req.get("max_attractions_per_day") is not None:
        result.add(("cap", req["max_attractions_per_day"]))
    if (req.get("budget_policy") or {}).get("mode"):
        result.add(("budget_mode", req["budget_policy"]["mode"]))
    if (req.get("budget_policy") or {}).get("include_intercity_transport") is not None:
        result.add(("budget_intercity", req["budget_policy"]["include_intercity_transport"]))
    for row in req.get("place_relations", []):
        names = tuple(key(n) for n in row["place_names"])
        if row["kind"] == "one_of":
            names = tuple(sorted(names))
        result.add(("relation", row["kind"], names, row["day_no"], row.get("strength", "hard")))
        # A hard before already entails both day-specific must-visits. Explicitly listing
        # those same must-visits is redundant, not a false-positive interpretation.
        if row["kind"] == "before" and row.get("strength", "hard") == "hard":
            result.update(("required", name, row["day_no"]) for name in names)
    for field in ("pace", "transport_preference", "dietary_requirement", "lodging", "excluded_categories"):
        if req.get(field):
            result.add(("other", field, str(req[field])))
    return result


def semantics(gold, predicted):
    truth, actual = atoms(gold), atoms(predicted)
    hard = {item for item in truth if not (item[0] == "relation" and item[-1] == "soft")}
    soft_truth = {item[:-1] for item in truth if item[0] == "relation" and item[-1] == "soft"}
    hard_actual = {item[:-1] for item in actual if item[0] == "relation" and item[-1] == "hard"}
    return {
        "tp": len(truth & actual),
        "predicted": len(actual),
        "gold": len(truth),
        "precision": len(truth & actual) / len(actual) if actual else 0,
        "recall": len(truth & actual) / len(truth) if truth else 1,
        "hard_complete": hard <= actual,
        "hard_soft_errors": len(soft_truth & hard_actual),
        "missing": sorted(map(repr, truth - actual)),
        "extra": sorted(map(repr, actual - truth)),
    }


def schedule_checks(public, state, plans):
    """No solver variables, no gold. All time, selection, cost tests read output only."""
    facts = public["facts"]
    pool = {row["id"]: row for row in facts["candidates"]}
    routes = {(a, b): duration for a, b, duration in facts["routes"]}
    req = state.get("requirements") or {}
    errors, checks, positions, seen = [], [], {}, set()

    def check(label, passed):
        checks.append({"label": label, "passed": bool(passed)})
        if not passed:
            errors.append(label)

    try:
        days, persons = int(state["days"]), int(state["persons"])
        check("days", sorted(day["day_no"] for day in plans) == list(range(1, days + 1)))
        cost, unknown = 0, False
        for day in plans:
            number = day["day_no"]
            windows = [w for w in req.get("day_windows", []) if w["day_no"] == number]
            start = minutes(windows[0].get("not_before") or "00:00") if windows else 0
            end = minutes(windows[0].get("finish_by") or "24:00") if windows else 1440
            items = day["items"]
            check(f"day-{number}:nonempty", bool(items))
            check(f"day-{number}:cap", len(items) <= min(req.get("max_attractions_per_day") or 6, 6))
            active = 0
            for index, item in enumerate(items):
                candidate = pool.get(item["id"])
                check(f"entity:{item['id']}", candidate is not None)
                if candidate is None:
                    continue
                a, b = minutes(item["start"]), minutes(item["end"])
                check(f"unique:{item['id']}", item["id"] not in seen)
                seen.add(item["id"])
                positions[key(candidate["name"])] = (number, a, b)
                check(f"duration:{item['id']}", b - a == candidate["duration_min"])
                check(f"day-window:{item['id']}", start <= a < b <= end)
                if candidate["opening"] is None or candidate["price"] is None:
                    unknown = True
                else:
                    check(f"opening:{item['id']}", any(x <= a and b <= y for x, y in candidate["opening"]))
                active += b - a
                if candidate["price"] is not None:
                    cost += candidate["price"] * persons
                if index:
                    previous = items[index - 1]
                    travel = routes.get((previous["id"], item["id"]))
                    if travel is None:
                        unknown = True
                    else:
                        check(f"transfer:{previous['id']}->{item['id']}", a >= minutes(previous["end"]) + travel)
            # The existing ordinary full-day gate, independently restated from the frozen spec.
            check(f"day-{number}:active-max", active <= min(480, end - start))
            if not windows or end - start >= 480:
                check(f"day-{number}:active-min", active >= 240)
        for row in req.get("required_places", []):
            found = positions.get(key(row["name"]))
            check(
                f"required:{row['name']}",
                found is not None and (row.get("day_no") is None or found[0] == row["day_no"]),
            )
        for name in req.get("excluded_places", []):
            check(f"excluded:{name}", key(name) not in positions)
        for relation in req.get("place_relations", []):
            if relation.get("strength", "hard") != "hard":
                continue
            a, b = (positions.get(key(n)) for n in relation["place_names"])
            if relation["kind"] == "before":
                passed = a is not None and b is not None and a[0] == b[0] == relation["day_no"] and a[2] <= b[1]
            else:
                passed = any(p is not None and p[0] == relation["day_no"] for p in (a, b))
            check(f"relation:{relation['constraint_id']}", passed)
        if (req.get("budget_policy") or {}).get("mode") == "hard_cap":
            check("activity-budget", state.get("budget") is not None and cost <= float(state["budget"]))
        if req.get("unresolved_requests"):
            unknown = True
        return {"errors": errors, "checks": checks, "unknown": unknown, "known_cost": cost}
    except (ValueError, TypeError, KeyError, IndexError) as exc:
        return {"errors": [*errors, f"invalid-output:{type(exc).__name__}:{exc}"], "checks": checks, "unknown": True}


def runtime_feedback(public, output):
    """B checks its own declared interpretation, never an oracle interpretation."""
    state = output.get("state") or {}
    result = schedule_checks(public, state, output.get("plans") or [])
    if output.get("status") == "FEASIBLE":
        return [*result["errors"], *(["所选事实仍有缺口，不得宣称FEASIBLE"] if result["unknown"] else [])]
    return []  # No hidden feasibility oracle is offered to a refusal.


def evaluate(row, output, expected):
    semantic = semantics(row["gold"], output.get("state") or {})
    check = schedule_checks(row, row["gold"], output.get("plans") or [])
    status = output.get("status", "ERROR")
    correct = status == expected and (not check["errors"] if expected == "FEASIBLE" else True)
    return {
        "status": status,
        "expected": expected,
        "passed": correct,
        "hard_preserved_and_passed": correct and semantic["hard_complete"],
        "false_feasible": status == "FEASIBLE" and (expected != "FEASIBLE" or bool(check["errors"])),
        "semantics": semantic,
        "schedule": check,
    }
