"""Local evidence export for live_perf_probe: checkpoints + final DB detail, no model calls.

The baseline is the first whole-trip checkpoint AFTER deterministic landing, BEFORE
business per-day regeneration. It is not the raw model output. Missing checkpoints
remain missing, including on failed runs. Reports contain full snapshots for recomputation.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import subprocess
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path
from statistics import median

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
VERSION = "repair-preview-v1"


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()


def identity() -> dict:
    from app.common.config import settings

    repo = ROOT.parent
    paths = [
        *ROOT.joinpath("app").rglob("*.py"),
        *ROOT.joinpath("app").rglob("*.sql"),
        *repo.joinpath("travel-frontend-react/src").rglob("*.ts*"),
    ]
    paths += [ROOT / "uv.lock", repo / "travel-frontend-react/package-lock.json"]
    source = {p.relative_to(repo).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(paths)}
    keys = (
        "llm_model",
        "llm_role_main",
        "llm_role_fast",
        "llm_role_search",
        "llm_generation_web_search",
        "budget_hard_constraint",
        "budget_overage_ratio",
        "route_service_enabled",
        "route_mode",
        "schedule_optimizer_enabled",
        "weather_enabled",
        "nominatim_enabled",
        "opentripmap_enabled",
    )
    config = {k: getattr(settings, k, None) for k in keys}
    measurement = {
        p.name: hashlib.sha256(p.read_bytes()).hexdigest()
        for p in (
            Path(__file__),
            repo / "travel-frontend-react/e2e/preview-timing.mjs",
            repo / "travel-frontend-react/e2e/preview-evidence.mjs",
        )
        if p.exists()
    }
    git = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True)
    return {
        "version": VERSION,
        "commit": git.stdout.strip(),
        "source_sha256": digest(source),
        "config": config,
        "measurement_sha256": digest(measurement),
        "config_sha256": digest(settings.model_dump(mode="json")),
        "runtime_scope": "local source/config; running server must use this checkout/config",
    }


def checkpoint_evidence(db_path: Path, itinerary_id: int, started_at: str, ended_at: str | None = None) -> dict:
    import ormsgpack
    from langgraph.checkpoint.serde.jsonplus import _msgpack_ext_hook_to_json

    stream, attempts = [], []
    # mode=ro prevents accidental creation/cleanup of the application's checkpoint DB.
    with sqlite3.connect(db_path.resolve().as_uri() + "?mode=ro", uri=True) as conn:
        for thread, cid, kind, blob in conn.execute(
            "SELECT thread_id, checkpoint_id, type, checkpoint FROM checkpoints "
            "WHERE thread_id LIKE 'stream-%' OR thread_id LIKE ? ORDER BY checkpoint_id",
            (f"day-{itinerary_id}-%",),
        ):
            if kind != "msgpack":
                raise ValueError(f"Unsupported checkpoint encoding: {kind}")
            # Existing serializer's JSON projection: no schema imports, graph compilation,
            # constructor execution, or writes to the live checkpoint database.
            state = ormsgpack.unpackb(blob, ext_hook=_msgpack_ext_hook_to_json, option=ormsgpack.OPT_NON_STR_KEYS)
            if datetime.fromisoformat(state["ts"]) < datetime.fromisoformat(started_at):
                continue
            if ended_at and datetime.fromisoformat(state["ts"]) > datetime.fromisoformat(ended_at):
                continue
            cv = state.get("channel_values", {})
            req = cv.get("day_request") or {}
            if req.get("request_id") != f"itinerary-{itinerary_id}":
                continue
            origin = {"thread_id": thread, "checkpoint_id": cid, "ts": state.get("ts")}
            if thread.startswith("stream-") and cv.get("daily_plans"):
                stream.append({**origin, "plans": cv["daily_plans"], "request": req})
            if thread.startswith("day-") and cv.get("plan"):
                attempts.append(
                    {
                        **origin,
                        "day_no": req["day_no"],
                        "attempt": cv.get("attempts"),
                        "plan": cv["plan"],
                        "issues": cv.get("validation_issues", []),
                    }
                )
    # Do not silently substitute a later retry for a missing whole-trip baseline.
    return {
        "baseline_definition": "first whole-trip checkpoint after deterministic landing; before business repair",
        "initial": stream[0] if stream else None,
        "day_checkpoints": attempts,
        "missing": [] if stream else ["first_whole_trip_checkpoint"],
    }


ITEM_FIELDS = (
    "item_type",
    "poi_name",
    "poi_id",
    "start_time",
    "end_time",
    "duration_min",
    "cost",
    "latitude",
    "longitude",
    "open_time",
)


def plans_from_detail(detail: dict) -> list[dict]:
    def field(row, key):
        parts = key.split("_")
        return row.get(key, row.get(parts[0] + "".join(p.title() for p in parts[1:])))

    return [
        {
            "day_no": int(day.get("dayNo", day.get("day_no", 0))),
            "items": [{k: field(item, k) for k in ITEM_FIELDS} for item in day.get("items", [])],
        }
        for day in detail.get("dayList", [])
    ]


def checks_for(plans: list[dict], payload: dict, consumption: dict | None = None) -> dict:
    from app.agent.generation.content.reflect import validate_plans
    from app.agent.generation.rules.constraint_checks import check_constraints
    from app.agent.generation.rules.day_policy import day_policy_for
    from app.agent.generation.rules.generation_core import estimate_plans_total
    from app.schemas.trip_requirements import TripRequirements

    req = TripRequirements.model_validate(payload.get("requirementsStruct") or {})
    by_day = {int(p["day_no"]): p for p in plans}
    checks = {}
    rule_issues = {}
    for day_no in range(1, int(payload["days"]) + 1):
        plan = by_day.get(day_no, {"day_no": day_no, "items": []})
        report = check_constraints(plan, req, day_no, day_policy_for(req, day_no))
        for check in report.checks:
            status = check.status
            if (
                check.kind in ("time_conflict", "day_window")
                and check.status != "not_applicable"
                and (
                    not plan["items"] or any(not it.get("start_time") or not it.get("end_time") for it in plan["items"])
                )
            ):
                status = "unknown"
            checks[f"day:{day_no}:{check.constraint_id}"] = {
                **check.model_dump(mode="json"),
                "status": status,
            }
        checks[f"delivery:{day_no}"] = {"status": "pass" if plan["items"] else "violation", "day_no": day_no}
        issues, _ = validate_plans([plan], persons=payload.get("persons", 1), requirements=req)
        rule_issues[str(day_no)] = issues
        checks[f"plan-rules:{day_no}"] = {
            "kind": "daily_plan_rules",
            "day_no": day_no,
            "status": "violation" if issues else "pass",
        }
    names = {"".join(str(i.get("poi_name", "")).split()).casefold() for p in plans for i in p["items"]}
    for place in req.required_places:
        if place.day_no is None:
            checks[f"trip:required:{place.constraint_id}"] = {
                "kind": "required_place",
                "day_no": None,
                "status": "pass" if "".join(place.name.split()).casefold() in names else "violation",
            }
    if req.budget_policy and req.budget_policy.mode == "hard_cap":
        estimate = estimate_plans_total(
            plans,
            payload.get("persons", 1),
            int(payload["days"]),
            consumption,
            rooms=req.lodging.rooms if req.lodging else None,
        )
        limit = payload.get("budget")
        checks["trip:budget"] = {
            "day_no": None,
            "estimate": estimate,
            "status": (
                "unknown"
                if limit is None or any(it.get("cost") is None for p in plans for it in p["items"])
                else "violation"
                if float(estimate.get("合计", 0)) > limit
                else "pass"
            ),
        }
    return {"checks": checks, "counts": dict(Counter(c["status"] for c in checks.values())), "rule_issues": rule_issues}


def _content(plan: dict) -> list[dict]:
    rows = []
    for item in plan.get("items", []):
        row = {k: item.get(k) for k in ITEM_FIELDS}
        for key in ("cost", "latitude", "longitude", "duration_min"):
            if row[key] is not None:
                row[key] = float(row[key])
        for key in ("start_time", "end_time"):
            if row[key] is not None:
                value = str(row[key])
                row[key] = value[:-3] if len(value) == 8 and value.endswith(":00") else value
        rows.append(row)
    return rows


def compare(initial: list[dict], final: list[dict], payload: dict, retry_days: list[int], consumption=None) -> dict:
    before, after = checks_for(initial, payload, consumption), checks_for(final, payload, consumption)
    b, a = before["checks"], after["checks"]
    violations = {k for k, c in b.items() if c["status"] == "violation"}
    final_violations = {k for k, c in a.items() if c["status"] == "violation"}
    resolved = sorted(k for k in violations if a.get(k, {}).get("status") == "pass")
    # Scope is the days actually regenerated, not every day that had a warning.
    affected = set(retry_days)
    old, new = {int(p["day_no"]): p for p in initial}, {int(p["day_no"]): p for p in final}
    changed, untouched = [], []
    for day in range(1, int(payload["days"]) + 1):
        same = _content(old.get(day, {})) == _content(new.get(day, {}))
        if not same:
            changed.append(day)
        if day not in affected:
            untouched.append({"day_no": day, "preserved": same})
    required = [k for k, c in b.items() if c.get("kind") == "required_place" and c["status"] == "pass"]
    return {
        "before": before,
        "after": after,
        "initial_violations": sorted(violations),
        "resolved": resolved,
        "new_violations": sorted(final_violations - violations),
        "unresolved": sorted(violations - set(resolved)),
        "new_rule_issues": {
            day: sorted(set(issues) - set(before["rule_issues"].get(day, [])))
            for day, issues in after["rule_issues"].items()
        },
        "violation_unit": "one typed constraint check, plus one conjunction of validate_plans rules per day",
        "elimination_rate": len(resolved) / len(violations) if violations else None,
        "required_retention": {"eligible": len(required), "retained": sum(a[k]["status"] == "pass" for k in required)},
        "changed_days": changed,
        "repair_days": sorted(set(retry_days)),
        "initial_violation_days": sorted({b[k]["day_no"] for k in violations if b[k].get("day_no") is not None}),
        "uninvolved_days": untouched,
    }


def analyze(raw: dict, db_path: Path | None = None) -> dict:
    result = {**raw, "analysis_version": VERSION}
    rows = []
    for run in raw["runs"]:
        row = {**run}
        missing = list(run.get("errors", []))
        cp = run.get("checkpoints")
        if cp is None and db_path is not None and run.get("itinerary_id"):
            try:
                cp = checkpoint_evidence(db_path, run["itinerary_id"], run["started_at"], run.get("finished_at"))
            except (sqlite3.Error, ValueError, OSError) as exc:
                missing.append(f"checkpoint_export:{type(exc).__name__}:{exc}")
        row["checkpoints"] = cp
        final = run.get("final_detail")
        first = (cp or {}).get("initial")
        if not first:
            missing.append("first_whole_trip_checkpoint")
        if not final:
            missing.append("final_db_detail")
        row["comparison"] = None
        if first and final and cp is not None:
            retry_days = sorted({p["day_no"] for p in cp["day_checkpoints"]})
            row["comparison"] = compare(
                first["plans"],
                plans_from_detail(final),
                run["payload"],
                retry_days,
                first.get("request", {}).get("context", {}).get("consumption"),
            )
        timing = run.get("browser", {})
        preview, core = timing.get("first_candidate_ms"), timing.get("core_visible_ms")
        eligible = int(run["payload"]["days"]) > 1
        row["preview_lead_ms"] = None
        if eligible and preview is not None and core is not None and core >= preview:
            row["preview_lead_ms"] = round(core - preview, 3)
        if eligible and row["preview_lead_ms"] is None:
            missing.append("browser_preview_core_pair")
        row["terminal_state"] = (final or {}).get("genState", "missing")
        if row["terminal_state"] not in ("COMPLETED", "PARTIAL", "FAILED"):
            missing.append("terminal_state_unconfirmed")
        row["collection_gaps"] = sorted(set(missing))
        rows.append(row)
    result["runs"] = rows
    result["by_scenario"] = {
        key: {
            "attempts": len(group),
            "repeat_ids": [r.get("repeat_id") for r in group],
            "terminal_states": dict(Counter(r["terminal_state"] for r in group)),
            "preview_pairs_ms": [r["preview_lead_ms"] for r in group if r["preview_lead_ms"] is not None],
        }
        for key in sorted({r["scenario_id"] for r in rows})
        for group in [[r for r in rows if r["scenario_id"] == key]]
    }
    leads = [r["preview_lead_ms"] for r in rows if r["preview_lead_ms"] is not None]
    result["summary"] = {
        "scenarios": len({r["scenario_id"] for r in rows}),
        "attempts": len(rows),
        "terminal_states": dict(Counter(r["terminal_state"] for r in rows)),
        "comparable_pairs": sum(r["comparison"] is not None for r in rows),
        "initially_violating_pairs": sum(bool((r["comparison"] or {}).get("initial_violations")) for r in rows),
        "new_violations": sum(len((r["comparison"] or {}).get("new_violations", [])) for r in rows),
        "uninvolved_day_changes": sum(
            not d["preserved"] for r in rows for d in (r["comparison"] or {}).get("uninvolved_days", [])
        ),
        "preview_eligible_attempts": sum(int(r["payload"]["days"]) > 1 for r in rows),
        "browser_pairs": len(leads),
        "median_preview_lead_ms": median(leads) if leads else None,
        "attempts_with_gaps": sum(bool(r["collection_gaps"]) for r in rows),
    }
    current = identity()
    result["analysis_identity"] = current
    result["same_source_config"] = all(
        raw.get("identity", {}).get(k) == current.get(k)
        for k in ("source_sha256", "config_sha256", "config", "measurement_sha256")
    )
    result["evidence_scope"] = (
        "real-model sample" if raw.get("mode") == "live-business-browser" else "collection replay only"
    )
    result["interpretation"] = (
        "Offline replay validates collection only. Live failures remain in all-attempt denominators."
    )
    return result


if __name__ == "__main__":
    print(json.dumps(identity(), ensure_ascii=False))
