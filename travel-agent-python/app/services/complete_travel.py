"""Persist the complete shared plan, not a runner-only answer; relative days retain null calendar dates."""

import json

from sqlalchemy import select, update

from app.agent import check_task_write, plan_complete_travel
from app.db.models import ItineraryDay, ItineraryItem
from app.db.session import session_scope
from app.schemas.trip import GenerateDayRequest
from app.services import day_persistence, generation_gate, itinerary_version


def plan_trip(user_id, itinerary_id, command, context, fingerprint):
    request = GenerateDayRequest(
        city=command.city,
        days=command.days,
        persons=command.persons,
        budget=None if command.budget is None else float(command.budget),
        start_date=None,
        day_no=1,
        intent=command.resolved_intent(),
        requirements=command.requirements,
        requirements_struct=command.requirements_struct,
        context={**context, "requested_stay_nights": command.stay_nights},
        needs_hotel=command.stay_nights > 0,
    )
    plans, issues = plan_complete_travel(request)
    # This branch has its own source-accurate whole-plan gate. No generic product threshold is weakened.
    persist_plans(itinerary_id, plans, issues, fingerprint)
    return issues


def persist_plans(itinerary_id, plans, issues, fingerprint):
    check_task_write()
    numbers = [plan.day_no for plan in plans]
    if len(numbers) != len(set(numbers)):
        raise ValueError("duplicate day in complete business output")
    with session_scope() as session:
        expected = set(
            session.scalars(
                select(ItineraryDay.day_no).where(ItineraryDay.itinerary_id == itinerary_id, ItineraryDay.deleted == 0)
            )
        )
        if not set(numbers) <= expected or (not issues and set(numbers) != expected):
            raise ValueError("complete plan day coverage differs from the persisted itinerary")
        for plan in plans:
            day = session.scalar(
                select(ItineraryDay).where(
                    ItineraryDay.itinerary_id == itinerary_id, ItineraryDay.day_no == plan.day_no
                )
            )
            if day is None:
                raise ValueError("model emitted a day outside the existing itinerary")
            generation_gate.verify_action(day, f"day-{itinerary_id}-{plan.day_no}", fingerprint)
            session.execute(
                update(ItineraryItem)
                .where(ItineraryItem.day_id == day.id, ItineraryItem.deleted == 0)
                .values(deleted=1)
            )
            for i, item in enumerate(plan.items):
                session.add(day_persistence._build_item(itinerary_id, day.id, item, i))
            day.metadata_json = json.dumps(
                {
                    "travelActivities": [
                        a.model_dump(mode="json", by_alias=True) for a in plan.travel_activities or []
                    ],
                    "travelHeader": plan.travel_header.model_dump(mode="json", by_alias=True),
                    "travelValidationIssues": issues,
                    "factSource": "chinatravel",
                    "relativeDays": True,
                },
                ensure_ascii=False,
            )
            day.travel_date = None
            day.generation_status = "PENDING" if issues else "SUCCEEDED"
            day.generation_error = "；".join(issues)[:500] or None
            day.generation_fingerprint = fingerprint
        itinerary_version.bump_planning_revision(session, itinerary_id)
        check_task_write()
