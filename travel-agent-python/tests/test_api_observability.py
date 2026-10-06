from fastapi.testclient import TestClient

from app.api import agent
from app.schemas.trip import GenerateResponse
from main import app


def test_generate_exposes_run_id_without_exposing_trace(monkeypatch):
    monkeypatch.setattr(
        agent,
        "run_generate",
        lambda _req: GenerateResponse(city="杭州", days=1, title="杭州1日游", daily_plans=[]),
    )
    with TestClient(app) as client:
        response = client.post(
            "/api/agent/v1/generate", headers={"X-Request-ID": "request-api"}, json={"city": "杭州", "days": 1}
        )
    assert response.status_code == 200
    assert response.headers.get("X-Agent-Run-ID")
    assert response.headers.get("X-Request-ID") == "request-api"
    assert "events" not in response.json()["data"]

    run_id = response.headers["X-Agent-Run-ID"]
    trace_response = client.get(f"/api/agent/v1/runs/{run_id}")
    assert trace_response.status_code == 200
    assert trace_response.json()["data"]["run_id"] == run_id
    assert trace_response.json()["data"]["request_id"] == "request-api"
    assert "events" in trace_response.json()["data"]


def test_run_trace_returns_404_for_unknown_id():
    with TestClient(app) as client:
        response = client.get("/api/agent/v1/runs/not-found")
    assert response.status_code == 404


def test_failed_generation_keeps_run_id_for_trace_lookup(monkeypatch):
    def fail(_req):
        raise ValueError("unsupported city")

    monkeypatch.setattr(agent, "run_generate", fail)
    with TestClient(app) as client:
        response = client.post("/api/agent/v1/generate", json={"city": "未知", "days": 1})
        run_id = response.headers.get("X-Agent-Run-ID")
        trace_response = client.get(f"/api/agent/v1/runs/{run_id}")
    assert response.status_code == 200
    assert response.json()["code"] == 500
    assert run_id
    assert trace_response.status_code == 200
    assert any(
        event["metadata"]["status"] == "failed"
        for event in trace_response.json()["data"]["events"]
        if event["name"] == "run_status"
    )


def test_metrics_endpoint_returns_aggregates():
    with TestClient(app) as client:
        response = client.get("/api/agent/v1/metrics")
    assert response.status_code == 200
    body = response.json()["data"]
    assert "success_rate" in body
    assert "mcp_failure_rate" in body
