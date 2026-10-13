"""类别排除的证据边界：信息不足不能推翻名称，证据必须与地点身份一致。"""

from copy import deepcopy

import pytest

from app.agent.generation.rules.constraint_checks import check_constraints
from app.agent.generation.rules.day_policy import day_policy_for
from app.schemas.trip import DailyPlan, FactEvidence, TripItem
from app.schemas.trip_requirements import TripRequirements
from app.services.day_delivery_gate import hard_delivery_blockers


def _facts(**tags):
    identity = FactEvidence(
        provider="nominatim",
        source_url="https://www.openstreetmap.org/way/1",
        verification_status="verified",
        value_kind="observed",
    ).model_dump()
    return {"identity": deepcopy(identity), **{f"category:{k}={v}": deepcopy(identity) for k, v in tags.items()}}


def _check(name, facts, category="temple"):
    req = TripRequirements(excluded_categories=[category])
    plan = {"items": [{"poi_name": name, "item_type": "attraction", "fact_evidence": facts}]}
    report = check_constraints(plan, req, 1, day_policy_for(req, 1))
    return next(c for c in report.checks if c.kind == "excluded_category")


@pytest.mark.parametrize("tags", [{"amenity": "place_of_worship"}, {"tourism": "attraction"}, {"religion": "unknown"}])
def test_incomplete_tags_cannot_clear_temple_name(tags):
    assert _check("浅草寺", _facts(**tags)).status == "violation"


@pytest.mark.parametrize("category,name", [("temple", "神宫公园"), ("museum", "博物馆纪念品商店")])
@pytest.mark.parametrize(
    "field,value",
    [
        ("verification_status", "unverified"),
        ("verification_status", "partially_verified"),
        ("value_kind", "generated"),
        ("freshness_status", "stale"),
        ("source_url", None),
        ("source_url", "https://www.openstreetmap.org/way/2"),
        ("provider", "overpass"),
    ],
)
def test_unusable_category_fact_cannot_clear_name(category, name, field, value):
    facts = _facts(shop="gift")
    facts["category:shop=gift"][field] = value
    assert _check(name, facts, category).status == "violation"


@pytest.mark.parametrize(
    "field,value", [("verification_status", "unverified"), ("value_kind", "generated"), ("freshness_status", "stale")]
)
def test_unusable_identity_cannot_clear_name(field, value):
    facts = _facts(leisure="park")
    facts["identity"][field] = value
    assert _check("神宫公园", facts).status == "violation"


def test_missing_identity_cannot_clear_name():
    facts = _facts(leisure="park")
    facts.pop("identity")
    assert _check("神宫公园", facts).status == "violation"


@pytest.mark.parametrize("wire", [False, True])
def test_verified_distinct_use_can_clear_name_in_python_and_wire(wire):
    facts = _facts(leisure="park")
    facts = {k: FactEvidence.model_validate(v) for k, v in facts.items()}
    if wire:
        facts = {k: v.model_dump(by_alias=True) for k, v in facts.items()}
    assert _check("神宫公园", facts).status == "pass"


def test_positive_worship_evidence_wins_over_park_tag():
    assert _check("Meiji Jingu", _facts(religion="shinto", leisure="park")).status == "violation"


def test_missing_religion_worship_tag_cannot_be_cleared_by_park_tag():
    assert _check("浅草寺", _facts(amenity="place_of_worship", leisure="park")).status == "violation"


def test_museum_positive_evidence_wins_over_gift_shop_tag():
    assert _check("博物馆商店", _facts(tourism="museum", shop="gift"), "museum").status == "violation"


def test_verified_church_can_clear_temple_substring():
    assert _check("神宫路教堂", _facts(amenity="place_of_worship", religion="christian")).status == "pass"


def test_verified_historic_shrine_is_excluded():
    assert _check("Old Sanctuary", _facts(historic="shrine")).status == "violation"


def test_explicit_temple_building_wins_over_park_tag():
    assert _check("浅草寺", _facts(building="temple", leisure="park")).status == "violation"


def test_unverified_positive_tag_does_not_assert_entity_category():
    facts = _facts(religion="shinto")
    facts["category:religion=shinto"]["verification_status"] = "unverified"
    assert _check("City Park", facts).status == "pass"


@pytest.mark.parametrize("facts", [[], {"identity": {}, "category:leisure=park": "bad"}])
def test_malformed_or_legacy_facts_keep_name_guard(facts):
    assert _check("神宫公园", facts).status == "violation"


def test_same_name_items_are_checked_individually():
    req = TripRequirements(excluded_categories=["temple"])
    plan = {
        "items": [
            {"poi_name": "神宫公园", "item_type": "attraction", "fact_evidence": _facts(leisure="park")},
            {"poi_name": "神宫公园", "item_type": "attraction", "fact_evidence": _facts(tourism="attraction")},
        ]
    }
    report = check_constraints(plan, req, 1, day_policy_for(req, 1))
    check = next(c for c in report.checks if c.kind == "excluded_category")
    assert check.status == "violation"
    assert check.item_refs == ["神宫公园"]


@pytest.mark.parametrize("tags,blocked", [({"amenity": "place_of_worship"}, True), ({"leisure": "park"}, False)])
def test_delivery_boundary_preserves_category_decision(tags, blocked):
    plan = DailyPlan(
        day_no=1,
        items=[
            TripItem(
                item_type="attraction",
                poi_name="神宫公园",
                start_time="10:00",
                end_time="14:00",
                duration_min=240,
                fact_evidence={k: FactEvidence.model_validate(v) for k, v in _facts(**tags).items()},
            )
        ],
    )
    blockers = hard_delivery_blockers(1, plan, TripRequirements(excluded_categories=["temple"]))
    assert any("excluded_category" in issue for issue in blockers) is blocked
