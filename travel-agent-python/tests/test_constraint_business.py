"""真实 plan_days/SQLite/API 读回；研究与外部来源冻结，不是线上模型 E2E。"""

import json
import math
import os
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from app.agent.data import osrm_routes, route_service
from app.common import http_client
from app.common.config import settings
from app.db.models import ItineraryDay, ItineraryItem, ItineraryMain
from app.db.session import session_scope
from app.schemas.business.itinerary import GenerateTripRequest
from app.services import budget_engine, itinerary_generation
from tests.constraint_planning_cases import frozen_context, planning_request

pytest_plugins = ("tests.test_m1b_requirements_pipeline",)


def test_global_city_names_keep_original_scripts():
    for city in ("京都", "Paris", "กรุงเทพมหานคร", "São Paulo"):
        assert itinerary_generation._validate(GenerateTripRequest(city=city, days=1)).city == city


def test_research_preview_precedes_solve_and_never_claims_a_day_or_schedule(client, monkeypatch):
    from app.agent.generation.orchestration import constraint_planning as planner
    from app.services import generation_events

    events = []
    monkeypatch.setattr(generation_events, "item_preview", lambda *args: events.append(("preview", args)))
    monkeypatch.setattr(generation_events, "item_preview_withdrawn", lambda *args: events.append(("withdraw", args)))

    def mapped(*args, **kwargs):
        # 事件必须晚于真实提交；映射按候选ID匹配，快照中的正式身份必须存在。
        snapshot = kwargs["snapshot"]
        items = {p["id"] for d in snapshot["dayList"] for p in d["items"]}
        assert all(m["itemId"] in items for m in args[3])
        events.append(("mapping", (args, kwargs)))

    monkeypatch.setattr(generation_events, "day_preview_mapping", mapped)
    original = planner._problem_snapshots

    def snapshots(*args):
        assert len(events) == 6 and all(kind == "preview" for kind, _ in events)
        return original(*args)

    monkeypatch.setattr(planner, "_problem_snapshots", snapshots)
    identifier, _ = _submit(client, monkeypatch)
    assert [kind for kind, _ in events] == ["preview"] * 6 + ["mapping"] * 2
    for _, args in events[:6]:
        assert args[0] == identifier and args[2] == 0
        assert set(args[4]) == {"poi_name", "item_type", "candidate_id"}
    mappings = [m for kind, (args, kwargs) in events[6:] for m in args[3]]
    assert len(mappings) == 6 and len({m["candidateId"] for m in mappings}) == 6


def test_failed_constraint_solve_withdraws_unassigned_candidates(monkeypatch):
    from types import SimpleNamespace

    from app.schemas.trip import TripItem
    from app.services import constraint_planning as service
    from app.services import generation_events

    withdrawn = []
    monkeypatch.setattr(service, "_retained_days", lambda _: [])
    monkeypatch.setattr(generation_events, "item_preview", lambda *_: None)
    monkeypatch.setattr(generation_events, "item_preview_withdrawn", lambda *args: withdrawn.append(args))

    def fail(*_args, on_candidates, **_kwargs):
        on_candidates([TripItem(poi_name="未求解馆", item_type="attraction")])
        raise RuntimeError("solver failed")

    monkeypatch.setattr(service, "run_constraint_planning", fail)
    command = SimpleNamespace(to_generate_request=lambda: None, stay_nights=0)
    with pytest.raises(RuntimeError, match="solver failed"):
        service.plan_trip(1, 2, command, {}, "test", SimpleNamespace(mark=lambda _: None))
    assert len(withdrawn) == 1 and withdrawn[0][0] == 2 and withdrawn[0][2:4] == (0, 1)


def test_cp_mapping_distinguishes_same_name_and_settles_failed_persistence(client, monkeypatch):
    from app.services import constraint_planning as service
    from app.services import generation_events

    frames = []
    monkeypatch.setattr(generation_events, "day_preview_mapping", lambda *a, **kw: frames.append((a, kw)))
    context = frozen_context()
    context["candidates"][1]["name"] = context["candidates"][2]["name"] = "同名馆区"
    req = planning_request().requirements_struct
    assert req is not None
    req.place_relations = []
    identifier, _ = _submit(
        client,
        monkeypatch,
        context_payload=context,
        body_override={"requirementsStruct": req.model_dump(mode="json", by_alias=True)},
    )
    mappings = [m for args, _ in frames for m in args[3] if m["poiName"] == "同名馆区"]
    assert len(mappings) == 2 and len({m["itemId"] for m in mappings}) == 2
    assert len({m["candidateId"] for m in mappings}) == 2
    assert frames[-1][1]["snapshot"]["id"] == identifier
    frames.clear()

    def fail(*args, **kwargs):
        raise RuntimeError("persistence failed")

    monkeypatch.setattr(service.day_persistence, "persist", fail)
    _submit(client, monkeypatch)
    assert frames and not frames[-1][0][3]
    assert len(frames[-1][1]["withdrawn"]) == 6
    assert all(not d["items"] for d in frames[-1][1]["snapshot"]["dayList"])


def test_curly_apostrophe_required_place_survives_solver_and_business_delivery(client, monkeypatch):
    # 冻结同一个已观察到的实体条件，不要求联网产物存在于CI。
    pool = [
        {"name": "Musée du Louvre", "id": "louvre", "country_code": "FR", "duration_min": 90},
        {"name": "Musée d'Orsay", "id": "orsay", "country_code": "FR", "duration_min": 90},
    ]
    req = planning_request(days=1).requirements_struct
    assert req is not None
    req.required_places = []
    raw = req.model_dump(mode="json", by_alias=True)
    raw.update(
        fixedArrangements=[],
        placeRelations=[],
        requiredPlaces=[
            {"constraintId": "louvre", "name": "Musée du Louvre"},
            {"constraintId": "orsay", "name": "Musée d’Orsay"},
        ],
        dayWindows=[{"dayNo": 1, "kind": "arrival", "notBefore": "14:00", "finishBy": "18:00"}],
    )
    identifier, _ = _submit(
        client,
        monkeypatch,
        context_payload={"candidates": pool},
        body_override={"days": 1, "persons": 1, "requirementsStruct": raw},
    )
    detail = client.get(f"/api/itinerary/{identifier}").json()["data"]
    assert detail["genState"] == "COMPLETED"
    assert len(detail["dayList"][0]["items"]) == 2
    assert detail["dayList"][0]["constraintPlanning"]["status"] == "UNKNOWN"
    assert detail["requirementsStruct"]["requiredPlaces"][1]["name"] == "Musée d’Orsay"


def _submit(client, monkeypatch, *, budget=200, context_payload=None, body_override=None):
    captured = []

    def context(request, **kwargs):
        captured.append(request)
        return frozen_context() if context_payload is None else context_payload

    def forbidden(*args, **kwargs):
        raise AssertionError("约束分支不得进入旧 LLM 排程或重试")

    monkeypatch.setattr(itinerary_generation, "run_plan_context", context)
    monkeypatch.setattr(itinerary_generation, "run_generate_trip_stream", forbidden)
    monkeypatch.setattr(itinerary_generation, "run_generate_day", forbidden)
    monkeypatch.setattr(itinerary_generation.generation_pool, "submit", lambda task, *args: task(*args))
    monkeypatch.setattr(itinerary_generation.enricher_pool, "submit", lambda task, *args: task(*args))
    monkeypatch.setattr(itinerary_generation, "_submit_budget_recalculate", lambda itinerary_id: None)
    confirmed = planning_request().requirements_struct
    assert confirmed is not None
    body = {
        "city": "Paris",
        "days": 2,
        "persons": 2,
        "stayNights": 0,
        "startDate": "2026-10-25",
        "budget": budget,
        "requirementsStruct": confirmed.model_dump(mode="json", by_alias=True),
    }
    body.update(body_override or {})
    response = client.post("/api/itinerary/generate", json=body)
    assert response.status_code == 200, response.text
    identifier = response.json()["data"]["id"]
    return identifier, captured


def test_api_worker_solver_database_refresh_and_currency(client, monkeypatch):
    identifier, captured = _submit(client, monkeypatch)
    assert len(captured) == 1 and captured[0].requirements_struct.constraint_planning.currency == "EUR"
    response = client.get(f"/api/itinerary/{identifier}")
    detail = response.json()["data"]
    assert detail["budgetCurrency"] == "EUR"
    assert all(day["constraintPlanning"]["numericalStatus"] == "OPTIMAL" for day in detail["dayList"])
    assert all(day["constraintPlanning"]["isCurrent"] for day in detail["dayList"])
    assert [day["constraintPlanning"]["localDate"] for day in detail["dayList"]] == ["2026-10-25", "2026-10-26"]
    assert all(day["constraintPlanning"]["status"] == "UNKNOWN" for day in detail["dayList"])
    from app.common import generation_event_log

    frames, complete, _ = generation_event_log.read_after(identifier, 0)
    mappings = [json.loads(frame) for frame in frames if json.loads(frame)["type"] == "day_preview_mapping"]
    assert complete and len(mappings) == 2
    assert sum(len(frame["data"]["mappings"]) for frame in mappings) == 6
    assert all(frame["data"]["snapshot"]["id"] == identifier for frame in mappings)
    assert all(set(frame["data"]["snapshot"]) == {"id", "planningRevision", "dayList"} for frame in mappings)
    with session_scope() as session:
        rows = (
            session.query(ItineraryItem)
            .filter(ItineraryItem.itinerary_id == identifier, ItineraryItem.deleted == 0)
            .all()
        )
        assert len(rows) == 6 and {row.cost_currency for row in rows} == {"EUR"}
        assert all(
            day.generation_status == "SUCCEEDED"
            for day in session.query(ItineraryDay).filter_by(itinerary_id=identifier)
        )
        # 内容修订使旧排程结论失效；无需前端猜是否仍满足约束。
        main = session.get(ItineraryMain, identifier)
        assert main is not None
        main.planning_revision += 1
    from app.services import itinerary_query

    itinerary_query.evict_detail(1, identifier)
    refreshed = client.get(f"/api/itinerary/{identifier}").json()["data"]
    assert all(not day["constraintPlanning"]["isCurrent"] for day in refreshed["dayList"])


def test_foreign_currency_not_added_or_converted_in_budget(client, monkeypatch):
    identifier, _ = _submit(client, monkeypatch)
    with session_scope() as session:
        rows = (
            session.query(ItineraryItem)
            .filter_by(itinerary_id=identifier, deleted=0)
            .order_by(ItineraryItem.cost)
            .all()
        )
        rows[0].cost_currency = "USD"
    details = budget_engine.recalculate(identifier)
    assert sum(row.amount for row in details) == 130  # (11+12+13+14+15) * 2；无猜测交通费用。


def test_infeasible_report_persists_without_llm_retry_or_false_complete(client, monkeypatch):
    identifier, _ = _submit(client, monkeypatch, budget=0)
    detail = client.get(f"/api/itinerary/{identifier}").json()["data"]
    assert detail["genState"] in ("PARTIAL", "FAILED")
    assert all(not day["items"] for day in detail["dayList"])
    assert all(day["constraintPlanning"]["status"] == "INFEASIBLE" for day in detail["dayList"])
    assert all(day["constraintPlanning"]["conflictIds"] for day in detail["dayList"])


def test_recorded_real_place_flows_through_business_without_inventing_price_or_hours(client, monkeypatch, tmp_path):
    snapshot = json.loads(
        (Path(__file__).parent / "fixtures/constraint_planning/paris_observation_20261024.json").read_text(
            encoding="utf-8"
        )
    )
    row = snapshot["row"]
    requested_name = next((name for name in row["aliases"] if name == "Musée du Louvre"), row["name"])
    confirmed = planning_request(days=1).requirements_struct
    assert confirmed is not None
    req = confirmed.model_dump(mode="json", by_alias=True)
    req.update(
        fixedArrangements=[],
        placeRelations=[],
        requiredPlaces=[{"constraintId": "real-place", "name": requested_name, "dayNo": 1}],
        dayWindows=[{"dayNo": 1, "kind": "arrival", "notBefore": "14:00", "finishBy": "16:00"}],
    )
    identifier, _ = _submit(
        client, monkeypatch, context_payload={"candidates": [row]}, body_override={"days": 1, "requirementsStruct": req}
    )
    detail = client.get(f"/api/itinerary/{identifier}").json()["data"]
    day = detail["dayList"][0]
    assert len(day["items"]) == 1 and day["items"][0]["cost"] is None
    report = day["constraintPlanning"]
    assert report["status"] == "UNKNOWN" and report["numericalStatus"] in ("OPTIMAL", "FEASIBLE")
    assert report["facts"][0]["sourceEntityId"] == "osm:relation:7515426"
    assert report["facts"][0]["sourceUrl"] == "https://www.openstreetmap.org/relation/7515426"
    assert report["facts"][0]["retrievedAt"] == row["source_fetched_at"]
    assert report["facts"][0]["originalAmount"] is None
    (tmp_path / "p1-real-source-business-replay.json").write_text(
        json.dumps(
            {
                "method": (
                    "recorded Nominatim observation replay; real API/SQLite/plan_days/solver; "
                    "no LLM or hosted route calls"
                ),
                "observation": snapshot,
                "detail": detail,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


def _live_requirements(rows):
    confirmed = planning_request(days=1).requirements_struct
    assert confirmed is not None
    req = confirmed.model_dump(mode="json", by_alias=True)
    req.update(
        fixedArrangements=[],
        requiredPlaces=[{"constraintId": f"museum-{i}", "name": row["name"], "dayNo": 1} for i, row in enumerate(rows)],
        placeRelations=[
            {"constraintId": "visit-order", "kind": "before", "placeNames": [r["name"] for r in rows], "dayNo": 1}
        ],
        dayWindows=[{"dayNo": 1, "kind": "arrival", "notBefore": "14:00", "finishBy": "18:00"}],
    )
    return req


def _live_http(monkeypatch, exchanges):
    def capture(response):
        response.read()
        exchanges.append(
            {
                "url": str(response.request.url),
                "http_status": response.status_code,
                "requested_at": response.request.extensions["acceptance_requested_at"],
                "body": response.json(),
            }
        )

    def limit(request):
        assert request.url.host in ("routing.openstreetmap.de", "nominatim.openstreetmap.org")
        assert len(exchanges) < 4, "免费验收最多四次外呼，不自动扩张/重试"
        request.extensions["acceptance_requested_at"] = datetime.now(UTC).isoformat()

    monkeypatch.setattr(settings, "ors_base_url", "")
    monkeypatch.setattr(settings, "osrm_base_url", "https://routing.openstreetmap.de")
    monkeypatch.setattr(settings, "route_max_calls", 4)
    route_service.clear_route_cache()
    osrm_routes._client.clear_cache()
    osrm_routes._client.reset_runtime_state()
    http = httpx.Client(event_hooks={"request": [limit], "response": [capture]}, follow_redirects=False)
    monkeypatch.setattr(http_client, "_api_client", http)
    return http


@pytest.mark.skipif(os.getenv("SINAN_FREE_ROUTE_ACCEPTANCE") != "1", reason="需显式启用有界免费网络验收")
def test_live_free_osrm_walking_reaches_business_database_and_refresh(client, monkeypatch, tmp_path):
    exchanges, artifact = (
        [],
        {
            "method": "live free OSRM; frozen research; real API/plan_days/SQLite/readback",
            "model_calls": 0,
            "paid_calls": 0,
            "deployment": False,
        },
    )
    with _live_http(monkeypatch, exchanges):
        try:
            observation = json.loads(
                (
                    Path(__file__).parent / "fixtures/constraint_planning/paris_route_observations_20261024.json"
                ).read_text(encoding="utf-8")
            )
            artifact["place_observation"] = observation
            rows = observation["selected_places"]
            artifact["selected_places"] = rows
            identifier, _ = _submit(
                client,
                monkeypatch,
                context_payload={"candidates": rows},
                body_override={"days": 1, "requirementsStruct": _live_requirements(rows)},
            )
            detail = client.get(f"/api/itinerary/{identifier}").json()["data"]
            artifact["detail"] = detail
            day = detail["dayList"][0]
            report = day["constraintPlanning"]
            assert detail["genState"] == "COMPLETED" and len(day["items"]) == 2
            assert report["status"] == "UNKNOWN" and report["numericalStatus"] in ("OPTIMAL", "FEASIBLE")
            assert len(report["routes"]) == 1
            route = report["routes"][0]
            assert route["source"] == "osrm" and route["basis"] == "road_network" and not route["timeDependent"]
            assert route["fetchedAt"] and route["sourceUrl"] == settings.osrm_base_url
            assert "FOSSGIS" in route["attribution"]
            a, b = day["items"]

            def minute(value):
                return int(value[:2]) * 60 + int(value[3:5])

            assert minute(b["startTime"]) - minute(a["endTime"]) >= route["durationMin"]
            foot_exchange = next(e for e in exchanges if "/routed-foot/" in e["url"])
            first_index = sorted((r["longitude"], r["latitude"]) for r in rows).index(
                (rows[0]["longitude"], rows[0]["latitude"])
            )
            assert route["durationMin"] == math.ceil(
                foot_exchange["body"]["durations"][first_index][1 - first_index] / 60
            )
            artifact["accepted"] = True
        finally:
            artifact["exchanges"] = exchanges
            (tmp_path / "p1-live-route-business.json").write_text(
                json.dumps(artifact, ensure_ascii=False, indent=2), encoding="utf-8"
            )
