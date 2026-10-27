"""真实模型用户表达→原 parse/compare/select/apply→逐轮独立数据库检查。"""

import json
import os
import traceback
from pathlib import Path

import httpx
import pytest

from app.agent.data.route_service import RouteService
from app.agent.generation.orchestration import constraint_planning
from app.schemas.replanning import ReplanChange
from app.schemas.trip_requirements import TripRequirements
from scripts.p5_scenarios import FAMILIES, semantic_errors
from scripts.run_p4_acceptance import save
from tests.p5_trajectory_cases import check_database, frozen_matrix, read_database, setup
from tests.test_constraint_business import _submit

pytest_plugins = ("tests.test_m1b_requirements_pipeline",)
FIXTURES = Path(__file__).parent / "fixtures/simulation"


@pytest.mark.parametrize("family", FAMILIES)
def test_real_user_trajectories(family, client, monkeypatch, tmp_path):
    source = FIXTURES / f"p5_{family}_real_20261025.json"
    if family == "negation_boundary":
        source = FIXTURES / "p5_negation_boundary_controller_v2_20261025.json"
    if not source.exists():
        pytest.skip("此故事真实模型表达尚未录制")
    recorded = json.loads(source.read_text("utf-8"))
    output = Path(os.environ.get("SINAN_P5_REPORT_DIR", str(tmp_path / "p5")))
    output.mkdir(parents=True, exist_ok=True)
    report = {
        "family": family,
        "recording_source": str(source),
        "split": recorded["profile"]["split"],
        "intake_checks": recorded["intake_checks"],
        "mode": (
            "recorded real model intake/user messages; real API/worker/SQLite; frozen POI/routes; no new models/network"
        ),
        "trajectories": [],
    }
    monkeypatch.setattr(constraint_planning, "get_route_matrix", frozen_matrix)
    monkeypatch.setattr(RouteService, "matrix", staticmethod(frozen_matrix))

    def deny_external(*_a, **_kw):
        raise RuntimeError("P5回放禁止外部HTTP传输")

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", deny_external)
    for story in recorded["trajectories"]:
        trace = {"id": story["id"], "turns": [], "agent_errors": [], "simulator_valid": True}
        try:
            req, context = setup(recorded["profile"], recorded["intake"])
            identifier, _ = _submit(
                client,
                monkeypatch,
                context_payload=context,
                budget=1000,
                body_override={
                    "city": "Paris",
                    "days": recorded["profile"]["days"],
                    "persons": 1,
                    "startDate": "2026-11-03",
                    "requirementsStruct": req.model_dump(mode="json", by_alias=True),
                },
            )
            detail = client.get(f"/api/itinerary/{identifier}").json()["data"]
            assert detail["genState"] == "COMPLETED", detail
            trace["initial_database"] = read_database(identifier)
            for turn in story["turns"]:
                operation = turn["operation"]
                row = {
                    "message": turn["message"],
                    "operation": operation,
                    "original_simulator_errors": turn["simulator_errors"],
                    "simulator_errors": semantic_errors(turn["message"], operation),
                }
                trace["turns"].append(row)
                if row["simulator_errors"]:
                    row["status"] = "SIMULATOR_INVALID"
                    trace["simulator_valid"] = False
                    break
                before = read_database(identifier)
                detail = client.get(f"/api/itinerary/{identifier}").json()["data"]
                current_req = TripRequirements.model_validate(detail["requirementsStruct"])
                target = next((p for d in before for p in d["items"] if p["poi_name"] == operation.get("target")), None)
                parsed = client.post(
                    f"/api/itinerary/{identifier}/replans/parse",
                    json={
                        "planningRevision": detail["planningRevision"],
                        "dayNo": operation.get("day_no", 1),
                        "itemId": target["id"] if target else None,
                        "message": turn["message"],
                    },
                )
                assert parsed.status_code == 200, parsed.text
                row["parsed"] = parsed.json()["data"]
                assert read_database(identifier) == before
                if operation["expected"] == "clarify":
                    assert row["parsed"]["change"] is None and row["parsed"]["questions"]
                    row["status"] = "SAFE_CLARIFICATION"
                    continue
                fields = {k: v for k, v in operation.items() if k not in {"target", "expected"}}
                if target:
                    fields["item_id"] = target["id"]
                expected = ReplanChange(**fields)
                canonical = expected.model_dump(mode="json", by_alias=True)
                row["direct_parse_correct"] = row["parsed"]["change"] == canonical
                row["simulated_form_confirmation"] = not row["direct_parse_correct"]
                # 追问后的表单确认是控制器显式操作，单列统计，不冒充模型理解成功。
                response = client.post(
                    f"/api/itinerary/{identifier}/replans/compare",
                    json={
                        "planningRevision": detail["planningRevision"],
                        "completedItemIds": [],
                        "optionalItemIds": [],
                        "allowCrossDay": True,
                        "change": canonical,
                    },
                )
                row["compare_status"] = response.status_code
                row["comparison"] = response.json()
                assert read_database(identifier) == before
                options = response.json().get("data", {}).get("options", []) if response.status_code == 200 else []
                if operation["expected"] == "reject":
                    assert response.status_code in {200, 400, 409, 422} and not options, response.text
                    row["status"] = "REJECTED_UNCHANGED"
                    continue
                assert response.status_code == 200 and options, response.text
                comparison = response.json()["data"]
                chosen = next((o for o in options if o["key"] == "minimal"), options[0])
                selected = client.post(
                    f"/api/itinerary/{identifier}/replans/select",
                    json={
                        "planningRevision": detail["planningRevision"],
                        "comparisonId": comparison["comparisonId"],
                        "choice": chosen["key"],
                    },
                )
                assert selected.status_code == 200, selected.text
                assert read_database(identifier) == before
                action = selected.json()["data"]
                applied = client.post(
                    f"/api/itinerary/{identifier}/apply-plans",
                    json={"actionMessageId": action["messageId"], "baseRevision": action["baseRevision"]},
                )
                assert applied.status_code == 200, applied.text
                after = read_database(identifier)
                row["checker_errors"] = check_database(before, after, expected, current_req, recorded["profile"])
                assert not row["checker_errors"], row["checker_errors"]
                row.update(status="APPLIED_AND_CHECKED", before=before, after=after)
                refreshed = client.get(f"/api/itinerary/{identifier}").json()["data"]
                assert refreshed["dayList"] == applied.json()["data"]["dayList"]
                saved_req = TripRequirements.model_validate(refreshed["requirementsStruct"])
                assert all(f in saved_req.fixed_arrangements for f in current_req.fixed_arrangements)
                if operation["kind"] == "appointment":
                    assert target is not None
                    assert any(
                        f.name == target["poi_name"]
                        and f.day_no == expected.day_no
                        and f.start_time == expected.start_time
                        and f.end_time == expected.end_time
                        for f in saved_req.fixed_arrangements
                    ), "新增预约必须随确认行程同事务保存，不能只保存条目时刻"
                row["persisted_requirements"] = refreshed["requirementsStruct"]
                if operation["kind"] != "close" and refreshed["planningRevision"] > detail["planningRevision"]:
                    stale = client.post(
                        f"/api/itinerary/{identifier}/replans/compare",
                        json={"planningRevision": detail["planningRevision"], "change": canonical},
                    )
                    assert stale.status_code == 409 and read_database(identifier) == after
                    row["stale_revision_rejected"] = True
            trace["final_database"] = read_database(identifier)
        except Exception as exc:
            trace["agent_errors"].append(f"{type(exc).__name__}: {exc}")
            trace["failure_traceback"] = traceback.format_exc()
        trace["passed_with_confirmation"] = (
            trace["simulator_valid"] and not trace["agent_errors"] and len(trace["turns"]) == 3
        )
        report["trajectories"].append(trace)
        save(output / f"{family}.json", report)
    assert any(t["simulator_valid"] for t in report["trajectories"]), "全部模拟器轨迹无效，不能记业务通过"
    assert not [t for t in report["trajectories"] if t["agent_errors"]], report
