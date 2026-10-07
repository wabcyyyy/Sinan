from app.agent.data.route_service import RouteService, clear_route_cache


def _poi(name, lat=30.0, lng=120.0):
    return {"poi_id": name, "poi_name": name, "latitude": lat, "longitude": lng}


def test_route_service_uses_provider_and_cache(monkeypatch):
    calls = []

    def fetcher(first, second, mode, departure):
        calls.append((first["poi_name"], second["poi_name"], mode, departure))
        return {
            "from_poi_id": first["poi_id"],
            "to_poi_id": second["poi_id"],
            "mode": mode,
            "distance_m": 1000,
            "duration_min": 12,
            "source": "amap",
            "confidence": 0.95,
            "degraded": False,
        }

    clear_route_cache()
    service = RouteService(fetcher=fetcher)
    first, second = _poi("A"), _poi("B", lng=120.01)
    route = service.get_route(first, second)
    assert route is not None and route["source"] == "amap"
    route = service.get_route(first, second)
    assert route is not None and route["duration_min"] == 12
    assert len(calls) == 1


def test_route_service_falls_back_to_coordinate_estimate(monkeypatch):
    def failing(*_args):
        raise TimeoutError("timeout")

    clear_route_cache()
    service = RouteService(fetcher=failing)
    route = service.get_route(_poi("A"), _poi("B", lat=30.1))
    assert route is not None
    assert route["source"] == "coordinate-estimate"
    assert route["degraded"] is True
    assert route["duration_min"] > 0


def test_route_service_peak_factor_is_explicit(monkeypatch):
    monkeypatch.setattr("app.agent.data.route_service.settings.route_peak_factor", 1.5)
    clear_route_cache()
    service = RouteService(
        fetcher=lambda *_args: {
            "from_poi_id": "A",
            "to_poi_id": "B",
            "mode": "walking",
            "distance_m": 1000,
            "duration_min": 10,
            "source": "amap",
            "confidence": 0.95,
            "degraded": False,
        }
    )
    route = service.get_route(_poi("A"), _poi("B"), departure_time="08:00")
    assert route is not None
    assert route["base_duration_min"] == 10
    assert route["duration_min"] == 15
