"""Official timetable search using existing compile_trip/solve_trip; failures and bounded search stay visible."""

import math
import time
from decimal import Decimal, InvalidOperation
from itertools import product

from app.agent.data import chinatravel
from app.agent.generation.content.complete_travel import catalog, project
from app.agent.generation.content.constraint_candidate_policy import (
    DAY_CANDIDATE_LIMIT,
    TIMETABLE_SOLVE_SECONDS,
    TRIP_PLANNING_SECONDS,
    day_pool,
    hotel_matches,
    lodging_rooms,
    requested_names,
)
from app.agent.generation.content.constraint_problem import DayPlanningProblem, PlanningCandidate, minute, money_minor
from app.agent.generation.content.constraint_trip_solver import solve_trip
from app.schemas.travel_plan import TravelPlan


def hhmm(value):
    return f"{value // 60:02}:{value % 60:02}"


def _closing(row):
    start, end = minute(row["opentime"]), minute(row["endtime"])
    return min(1440, end + 1440 if end <= start else end)


def _route(city, first, second, mode, persons, cache):
    key = first, second, mode
    if key not in cache:
        legs = [] if first == second else chinatravel.route(city, first, second, "00:00", mode)
        cost = sum(
            Decimal(str(leg["cost"]))
            * (persons if leg["mode"] == "metro" else math.ceil(persons / 4) if leg["mode"] == "taxi" else 1)
            for leg in legs
        )
        duration = minute(legs[-1]["end_time"]) if legs else 0
        cache[key] = {
            "duration_min": duration,
            "source": "ChinaTravel-Sandbox/goto",
            "cost_minor": money_minor(Decimal(cost)),
            "mode": mode,
            "actual_modes": sorted({leg["mode"] for leg in legs}),
        }
    return cache[key]


def _nodes(request, facts, outbound, inbound):
    all_days, templates, positions = [], {}, {}
    persons = request.persons
    lodging = request.requirements_struct.lodging
    rooms = lodging_rooms(request.requirements_struct, persons)
    constraints = request.requirements_struct.travel_constraints
    hotel_rows = [
        row
        for row in facts["hotels"]
        if not lodging or not lodging.locked_hotel_identity or hotel_matches(row, lodging.locked_hotel_identity)
    ]
    hotels = day_pool(hotel_rows, 1, 3, requested_names(request.requirements_struct))
    for day in range(1, request.days + 1):
        nodes = []
        wanted = requested_names(request.requirements_struct, day)

        def add(key, row, role, low, high, duration, quantity=1, day=day, nodes=nodes):
            if (
                role in {"stay", "origin"}
                and constraints
                and constraints.room_beds is not None
                and int(row["numbed"]) != constraints.room_beds
            ):
                return
            if role in {"stay", "origin"} and rooms * int(row.get("capacity", 2)) < persons:
                return  # Existing two-per-room planning assumption unless explicit source capacity is available.
            if (
                role in {"stay", "origin"}
                and constraints
                and constraints.max_hotel_cost_per_person_per_night is not None
                and Decimal(str(row["price"])) * rooms > constraints.max_hotel_cost_per_person_per_night * persons
            ):
                return  # Exclude the same over-cap room from both overnight and next-day departure domains.
            item_type = {
                "stay": "hotel",
                "origin": "anchor",
                "lunch": "food",
                "dinner": "food",
                "out": "transport",
                "back": "transport",
            }.get(role, role)
            fixed = [
                f
                for f in request.requirements_struct.fixed_arrangements
                if f.day_no == day and f.name == row.get("name") and f.item_type == item_type
            ]
            if fixed:
                duration = minute(fixed[0].end_time) - minute(fixed[0].start_time)
            if high - low < duration or duration < 0 or (duration == 0 and role != "origin"):
                return
            price = row.get("Cost", row.get("price", 0))
            if any(f.cost is not None and Decimal(str(price)) != f.cost for f in fixed):
                return  # A user price and official unit price conflict; do not silently ignore or replace either.
            if role == "origin":
                price = 0
            value = {
                "type": {"out": row.get("_mode"), "back": row.get("_mode"), "stay": "accommodation"}.get(role, role),
                "start_time": "00:00",
                "end_time": "00:01",
                "price": price,
                "cost": round(price * quantity, 2),
                "transports": [],
            }
            if role in {"out", "back"}:
                value.update({"start": row["From"], "end": row["To"], "tickets": persons})
                value["TrainID" if row["_mode"] == "train" else "FlightID"] = row.get("TrainID", row.get("FlightID"))
                position = row["To"] if role == "out" else row["From"]
            else:
                value["position"] = position = row["name"]
                if role == "attraction":
                    value["tickets"] = persons
                if role == "stay":
                    value.update({"rooms": rooms, "room_type": int(row["numbed"])})
            identity = f"{role}:{day}:{key}" if role in {"stay", "origin"} else f"{role}:{key}"
            templates[identity], positions[identity] = value, position
            nodes.append(
                PlanningCandidate(
                    identity,
                    position,
                    duration,
                    money_minor(Decimal(str(value["cost"]))),
                    "CNY",
                    ((low, high),),
                    100 if role == "attraction" else 20 if role in {"lunch", "dinner"} else 0,
                    {
                        "stay": "hotel",
                        "origin": "anchor",
                        "lunch": "food",
                        "dinner": "food",
                        "out": "transport",
                        "back": "transport",
                    }.get(role, role),
                    categories=tuple(str(row.get(k, "")) for k in ("type", "cuisine", "featurehoteltype")),
                    source="ChinaTravel-Sandbox",
                )
            )

        if day == 1:
            add(
                "selected",
                outbound,
                "out",
                minute(outbound["BeginTime"]),
                minute(outbound["EndTime"]),
                minute(outbound["EndTime"]) - minute(outbound["BeginTime"]),
                persons,
            )
        if day == request.days:
            add(
                "selected",
                inbound,
                "back",
                minute(inbound["BeginTime"]),
                minute(inbound["EndTime"]),
                minute(inbound["EndTime"]) - minute(inbound["BeginTime"]),
                persons,
            )
        reserved = int(day == 1) + int(day == request.days) + len(hotels) * (int(day > 1) + int(day < request.days))
        attraction_limit = min(6, DAY_CANDIDATE_LIMIT - reserved - 8)
        attractions = day_pool(facts["candidates"], day, max(4, attraction_limit), wanted)
        for row in attractions:
            add(
                str(row["id"]),
                row,
                "attraction",
                minute(row["opentime"]),
                _closing(row),
                max(1, math.ceil(row["recommendmintime"] * 60)),
                persons,
            )
        day_foods = day_pool(facts["foods"], day, 4, wanted)
        for row in day_foods:
            for role, low, high in (("lunch", 11 * 60, 14 * 60), ("dinner", 17 * 60, 20 * 60)):
                add(
                    str(row["id"]),
                    row,
                    role,
                    max(low, minute(row["opentime"])),
                    min(high, _closing(row)),
                    45,
                    persons,
                )
        for row in hotels:
            if day < request.days:
                add(str(row["id"]), row, "stay", 0, 24 * 60, 30, rooms)
            if day > 1:
                add(str(row["id"]), row, "origin", 0, 24 * 60, 0)
        all_days.append(nodes)
    return all_days, templates, positions


def generate(request):
    facts = catalog(request.context, request.requirements_struct)
    deadline, trace, cache = time.monotonic() + TRIP_PLANNING_SECONDS, [], {}
    deadline = min(deadline, request.context.get("run_deadline_monotonic", deadline))
    request.context["travel_solver_trace"] = trace  # Retain excluded source records even if setup fails.

    def priced_services(direction):
        known = []
        for mode, rows in facts["intercity"][direction].items():
            for row in rows:
                try:
                    if not isinstance(row.get("Cost"), (int, float)) or isinstance(row["Cost"], bool):
                        raise ValueError("missing/non-numeric fare")
                    money_minor(Decimal(str(row["Cost"])))
                except (ValueError, InvalidOperation):
                    trace.append(
                        {"status": "unknown-intercity-fare", "direction": direction, "mode": mode, "source_row": row}
                    )
                    continue
                known.append(dict(row, _mode=mode))
        return known

    outgoing, incoming = priced_services("outbound"), priced_services("return")
    pairs = sorted(
        product(outgoing, incoming),
        key=lambda pair: (pair[0]["Cost"] + pair[1]["Cost"], pair[0]["BeginTime"], pair[1]["BeginTime"]),
    )
    eligible = []
    for outbound, inbound in pairs:
        constraints = request.requirements_struct.travel_constraints
        modes = {outbound["_mode"], inbound["_mode"]}
        if constraints and (
            (constraints.allowed_intercity_modes and not modes <= set(constraints.allowed_intercity_modes))
            or not set(constraints.required_intercity_modes) <= modes
        ):
            continue
        normal = minute(outbound["EndTime"]) > minute(outbound["BeginTime"]) and minute(inbound["EndTime"]) > minute(
            inbound["BeginTime"]
        )
        connected = request.days > 1 or minute(outbound["EndTime"]) < minute(inbound["BeginTime"])
        if normal and connected:
            eligible.append((outbound, inbound))
    trace.append(
        {
            "stage": "timetable-domain",
            "all_pairs": len(pairs),
            "eligible": len(eligible),
            "excluded_or_unsupported": len(pairs) - len(eligible),
            "search_pair_limit": 8,
        }
    )
    for outbound, inbound in eligible[:8]:
        if minute(outbound["EndTime"]) <= minute(outbound["BeginTime"]) or minute(inbound["EndTime"]) <= minute(
            inbound["BeginTime"]
        ):
            trace.append({"status": "unsupported-overnight-service", "out": outbound, "back": inbound})
            continue
        if request.days == 1 and minute(outbound["EndTime"]) >= minute(inbound["BeginTime"]):
            trace.append({"status": "incompatible-one-day-timetable", "out": outbound, "back": inbound})
            continue
        nodes, templates, positions = _nodes(request, facts, outbound, inbound)
        if time.monotonic() >= deadline:
            break
        problems = []
        for day, candidates in enumerate(nodes, 1):
            matrix = {}
            for first in candidates:
                if time.monotonic() >= deadline:
                    raise TimeoutError("complete CP batch budget exhausted during official route queries")
                for second in candidates:
                    if first == second:
                        continue
                    options = []
                    for mode in ("taxi", "metro", "walk"):
                        try:
                            options.append(
                                _route(
                                    request.city,
                                    positions[first.candidate_id],
                                    positions[second.candidate_id],
                                    mode,
                                    request.persons,
                                    cache,
                                )
                            )
                        except (ValueError, KeyError, TypeError) as exc:
                            cache[first.name, second.name, mode, "error"] = str(exc)
                    if options:
                        matrix[first.candidate_id, second.candidate_id] = {
                            **min(options, key=lambda option: option["duration_min"]),
                            "options": options,
                        }
            req = request.requirements_struct.model_copy(deep=True)
            req.budget_policy = None  # Exact complete-trip cap is compiled once, including transport arcs.
            for fixed in req.fixed_arrangements:
                if fixed.cost is not None:
                    fixed.cost *= (
                        request.requirements_struct.travel_constraints.rooms
                        if fixed.item_type == "hotel"
                        and request.requirements_struct.travel_constraints
                        and request.requirements_struct.travel_constraints.rooms
                        else request.requirements_struct.lodging.rooms
                        if fixed.item_type == "hotel"
                        and request.requirements_struct.lodging
                        and request.requirements_struct.lodging.rooms
                        else math.ceil(request.persons / 2)
                        if fixed.item_type == "hotel"
                        else request.persons
                    )
            problems.append(DayPlanningProblem(req, tuple(candidates), matrix, "CNY", day))
        results = solve_trip(
            problems,
            budget=Decimal(str(request.budget)) if request.budget is not None else None,
            time_limit_s=min(TIMETABLE_SOLVE_SECONDS, max(0.01, deadline - time.monotonic())),
            shrink_conflicts=False,
        )
        trace.append(
            {
                "mode": "per-edge-choice",
                "out": outbound,
                "back": inbound,
                "results": [{"status": r.status, "solver": r.solver_status} for r in results],
            }
        )
        if not all(r.status in {"OPTIMAL", "FEASIBLE"} for r in results):
            continue
        data = {
            "people_number": request.persons,
            "start_city": request.context["origin_city"],
            "target_city": request.city,
            "itinerary": [],
        }
        previous = None
        for day, result in enumerate(results, 1):
            activities = []
            chosen_modes = {(row["from_id"], row["to_id"]): row["mode"] for row in result.selected_routes}
            origin = next((v for v in result.visits if v.candidate_id.startswith("origin:")), None)
            if origin is not None:
                previous = positions[origin.candidate_id], hhmm(origin.end_min), origin.candidate_id
            for visit in result.visits:
                if visit.candidate_id.startswith("origin:"):
                    continue  # Internal overnight departure, never an invented meal or emitted activity.
                activity = dict(
                    templates[visit.candidate_id], start_time=hhmm(visit.start_min), end_time=hhmm(visit.end_min)
                )
                position = positions[visit.candidate_id]
                activity["transports"] = []
                if previous is not None and previous[0] != position:
                    selected_mode = chosen_modes[previous[2], visit.candidate_id]
                    for leg in chinatravel.route(request.city, previous[0], position, previous[1], selected_mode):
                        row = dict(leg, price=leg["cost"])
                        if row["mode"] == "metro":
                            row.update(tickets=request.persons, cost=round(row["cost"] * request.persons, 2))
                        elif row["mode"] == "taxi":
                            row.update(
                                cars=math.ceil(request.persons / 4),
                                cost=round(row["cost"] * math.ceil(request.persons / 4), 2),
                            )
                        activity["transports"].append(row)
                previous = position, activity["end_time"], visit.candidate_id
                activities.append(activity)
            data["itinerary"].append({"day": day, "activities": activities})
        issues = chinatravel.common_issues(data)
        trace[-1]["candidate"] = data
        trace[-1]["common_issues"] = issues
        if issues:
            continue
        attraction_count = sum(a["type"] == "attraction" for d in data["itinerary"] for a in d["activities"])
        trace[-1]["attraction_count"] = attraction_count
        if attraction_count == 0:
            # A checker loophole is not a useful trip; retain its actual output but keep searching.
            trace[-1]["candidate"] = data
            request.context["travel_last_output"] = data
            continue
        request.context["travel_last_output"], request.context["travel_solver_trace"] = data, trace
        return [day.model_dump() for day in project(TravelPlan.model_validate(data))], []
    request.context["travel_solver_trace"] = trace
    raise ValueError(
        "bounded CP timetable/candidate search produced no common-valid plan; retain failures, not global infeasibility"
    )
