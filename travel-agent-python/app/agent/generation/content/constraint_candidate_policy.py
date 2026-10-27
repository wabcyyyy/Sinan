"""Fixed weekly resource bounds and day rotation; never selected using evaluation Gold."""

import math

TRIP_CANDIDATE_LIMIT = 40
DAY_CANDIDATE_LIMIT = 20
TRIP_PLANNING_SECONDS = 30
TIMETABLE_SOLVE_SECONDS = 6
COMPLETE_OUTPUT_TOKENS = 8192
COMPLETE_CATALOG_LIMITS = {"candidates": 40, "foods": 32, "hotels": 8}


def lodging_rooms(requirements, persons):
    if requirements.travel_constraints and requirements.travel_constraints.rooms is not None:
        return requirements.travel_constraints.rooms
    if requirements.lodging and requirements.lodging.rooms is not None:
        return requirements.lodging.rooms
    return math.ceil(persons / 2)


def hotel_matches(row, identity):
    return identity == row.get("name") or (row.get("id") is not None and identity == str(row["id"]))


def requested_names(requirements, day_no=None):
    names = {p.name for p in requirements.required_places if day_no is None or p.day_no in (None, day_no)}
    names.update(f.name for f in requirements.fixed_arrangements if day_no is None or f.day_no == day_no)
    for relation in requirements.place_relations:
        if day_no is None or relation.day_no == day_no:
            names.update(relation.place_names)
    if requirements.travel_constraints:
        names.update(requirements.travel_constraints.required_names)
    if requirements.lodging and requirements.lodging.locked_hotel_identity:
        names.add(requirements.lodging.locked_hotel_identity)
    return names


def day_pool(rows, day_no, size, mandatory=(), *, name=lambda row: row["name"], stride=4):
    """Explicit requested entities survive pruning; remainder rotates without forcing a date assignment."""
    forced = [row for row in rows if name(row) in mandatory]
    if len(forced) > size:
        raise ValueError("explicit requested entities exceed this bounded daily candidate pool; not proven infeasible")
    if not rows:
        return forced
    offset = ((day_no - 1) * stride) % len(rows)
    rotation = rows[offset:] + rows[:offset]
    return (forced + [row for row in rotation if row not in forced])[:size]
