import httpx
import pytest

from app.agent.data import osrm_routes, route_service
from app.common import http_client
from app.common.config import settings


@pytest.fixture
def osrm(monkeypatch):
    monkeypatch.setattr(settings, "ors_base_url", "")
    monkeypatch.setattr(settings, "osrm_base_url", "https://osrm.test")
    osrm_routes._client.clear_cache()
    osrm_routes._client.reset_runtime_state()
    assert osrm_routes._client.min_interval_seconds == 1.0
    monkeypatch.setattr(osrm_routes._client, "min_interval_seconds", 0.001)
    route_service.clear_route_cache()
    return [
        {"poi_id": "a", "longitude": 2.33, "latitude": 48.86},
        {"poi_id": "b", "longitude": 2.34, "latitude": 48.87},
    ]


def test_osrm_table_preserves_direction_units_source_and_caches_reverse(osrm, monkeypatch):
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "code": "Ok",
                "durations": [[0, 121], [241, 0]],
                "distances": [[0, 840], [1500, 0]],
                "data_version": "2026-10-23T00:00:00Z",
            },
        )

    monkeypatch.setattr(http_client, "_api_client", httpx.Client(transport=httpx.MockTransport(respond)))
    matrix = route_service.get_route_matrix(osrm, departure_time="2026-10-25T08:00:00+01:00")
    assert len(requests) == 1
    assert requests[0].url.path.startswith("/routed-foot/table/v1/foot/")
    assert requests[0].url.params["annotations"] == "duration,distance"
    assert "TravelAssistantDemo" in requests[0].headers["User-Agent"]
    assert matrix["a", "b"]["duration_min"] == 3
    assert matrix["b", "a"]["duration_min"] == 5
    assert matrix["a", "b"]["fetched_at"] == matrix["b", "a"]["fetched_at"]
    assert matrix["a", "b"]["data_date"] == "2026-10-23T00:00:00Z"
    assert matrix["a", "b"]["time_dependent"] is False and "peak_factor" not in matrix["a", "b"]
    assert "OpenStreetMap" in matrix["a", "b"]["attribution"]


@pytest.mark.parametrize("metric", [None, -1])
def test_unroutable_or_invalid_table_is_explicitly_estimated(osrm, monkeypatch, metric):
    monkeypatch.setattr(
        http_client,
        "_api_client",
        httpx.Client(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    200, json={"code": "Ok", "durations": [[0, metric], [metric, 0]], "distances": [[0, 100], [100, 0]]}
                )
            )
        ),
    )
    matrix = route_service.get_route_matrix(osrm)
    assert all(r["source"] == "coordinate-estimate" and r["degraded"] for r in matrix.values())


def test_explicit_config_only_and_ors_priority(osrm, monkeypatch):
    calls = []
    monkeypatch.setattr(route_service, "fetch_ors_route", lambda *args: calls.append("ors"))
    monkeypatch.setattr(route_service, "fetch_osrm_route", lambda *args: calls.append("osrm"))
    monkeypatch.setattr(settings, "ors_base_url", "https://ors.test")
    route_service.fetch_configured_route(osrm[0], osrm[1], "walking", None)
    assert calls == ["ors"]
    monkeypatch.setattr(settings, "ors_base_url", "")
    monkeypatch.setattr(settings, "osrm_base_url", "")
    route_service.get_route_matrix(osrm)
    assert calls == ["ors"]
    assert osrm_routes.fetch_osrm_route(osrm[0], osrm[1], "transit", None) is None
