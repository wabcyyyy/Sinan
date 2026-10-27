import httpx

from app.agent.data import route_service
from app.agent.data.ors_routes import fetch_ors_route
from app.common import http_client
from app.common.config import settings


def test_ors_wire_units_direction_and_static_time_not_peak_factored(monkeypatch):
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "durations": [[121]],
                "distances": [[840]],
                "metadata": {"engine": {"graph_date": "2026-10-20T00:00:00Z"}},
            },
        )

    monkeypatch.setattr(settings, "ors_base_url", "http://ors.test/ors")
    monkeypatch.setattr(settings, "ors_region", "ile-de-france")
    monkeypatch.setattr(http_client, "_api_client", httpx.Client(transport=httpx.MockTransport(respond)))
    route_service.clear_route_cache()
    service = route_service.RouteService(fetch_ors_route)
    first = {"poi_id": "a", "latitude": 48.86, "longitude": 2.33}
    second = {"poi_id": "b", "latitude": 48.87, "longitude": 2.34}
    matrix = service.matrix([first, second], departure_time="2026-10-25T08:00:00+01:00")
    assert len(requests) == 2 and requests[0].url.path == "/ors/v2/matrix/foot-walking"
    assert matrix["a", "b"]["duration_min"] == 3
    assert matrix["a", "b"]["source"] == "openrouteservice"
    assert matrix["b", "a"]["time_dependent"] is False and "peak_factor" not in matrix["b", "a"]


def test_no_config_and_unsupported_modes_do_not_call_hosted_routes(monkeypatch):
    monkeypatch.setattr(settings, "ors_base_url", "")
    assert fetch_ors_route({}, {}, "walking", None) is None
    monkeypatch.setattr(settings, "ors_base_url", "http://ors.test")
    assert fetch_ors_route({}, {}, "transit", None) is None
