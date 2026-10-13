"""Final persisted delivery metrics; every submitted case remains in the denominator."""

from app.agent.grounding.grounding_evidence import evidence_row
from app.schemas.trip import DailyPlan, TripItem
from app.services.day_delivery_gate import hard_delivery_blockers

_KEYS = {
    "item_type": "itemType",
    "poi_name": "poiName",
    "start_time": "startTime",
    "end_time": "endTime",
    "duration_min": "durationMin",
    "cost": "cost",
    "latitude": "latitude",
    "longitude": "longitude",
    "open_time": "openTime",
}


def assess_delivery(detail: dict, budget: dict | None, elapsed: float, usage: list[dict]) -> dict:
    days = detail.get("dayList") or []
    usable = 0
    hard_violations = 0
    evidence_overreach = 0
    for day in days:
        items = [
            TripItem(**{key: row.get(alias) for key, alias in _KEYS.items() if row.get(alias) is not None})
            for row in day.get("items") or []
        ]
        blockers = hard_delivery_blockers(int(day["dayNo"]), DailyPlan(day_no=day["dayNo"], items=items))
        if day.get("generationStatus") == "SUCCEEDED":
            hard_violations += len(blockers)
            usable += int(bool(items) and not blockers)
        for row in day.get("items") or []:
            for field, evidence in (row.get("factEvidence") or {}).items():
                if evidence.get("valueKind", evidence.get("value_kind")) != "observed":
                    continue
                reference = evidence.get("sourceRef", evidence.get("source_ref"))
                snapshot = evidence_row(
                    {"evidence_id": reference, "name": row.get("poiName"), "source": evidence.get("provider")}
                )
                allowed = snapshot is not None and field == "identity"
                if snapshot and field == "cost":
                    allowed = row.get("cost") in (snapshot.get("ticket_price"), snapshot.get("avg_cost"))
                if snapshot and field == "open_time":
                    allowed = row.get("openTime") == snapshot.get("open_time") and bool(snapshot.get("open_time"))
                if snapshot and field == "coordinates":
                    allowed = all(
                        row.get(key) == snapshot.get(key) and snapshot.get(key) is not None
                        for key in ("latitude", "longitude")
                    )
                if snapshot and field.startswith("diet:"):
                    allowed = field.removeprefix("diet:") in (snapshot.get("dietary_options") or [])
                if snapshot and field.startswith("category:"):
                    key, separator, value = field.removeprefix("category:").partition("=")
                    allowed = bool(separator) and (snapshot.get("category_tags") or {}).get(key) == value
                evidence_overreach += int(not allowed)
    wanted = int(detail.get("days") or 0)
    complete = wanted > 0 and usable == wanted and detail.get("genState") == "COMPLETED"
    respected = None
    if budget:
        respected = (
            budget["llm_calls"] <= budget["max_llm_calls"]
            and budget["prompt_tokens"] + budget["completion_tokens"] <= budget["max_tokens"]
            and budget["retrievals"] <= budget["max_retrievals"]
            and budget["research_calls"] <= budget["max_research_calls"]
            and elapsed <= budget["deadline_seconds"]
        )
    return {
        "generation_success": detail.get("status") == 2,
        "complete_delivery": complete,
        "usable_days": usable,
        "requested_days": wanted,
        "state": detail.get("genState", "UNKNOWN"),
        "terminal_resets": budget.get("terminal_resets", 0) if budget else None,
        "budget": budget,
        "budget_respected": respected,
        "elapsed_seconds": elapsed,
        "usage": usage,
        "usage_unknown": sum(not row.get("usage_complete") for row in usage),
        "hard_violations": hard_violations,
        "evidence_overreach": evidence_overreach,
        "duplicate_executions": None,
        "account_leaks": None,
    }


def summarize(samples: list[dict]) -> dict:
    count = len(samples)
    complete = sum(bool(row.get("complete_delivery")) for row in samples)
    return {
        "submitted": count,
        "generation_success": sum(bool(row.get("generation_success")) for row in samples),
        "complete_delivery": complete,
        "complete_delivery_rate": complete / count if count else None,
        "usable_days": sum(row.get("usable_days", 0) for row in samples),
        "terminal_resets": sum(row.get("terminal_resets") or 0 for row in samples),
        "failures_or_unknown": sum(row.get("state") in ("FAILED", "UNKNOWN", "NOT_RUN") for row in samples),
        "usage_unknown": sum(row.get("usage_unknown", 0) for row in samples),
    }


def release_allowed(report: dict) -> bool:
    rows = report.get("samples") or []
    # Evidence of every zero-tolerance property is required; absent/unknown never means zero.
    return (
        report.get("mode") == "live"
        and report.get("full_gates_passed") is True
        and len(rows) == 26
        and sum(row.get("complete_delivery") is True for row in rows) >= 21
        and all(
            row.get("budget_respected") is True
            and all(
                row.get(key) == 0
                for key in ("hard_violations", "evidence_overreach", "duplicate_executions", "account_leaks")
            )
            for row in rows
        )
    )
