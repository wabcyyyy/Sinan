"""Deterministic checks against producer-parsed NL and public source fields; no benchmark oracle."""

import math
from decimal import Decimal

from app.agent.generation.content.constraint_candidate_policy import hotel_matches


def _typed_issues(plan, request):
    requirements = request.requirements_struct
    issues = []
    if [day.day for day in plan.itinerary] != list(range(1, request.days + 1)):
        issues.append("complete plan must contain every requested day exactly once in order")
    if request.budget is not None and plan.total_cost() > Decimal(str(request.budget)):
        issues.append("complete trip exceeds parsed total budget including all activities and transport legs")
    kind = {
        "attraction": {"attraction"},
        "food": {"breakfast", "lunch", "dinner"},
        "hotel": {"accommodation"},
        "transport": {"train", "airplane"},
    }
    hotels = {row["name"]: row for row in request.context.get("hotels", [])}
    expected_nights = request.context.get("requested_stay_nights", request.days - 1 if request.needs_hotel else 0)
    if sum(a.type == "accommodation" for d in plan.itinerary for a in d.activities) != expected_nights:
        issues.append("complete plan accommodation nights differ from requested stay nights")
    for place in requirements.required_places:
        if not any(
            a.position == place.name
            for d in plan.itinerary
            if place.day_no is None or d.day == place.day_no
            for a in d.activities
        ):
            issues.append(f"required place/day missing: {place.name}")
    for fixed in requirements.fixed_arrangements:
        matches = [
            a
            for d in plan.itinerary
            if d.day == fixed.day_no
            for a in d.activities
            if a.position == fixed.name and a.type in kind[fixed.item_type]
        ]
        if not any(
            a.start_time == fixed.start_time
            and a.end_time == fixed.end_time
            and (fixed.cost is None or Decimal(str(a.price)) == fixed.cost)
            for a in matches
        ):
            issues.append(f"fixed arrangement not retained: day{fixed.day_no} {fixed.name}")
    for relation in requirements.place_relations:
        if relation.strength != "hard":
            continue
        day = next((d for d in plan.itinerary if d.day == relation.day_no), None)
        groups = [[a for a in (day.activities if day else []) if a.position == name] for name in relation.place_names]
        if relation.kind == "one_of":
            passed = any(groups)
        else:
            passed = all(len(group) == 1 for group in groups) and groups[0][0].end_time <= groups[1][0].start_time
        if not passed:
            issues.append(f"hard place relation not retained: {relation.constraint_id}")
    for day in plan.itinerary:
        if 1 < day.day < request.days and not any(
            a.type in {"attraction", "breakfast", "lunch", "dinner"} for a in day.activities
        ):
            issues.append(f"day{day.day} has no substantive activity or meal; not a complete playable day")
        for window in requirements.day_windows:
            if window.day_no == day.day and any(
                (window.not_before and a.start_time < window.not_before)
                or (window.finish_by and a.end_time > window.finish_by)
                for a in day.activities
            ):
                issues.append(f"day{day.day} violates the parsed day window")
        lodgings = [a for a in day.activities if a.type == "accommodation"]
        if request.needs_hotel and day.day < request.days and len(lodgings) != 1:
            issues.append(f"day{day.day} must retain exactly one overnight accommodation")
        for activity in lodgings:
            # Match the existing room-option default; it is a planning assumption if the source has no capacity field.
            capacity = int(hotels.get(activity.position, {}).get("capacity", 2))
            if (activity.rooms or 0) * capacity < request.persons:
                issues.append("hotel rooms do not accommodate parsed persons under the declared room capacity")
            if requirements.lodging:
                if requirements.lodging.rooms is not None and activity.rooms != requirements.lodging.rooms:
                    issues.append("hotel room count differs from explicit lodging requirement")
                if requirements.lodging.locked_hotel_identity and not hotel_matches(
                    hotels.get(activity.position, {"name": activity.position}),
                    requirements.lodging.locked_hotel_identity,
                ):
                    issues.append("locked hotel identity changed")
    selected = {a.position for d in plan.itinerary for a in d.activities}
    if any(name in selected for name in requirements.excluded_places):
        issues.append("excluded place selected")
    return issues


def request_issues(plan, request):
    issues = _typed_issues(plan, request)
    activities = [a for day in plan.itinerary for a in day.activities]
    if len(plan.itinerary) != request.days or plan.people_number != request.persons:
        issues.append("parsed request day/person count differs from generated complete plan")
    if plan.target_city != request.city or plan.start_city != request.context["origin_city"]:
        issues.append("generated city pair differs from parsed request")
    if request.budget is not None and plan.total_cost() > request.budget:
        issues.append(f"complete trip cost {plan.total_cost()} exceeds parsed budget {request.budget}")
    for activity in activities:
        if activity.type in {"attraction", "train", "airplane"} and activity.tickets != request.persons:
            issues.append(
                f"{activity.position or activity.train_id or activity.flight_id}: tickets must match parsed persons"
            )
        for leg in activity.transports:
            if leg.mode == "metro" and leg.tickets != request.persons:
                issues.append("metro tickets must match parsed persons")
            if leg.mode == "taxi" and leg.cars != math.ceil(request.persons / 4):
                issues.append("taxi cars must accommodate parsed persons at four per car")
    requirements = request.requirements_struct
    constraints = requirements.travel_constraints if requirements else None
    if constraints is None:
        return [*issues, "NL travel attribute binding missing"]
    names = {a.position for a in activities if a.position}
    for name in constraints.required_names:
        if name not in names:
            issues.append(f"parsed required place missing: {name}")
    for name in constraints.excluded_names:
        if name in names:
            issues.append(f"parsed excluded place selected: {name}")
    for key, attribute, roles, field in (
        ("candidates", "attraction_types", {"attraction"}, "type"),
        ("foods", "restaurant_cuisines", {"lunch", "dinner", "breakfast"}, "cuisine"),
        ("hotels", "hotel_features", {"accommodation"}, "featurehoteltype"),
    ):
        lookup = {row["name"]: row[field] for row in request.context[key]}
        actual = {lookup.get(a.position) for a in activities if a.type in roles}
        for value in getattr(constraints, attribute):
            if value not in actual:
                issues.append(f"parsed {attribute} missing: {value}")
    intercity = {a.type for a in activities if a.type in {"train", "airplane"}}
    innercity = {leg.mode for a in activities for leg in a.transports}
    if constraints.allowed_intercity_modes and not intercity <= set(constraints.allowed_intercity_modes):
        issues.append("parsed allowed intercity modes violated")
    if not set(constraints.required_intercity_modes) <= intercity:
        issues.append("parsed required intercity modes missing")
    if not set(constraints.required_innercity_modes) <= innercity:
        issues.append("parsed required innercity modes missing")
    for activity in activities:
        if activity.type == "accommodation":
            if (
                constraints.max_hotel_cost_per_person_per_night is not None
                and Decimal(str(activity.cost)) > constraints.max_hotel_cost_per_person_per_night * request.persons
            ):
                issues.append("parsed hotel per-person per-night cost cap exceeded")
            if constraints.rooms is not None and activity.rooms != constraints.rooms:
                issues.append("parsed hotel room count mismatch")
            if constraints.room_beds is not None and activity.room_type != constraints.room_beds:
                issues.append("parsed hotel bed count mismatch")
    return issues + constraints.unresolved + requirements.unresolved_requests
