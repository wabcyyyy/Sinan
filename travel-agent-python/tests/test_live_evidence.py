"""Collection correctness: no LLM/network; deliberate failures stay in the report."""

import json
import sqlite3
from copy import deepcopy

import httpx
import pytest
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

from app.schemas.trip import GenerateDayRequest
from scripts import live_evidence as evidence
from scripts import live_perf_probe as probe


def item(name, start="10:00", end="11:00"):
    return {
        "item_type": "attraction",
        "poi_name": name,
        "start_time": start,
        "end_time": end,
        "duration_min": 60,
        "cost": 20,
    }


def plans():
    return [
        {"day_no": 1, "items": [item("必去点"), item("茶馆", "12:00", "13:00")]},
        {"day_no": 2, "items": [item("公园"), item("江边", "12:00", "13:00")]},
    ]


@pytest.fixture(autouse=True)
def no_model(monkeypatch):
    from app.common.llm_client import LLMClient

    def forbidden(*args, **kwargs):
        pytest.fail("Evidence export must not call a model")

    monkeypatch.setattr(LLMClient, "chat", forbidden)
    monkeypatch.setattr(LLMClient, "stream_chat_deltas", forbidden)


def test_resolution_single_bound_regression_and_uninvolved_days():
    before = plans()
    before[1]["items"][1]["start_time"] = "10:30"
    after = plans()
    after[0]["items"][0]["poi_name"] = "误删必去"
    payload = {
        "days": 2,
        "requirementsStruct": {
            "requiredPlaces": [{"constraintId": "anchor", "name": "必去点", "dayNo": 1}],
            "dayWindows": [{"dayNo": 1, "notBefore": "09:00"}],
        },
    }
    result = evidence.compare(before, after, payload, [2])
    assert "day:2:time:day:2" in result["resolved"]
    assert "day:1:required:anchor" in result["new_violations"]
    assert result["required_retention"] == {"eligible": 1, "retained": 0}
    assert result["before"]["checks"]["day:1:window:day:1"]["status"] == "pass"
    assert result["before"]["counts"].get("unknown", 0) == 0
    assert result["uninvolved_days"] == [{"day_no": 1, "preserved": False}]


def test_numeric_time_normalization_and_global_required():
    before, after = plans(), plans()
    after[0]["items"][0].update(cost="20.00", start_time="10:00:00", end_time="11:00:00")
    result = evidence.compare(
        before,
        after,
        {
            "days": 2,
            "requirementsStruct": {
                "requiredPlaces": [{"constraintId": "any-day", "name": "必去点"}],
            },
        },
        [],
    )
    assert result["changed_days"] == []
    assert result["required_retention"] == {"eligible": 1, "retained": 1}
    assert result["elimination_rate"] is not None  # existing per-day density issues remain visible
    assert result["elimination_rate"] == 0


def test_missing_time_is_unknown_not_pass():
    draft = plans()
    draft[0]["items"][0]["start_time"] = None
    result = evidence.checks_for(
        draft, {"days": 2, "requirementsStruct": {"dayWindows": [{"dayNo": 1, "notBefore": "09:00"}]}}
    )
    assert result["checks"]["day:1:time:day:1"]["status"] == "unknown"
    assert result["checks"]["day:1:window:day:1"]["status"] == "unknown"


def test_missing_cost_does_not_prove_hard_cap():
    draft = plans()
    draft[0]["items"][0]["cost"] = None
    result = evidence.checks_for(
        draft, {"days": 2, "budget": 3000, "requirementsStruct": {"budgetPolicy": {"mode": "hard_cap"}}}
    )
    assert result["checks"]["trip:budget"]["status"] == "unknown"


def test_checkpoint_exact_request_time_and_first_snapshot(tmp_path):
    db = tmp_path / "checkpoints.sqlite3"
    serializer = JsonPlusSerializer()
    req = GenerateDayRequest(city="杭州", day_no=1, days=2, request_id="itinerary-701")
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE checkpoints (thread_id TEXT, checkpoint_id TEXT, type TEXT, checkpoint BLOB)")
        for cid, stamp, request in [
            ("01", "2026-10-10T00:00:00+00:00", req),
            ("02", "2026-10-16T00:00:01+00:00", req),
            ("03", "2026-10-16T00:00:02+00:00", req.model_copy(update={"request_id": "itinerary-702"})),
        ]:
            kind, blob = serializer.dumps_typed(
                {"ts": stamp, "channel_values": {"day_request": request, "daily_plans": plans()}}
            )
            conn.execute("INSERT INTO checkpoints VALUES (?, ?, ?, ?)", ("stream-test", cid, kind, blob))
        kind, blob = serializer.dumps_typed(
            {
                "ts": "2026-10-17T00:00:00+00:00",
                "channel_values": {
                    "day_request": req.model_copy(update={"day_no": 2}),
                    "plan": plans()[1],
                    "attempts": 1,
                },
            }
        )
        conn.execute("INSERT INTO checkpoints VALUES (?, ?, ?, ?)", ("day-701-2", "04", kind, blob))
    result = evidence.checkpoint_evidence(db, 701, "2026-10-16T00:00:00+00:00")
    assert result["initial"]["checkpoint_id"] == "02"
    assert result["initial"]["plans"] == plans()
    assert result["missing"] == []
    with pytest.raises(sqlite3.OperationalError):
        evidence.checkpoint_evidence(tmp_path / "absent.db", 701, "")
    assert not (tmp_path / "absent.db").exists()
    original = db.read_bytes()
    assert evidence.checkpoint_evidence(db, 701, "2026-10-16T00:00:00Z")["initial"]["checkpoint_id"] == "02"
    assert db.read_bytes() == original
    bounded = evidence.checkpoint_evidence(db, 701, "2026-10-16T00:00:00Z", "2026-10-16T01:00:00Z")
    assert bounded["day_checkpoints"] == []
    assert len(evidence.checkpoint_evidence(db, 701, "2026-10-16T00:00:00Z")["day_checkpoints"]) == 1


def test_all_attempts_retained_and_browser_pairs_not_imputed(monkeypatch):
    ident = {"source_sha256": "fixed", "config": {}, "measurement_sha256": "fixed"}
    monkeypatch.setattr(evidence, "identity", lambda: ident)
    run = {
        "scenario_id": "same",
        "payload": {"days": 2},
        "errors": [],
        "browser": {"first_candidate_ms": 100, "core_visible_ms": 500},
        "final_detail": {"genState": "COMPLETED", "dayList": []},
    }
    failed = deepcopy(run)
    failed["browser"]["core_visible_ms"] = None
    failed["final_detail"]["genState"] = "FAILED"
    raw = {"mode": "offline-replay-business-browser", "identity": ident, "runs": [run, failed]}
    result = evidence.analyze(raw)
    assert result["summary"]["scenarios"] == 1
    assert result["summary"]["attempts"] == 2
    assert result["summary"]["browser_pairs"] == 1
    assert result["summary"]["terminal_states"] == {"COMPLETED": 1, "FAILED": 1}
    assert result["summary"]["comparable_pairs"] == 0
    assert result["runs"][1]["preview_lead_ms"] is None
    assert result["evidence_scope"] == "collection replay only"
    assert all("first_whole_trip_checkpoint" in r["collection_gaps"] for r in result["runs"])
    assert json.loads(json.dumps(result))["runs"] == result["runs"]


def test_probe_event_time_includes_submit_and_keeps_disconnect(monkeypatch):
    monkeypatch.setattr(probe.time, "perf_counter", lambda: 12.0)
    event = {"type": "core_ready", "data": {}}
    client = httpx.Client(
        base_url="http://test",
        transport=httpx.MockTransport(lambda request: httpx.Response(200, text=f"data: {json.dumps(event)}\n\n")),
    )
    frames = probe._sse_frames(client, "unused", 701, 100, 10)
    assert frames["seen"]["core_ready"] == 2.0
    assert frames["frames"][0]["frame"] == event
    client.close()


def test_offline_input_cannot_fall_through_to_live_calls(tmp_path, monkeypatch):
    source = tmp_path / "browser.json"
    source.write_text('{"mode":"offline-replay-business-browser","runs":[]}', encoding="utf-8")
    monkeypatch.setattr(probe.sys, "argv", ["probe", "--evidence-input", str(source)])
    monkeypatch.setattr(evidence, "analyze", lambda raw, db: {"summary": {"attempts": 0}})

    def forbidden(*args, **kwargs):
        pytest.fail("offline analysis reached live code")

    monkeypatch.setattr(probe, "run_perf", forbidden)
    monkeypatch.setattr(probe, "judge_clarify_samples", forbidden)
    assert probe.main() == 0
    assert source.with_name("browser.analyzed.json").exists()
