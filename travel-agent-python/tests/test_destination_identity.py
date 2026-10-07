"""目的地闸与中文名称：别名有据，不把搜索第一项当目的地。"""

import pytest

from app.agent.data import city_center, places
from app.agent.editing import clarify
from app.agent.generation.content.day_prompts import open_trip_prompt
from app.agent.generation.content.reflect import (
    MAX_DAILY_ATTRACTIONS,
    MAX_DAILY_MINUTES,
    MIN_ACTIVE_MINUTES,
)
from app.agent.grounding import existence, facts, suggestion_grounding
from app.common.envelope import ApiError
from app.prompts.open_generation import open_day_system_prompt
from app.schemas.business.itinerary import GenerateTripRequest
from app.schemas.trip import ClarifyRequest, GenerateDayRequest
from app.services import itinerary_generation


@pytest.fixture
def geocoder(monkeypatch):
    monkeypatch.setattr(city_center.city_reference, "get_city_geo", lambda city: None)
    monkeypatch.setattr(
        places, "resolve_city_center", lambda city: {"name": "Trakai", "latitude": 54.6, "longitude": 24.9}
    )
    rows = [{"name": "Trakai", "latitude": 54.6, "longitude": 24.9, "aliases": []}]
    monkeypatch.setattr(places, "geocode_place_rows", lambda *a, **kw: rows)
    return rows


def test_unrelated_first_city_is_rejected_and_user_can_correct(geocoder, monkeypatch):
    assert city_center.city_center("亚特兰蒂斯") is None
    monkeypatch.setattr(clarify, "_ask", lambda req: "{}")
    result = clarify.run_clarify(ClarifyRequest(message="开始", slots={"city": "亚特兰蒂斯", "days": 3, "persons": 2}))
    assert result.blocked and not result.ready and result.missing == ["city"]
    assert result.question is not None
    assert "更正城市名称" in result.question
    assert result.slots["city"] == "亚特兰蒂斯"
    with pytest.raises(ApiError) as raised:
        itinerary_generation.generate(1, GenerateTripRequest(city="亚特兰蒂斯", days=3, persons=2))
    assert raised.value.status == 400


def test_matching_city_alias_can_be_after_an_unrelated_hit(geocoder):
    geocoder.append({"name": "東京都", "aliases": ["东京", "Tokyo"], "latitude": 35.68, "longitude": 139.69})
    assert city_center.city_center("东京") == {"latitude": 35.68, "longitude": 139.69}
    assert city_center.destination_problem("东京") is None
    hit = places.geocode_place("东京")
    assert hit is not None and hit["name"] == "東京都"


def test_substring_hotel_is_not_a_city(geocoder):
    geocoder[:] = [{"name": "Atlantis Hotel", "aliases": [], "latitude": 25.0, "longitude": 55.0}]
    assert city_center.city_center("Atlantis") is None
    assert city_center.destination_problem("Atlantis") is not None


def test_city_lookup_failure_is_unknown_not_rejection(geocoder, monkeypatch):
    monkeypatch.setattr(places, "geocode_place_rows", lambda *a, **kw: None)
    assert city_center.destination_problem("巴黎") is None
    assert city_center.city_center("巴黎") is None


def test_provider_missing_name_cannot_confirm_requested_city(monkeypatch):
    monkeypatch.setattr(places, "_otm_get", lambda *args: {"lat": 54.6, "lon": 24.9})
    monkeypatch.setattr(city_center.city_reference, "get_city_geo", lambda city: None)
    monkeypatch.setattr(places, "geocode_place_rows", lambda *args, **kwargs: [])
    hit = places.resolve_city_center("亚特兰蒂斯")
    assert hit is not None and hit["name"] == ""
    assert city_center.city_center("亚特兰蒂斯") is None
    assert city_center.destination_problem("亚特兰蒂斯") is not None


def test_known_city_uses_dictionary_without_external_calls(monkeypatch):
    monkeypatch.setattr(city_center.city_reference, "get_city_geo", lambda city: {"lat": "30.2", "lng": "120.1"})
    monkeypatch.setattr(places, "geocode_place_rows", lambda *a, **kw: pytest.fail("known city must not geocode"))
    assert city_center.city_center("杭州") == {"latitude": 30.2, "longitude": 120.1}
    assert city_center.destination_problem("杭州") is None


def test_grounded_chinese_alias_is_used_without_losing_coordinates(monkeypatch):
    existence.reset_existence_state()
    monkeypatch.setattr(existence, "city_center", lambda city: (35.68, 139.69))
    monkeypatch.setattr(existence, "providers_in_order", lambda: [existence.NominatimProvider()])
    monkeypatch.setattr(existence.settings, "nominatim_enabled", True)
    monkeypatch.setattr(
        places,
        "geocode_place_rows",
        lambda *a, **kw: [
            {
                "name": "東京国立博物館",
                "aliases": ["东京国立博物馆", "東京国立博物館"],
                "localized_name": "东京国立博物馆",
                "latitude": 35.719,
                "longitude": 139.776,
            }
        ],
    )
    try:
        item = {"poi_name": "東京国立博物館"}
        assert facts.local_ground(item, "东京")
        assert item["poi_name"] == "东京国立博物馆"
        assert item["source"] == "nominatim" and item["latitude"] == 35.719
        rows, stats = suggestion_grounding.verify_suggestion_rows([{"name": "東京国立博物館"}], "东京", limit=1)
        assert rows[0]["name"] == "东京国立博物馆" and stats["filled"] == 1
    finally:
        existence.reset_existence_state()


def test_missing_localized_alias_keeps_original_name(monkeypatch):
    monkeypatch.setattr(
        facts,
        "resolve_poi",
        lambda *args: existence.ResolveResult(
            state=existence.VERIFIED,
            provider="nominatim",
            name="Original",
            latitude=35.7,
            longitude=139.7,
        ),
    )
    item = {"poi_name": "原名"}
    assert facts.local_ground(item, "东京")
    assert item["poi_name"] == "原名"


@pytest.mark.parametrize("path", ["day", "trip"])
def test_localized_names_are_consistent_in_generated_narrative(monkeypatch, path):
    from app.agent.generation.orchestration import day_stream, open_plans
    from app.schemas.trip import GenerateRequest

    monkeypatch.setattr(existence.settings, "llm_api_key", "configured")
    monkeypatch.setattr(
        facts,
        "resolve_poi",
        lambda *args: existence.ResolveResult(
            state=existence.VERIFIED,
            provider="nominatim",
            name="東京国立博物館",
            localized_name="东京国立博物馆",
            latitude=35.719,
            longitude=139.776,
        ),
    )

    def draft(*args):
        return {
            "day_no": 1,
            "theme": "東京国立博物館文化慢游",
            "note": "游览東京国立博物館",
            "photo_spots": [{"name": "東京国立博物館入口", "tip": "早上拍"}],
            "practical_notes": ["東京国立博物館提前预约"],
            "items": [
                {
                    "item_type": "attraction",
                    "poi_name": "東京国立博物館",
                    "cost": 100,
                    "start_time": "09:00",
                    "end_time": "13:00",
                }
            ],
        }

    if path == "day":
        monkeypatch.setattr(day_stream, "llm_open_day", draft)
        plan, _ = day_stream.generate_day_once(GenerateDayRequest(city="东京", days=1, day_no=1), force_fallback=True)
        data = plan.model_dump()
    else:
        monkeypatch.setattr(open_plans, "llm_open_day", draft)
        monkeypatch.setattr(open_plans, "activity_floor", lambda city: [])
        result, errors = open_plans.generate_open_plans(GenerateRequest(city="东京", days=1), "", [])
        assert not errors
        assert result is not None
        data = result["daily_plans"][0]
    assert data["items"][0]["poi_name"] == "东京国立博物馆"
    assert data["theme"] == ("东京国立博物馆" if path == "day" else "东京国立博物馆文化慢游")
    assert data["photo_spots"][0]["name"] == "东京国立博物馆入口"
    assert data["practical_notes"] == ["东京国立博物馆提前预约"]


def test_both_generation_prompts_require_consistent_chinese_names():
    class Memory:
        def as_sorted_list(self):
            return []

    prompts = [
        open_day_system_prompt(
            day_no=1,
            pace="",
            hotel_clause="",
            hotel_hint="",
            mem=Memory(),
            min_active_minutes=MIN_ACTIVE_MINUTES,
            max_daily_minutes=MAX_DAILY_MINUTES,
            max_daily_attractions=MAX_DAILY_ATTRACTIONS,
        ),
        open_trip_prompt(GenerateDayRequest(city="东京", days=3, day_no=1))[0],
    ]
    assert all("简体中文" in prompt and "禁止编造翻译" in prompt and "保持同一写法" in prompt for prompt in prompts)
