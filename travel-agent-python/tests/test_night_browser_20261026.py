"""Explicit bounded formal browser acceptance; services are owned by this isolated experiment."""

import json
import os
import sqlite3
import subprocess
from pathlib import Path

import pytest


def test_current_live_browser():
    if not os.environ.get("SINAN_NIGHT26_LIVE_OUTPUT"):
        pytest.skip("explicit live browser output required")
    frontend = Path(__file__).resolve().parents[2] / "travel-frontend-react"
    subprocess.run(["node", "e2e/night-live-20261026.mjs"], cwd=frontend, timeout=72, check=True)


def test_current_output_replay_browser():
    if not os.environ.get("SINAN_NIGHT26_REPLAY_ROOT"):
        pytest.skip("explicit current-output browser replay required")
    frontend = Path(__file__).resolve().parents[2] / "travel-frontend-react"
    subprocess.run(["node", "e2e/night-replay-20261026.mjs"], cwd=frontend, timeout=57, check=True)


def test_three_current_journeys_independent_sql_readback():
    root = os.environ.get("SINAN_NIGHT26_DB_ROOT")
    if not root:
        pytest.skip("explicit completed journey databases required")
    from app.schemas.trip_requirements import TripRequirements
    from scripts.run_p4_acceptance import save

    for name in ("continuous-live-confirmed-source", "replay-conflict-fixed", "replay-screenshot-entity-bound"):
        directory = Path(root) / name
        observed = json.loads((directory / "frontend/browser.json").read_text("utf8"))
        assert observed["status"] == "passed"
        expected = observed.get("final") or observed["initial"]
        with sqlite3.connect(directory / "browser.sqlite3") as database:
            database.row_factory = sqlite3.Row
            main = dict(database.execute("select * from itinerary_main where id=?", (expected["id"],)).fetchone())
            rows = [
                dict(row)
                for row in database.execute(
                    "select * from itinerary_item where itinerary_id=? and deleted=0 order by id", (expected["id"],)
                )
            ]
            required = TripRequirements.model_validate(json.loads(main["requirements_json"]))
            api_required = TripRequirements.model_validate(expected["requirementsStruct"])
            assert required == api_required and main["planning_revision"] == expected["planningRevision"]
            api_rows = sorted((p for day in expected["dayList"] for p in day["items"]), key=lambda p: p["id"])
            assert len(rows) == len(api_rows)
            for stored, public in zip(rows, api_rows, strict=True):
                assert (stored["id"], stored["poi_name"]) == (public["id"], public["poiName"])
                assert stored["start_time"][:5] == public["startTime"][:5]
                assert stored["end_time"][:5] == public["endTime"][:5]
            for fixed in required.fixed_arrangements:
                stored = next(row for row in rows if row["poi_name"] == fixed.name)
                assert (stored["start_time"][:5], stored["end_time"][:5]) == (fixed.start_time, fixed.end_time)
            if name == "replay-conflict-fixed":
                old = dict(
                    database.execute(
                        "select * from itinerary_main where id=?", (observed["original"]["id"],)
                    ).fetchone()
                )
                old_req = TripRequirements.model_validate(json.loads(old["requirements_json"]))
                assert old_req == TripRequirements.model_validate(observed["original"]["requirementsStruct"])
                assert all(relation.strength == "hard" for relation in old_req.place_relations)
        save(
            directory / "independent-database.json",
            {
                "method": "direct sqlite3 reads, no business getter or CP compiler",
                "item_count": len(rows),
                "requirements_equal": True,
                "revision_equal": True,
                "fixed_times_equal": True,
                "items": [
                    {key: row[key] for key in ("id", "poi_name", "start_time", "end_time", "cost", "cost_currency")}
                    for row in rows
                ],
                "human_review": "not_performed",
            },
        )
