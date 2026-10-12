"""发布 P1 回归：用户硬要求不能停留在软偏好，分店/饮食标签须有外部依据。"""

import json

import pytest

from app.agent.core.poi_identity import same_entity
from app.agent.data import places
from app.agent.editing import clarify
from app.agent.generation.content.reflect import hard_day_issues
from app.agent.generation.content.requirement_directives import requirements_directives_clause
from app.agent.generation.orchestration import day_workflow
from app.agent.generation.output.facts import field_fact_evidence
from app.agent.generation.rules.constraint_checks import check_constraints
from app.agent.generation.rules.day_policy import day_policy_for
from app.agent.generation.rules.generation_core import strip_model_claims
from app.agent.grounding import existence, facts
from app.agent.grounding.grounding_evidence import issue_evidence, lookup_ticket, verify_evidence
from app.agent.grounding.grounding_labels import label_for_landed_item
from app.common.config import settings
from app.schemas.requirement_patches import RequirementPatch, apply_intake_patches
from app.schemas.trip import ClarifyRequest, DailyPlan, GenerateDayRequest
from app.schemas.trip_requirements import IntakeState, TripRequirements
from app.services.day_delivery_gate import hard_delivery_blockers


def _plan():
    return {
        "day_no": 1,
        "items": [
            {"item_type": "attraction", "poi_name": "公园", "start_time": "09:00", "end_time": "12:00"},
            {"item_type": "food", "poi_name": "餐厅", "start_time": "12:30", "end_time": "13:30", "cost": 50},
            {"item_type": "attraction", "poi_name": "花园", "start_time": "14:00", "end_time": "16:00"},
        ],
    }


def test_count_limit_blocks_delivery_and_final_graph_attempt(monkeypatch):
    req = TripRequirements(max_attractions_per_day=1)
    plan = _plan()
    assert day_policy_for(req, 1).max_attractions == 1
    assert any("attraction_limit" in issue for issue in hard_day_issues(plan, req))
    wire = DailyPlan.model_validate(plan)
    assert any("attraction_limit" in issue for issue in hard_delivery_blockers(1, wire, req))
    monkeypatch.setattr(day_workflow, "generate_day_once", lambda *a, **kw: (wire, "llm"))
    with pytest.raises(ValueError, match="attraction_limit"):
        day_workflow.run_day_agent(GenerateDayRequest(city="东京", requirements_struct=req))


@pytest.mark.parametrize("name", ["浅草寺", "明治神宮神社", "灵隐寺", "龍山廟", "Sensoji Temple", "Meiji Shrine"])
def test_temple_category_is_enforced_across_languages(name):
    req = TripRequirements(excluded_categories=["temple"])
    plan = _plan()
    plan["items"][0]["poi_name"] = name
    assert any("excluded_category" in issue for issue in hard_day_issues(plan, req))


def test_diet_is_unknown_even_for_a_place_with_coordinates():
    plan = _plan()
    plan["items"][1].update(latitude=35.68, longitude=139.69, source="nominatim", poi_name="素食餐厅")
    strict = TripRequirements(dietary_requirement="vegetarian")
    assert any("尚无素食可用性依据" in issue for issue in hard_day_issues(plan, strict))
    candidates = strict.model_copy(update={"dining_evidence_policy": "review_candidates"})
    assert hard_day_issues(plan, candidates) == []
    report = check_constraints(plan, candidates, 1, day_policy_for(candidates, 1))
    assert report.unknown_checks[0].kind == "dietary_requirement"
    assert not report.unknown_checks[0].blocking
    assert "不能确认满足饮食要求" in report.unknown_checks[0].reason


def test_model_cannot_issue_dietary_evidence():
    item = {
        "poi_name": "编造分店",
        "fact_evidence": {"diet:vegan": {"verification_status": "verified"}},
        "factEvidence": {"identity": {}},
        "dietary_options": ["vegan"],
    }
    strip_model_claims(item)
    assert item == {"poi_name": "编造分店"}


@pytest.mark.parametrize("candidate", ["T’s たんたん", "T’s たんたん（東京駅店）"])
def test_brand_or_other_branch_cannot_verify_invented_branch(candidate):
    name = "T’s たんたん（新宿店）"
    assert not same_entity(name, candidate)
    row = {
        "name": candidate,
        "city": "东京",
        "source": "nominatim",
        "external_id": "p1-branch-test",
        "latitude": 35.68,
        "longitude": 139.69,
    }
    issue_evidence(row)
    assert not verify_evidence({**row, "name": name})
    assert not verify_evidence({**row, "source": "web.search"})


@pytest.mark.parametrize("query", ["T's Tantan Shinjuku", "T's Tantan (Shinjuku)"])
def test_english_branch_cannot_inherit_brand_or_other_branch(query):
    assert not same_entity(query, "T's Tantan")
    assert not same_entity(query, "T's Tantan Tokyo")
    assert same_entity(query, query)


@pytest.mark.parametrize(
    "tags, expected",
    [
        ({}, []),
        ({"diet:vegetarian": "limited"}, []),
        ({"diet:vegetarian": "yes"}, ["vegetarian"]),
        ({"diet:vegan": "only"}, ["vegan", "vegetarian"]),
    ],
)
def test_geocoder_uses_actual_diet_tags_and_source_entity(monkeypatch, tags, expected):
    monkeypatch.setattr(settings, "nominatim_enabled", True)

    def fetch(*args, **kwargs):
        assert kwargs["params"]["extratags"] == "1"
        return [
            {
                "lat": "35.68",
                "lon": "139.69",
                "name": "Restaurant",
                "osm_type": "node",
                "osm_id": 123,
                "extratags": tags,
            }
        ]

    monkeypatch.setattr(places, "fetch_json", fetch)
    monkeypatch.setattr(places._nominatim_client, "call", lambda key, loader: loader())
    rows = places.geocode_place_rows("Restaurant", "东京", namedetails=True)
    assert rows and rows[0]["dietary_options"] == expected
    assert rows[0]["place_id"] == "osm:node:123"
    assert rows[0]["source_url"] == "https://www.openstreetmap.org/node/123"


def test_observed_diet_flows_from_provider_to_ticket_to_delivery(monkeypatch):
    monkeypatch.setattr(settings, "nominatim_enabled", True)
    existence.reset_existence_state()
    monkeypatch.setattr(existence, "city_center", lambda city: (35.68, 139.69))
    monkeypatch.setattr(existence, "providers_in_order", lambda: [existence.NominatimProvider()])
    row = {
        "name": "餐厅",
        "latitude": 35.68,
        "longitude": 139.69,
        "place_id": "p1-diet-test",
        "source_url": "https://www.openstreetmap.org/node/123",
        "dietary_options": ["vegetarian"],
    }
    monkeypatch.setattr(places, "geocode_place_rows", lambda *a, **kw: [row])
    item = _plan()["items"][1]
    assert facts.local_ground(item, "东京")
    assert item["poi_id"] == "p1-diet-test"
    ticket = lookup_ticket("餐厅", "东京", "p1-diet-test")
    assert ticket and ticket.dietary_options == ("vegetarian",)
    label = label_for_landed_item(item, "东京")
    item["fact_evidence"] = field_fact_evidence(item, label, cost_observed=False, open_time_observed=False)
    assert item["fact_evidence"]["identity"].verification_status == "verified"
    assert item["fact_evidence"]["diet:vegetarian"].review_requirement == "before_departure"
    assert "diet:vegan" not in item["fact_evidence"]
    plan = _plan()
    plan["items"][1] = item
    req = TripRequirements(dietary_requirement="vegetarian", dining_evidence_policy="verified_only")
    assert hard_day_issues(plan, req) == []
    assert hard_delivery_blockers(1, DailyPlan.model_validate(plan), req) == []
    vegan = req.model_copy(update={"dietary_requirement": "vegan"})
    assert any("尚无纯素" in issue for issue in hard_day_issues(plan, vegan))
    existence.reset_existence_state()


def test_user_must_choose_dining_policy_and_diet_change_resets_it(monkeypatch):
    patches = [
        {"op": "set", "target": "max_attractions_per_day", "value": 2},
        {"op": "set", "target": "excluded_category", "value": "temple"},
        {"op": "set", "target": "dietary_requirement", "value": "vegetarian"},
    ]
    monkeypatch.setattr(clarify, "_ask", lambda *a: json.dumps({"patches": patches}))
    state = IntakeState(city="东京", days=2, persons=2)
    result = clarify.run_clarify(ClarifyRequest(message="素食，不去寺庙，每天最多两个景点", state=state))
    assert result.blocked and not result.ready and result.next == "dining_evidence_policy"
    assert result.state.requirements.max_attractions_per_day == 2
    assert result.state.requirements.excluded_categories == ["temple"]
    req = state.requirements
    assert "最多 2 个" in requirements_directives_clause(req)
    assert "寺庙/神社" in requirements_directives_clause(req)
    assert (
        apply_intake_patches(
            state, [RequirementPatch(op="set", target="dining_evidence_policy", value="review_candidates")]
        )
        == []
    )
    monkeypatch.setattr(clarify, "_ask", lambda *a: "{}")
    assert clarify.run_clarify(ClarifyRequest(message="继续", state=state)).ready
    assert "不可声称已满足" in requirements_directives_clause(req)
    apply_intake_patches(state, [RequirementPatch(op="set", target="dietary_requirement", value="vegan")])
    assert req.dining_evidence_policy is None
    apply_intake_patches(state, [RequirementPatch(op="unset", target="dietary_requirement")])
    assert req.dietary_requirement is None
    assert apply_intake_patches(state, [RequirementPatch(op="set", target="max_attractions_per_day", value=0)])
    assert apply_intake_patches(state, [RequirementPatch(op="set", target="dietary_requirement", value="anything")])


def test_model_cannot_accept_candidate_policy_for_user(monkeypatch):
    state = IntakeState(city="东京", days=2, persons=2, requirements=TripRequirements(dietary_requirement="vegan"))
    monkeypatch.setattr(
        clarify,
        "_ask",
        lambda *a: json.dumps(
            {"patches": [{"op": "set", "target": "dining_evidence_policy", "value": "review_candidates"}]}
        ),
    )
    response = clarify.run_clarify(ClarifyRequest(message="直接开始", state=state))
    assert not response.ready and state.requirements.dining_evidence_policy is None
    response = clarify.run_clarify(ClarifyRequest(message="先给餐饮候选，我会自行核实", state=state))
    assert response.ready and state.requirements.dining_evidence_policy == "review_candidates"


def test_changed_dining_policy_requires_new_explicit_choice(monkeypatch):
    state = IntakeState(
        city="东京",
        days=2,
        persons=2,
        requirements=TripRequirements(dietary_requirement="vegetarian", dining_evidence_policy="review_candidates"),
    )
    monkeypatch.setattr(
        clarify,
        "_ask",
        lambda *a: json.dumps(
            {"patches": [{"op": "set", "target": "dining_evidence_policy", "value": "verified_only"}]}
        ),
    )
    response = clarify.run_clarify(ClarifyRequest(message="改成只安排有素食依据的餐厅", state=state))
    assert not response.ready and state.requirements.dining_evidence_policy is None
    response = clarify.run_clarify(ClarifyRequest(message="只安排有饮食依据的餐厅", state=state))
    assert response.ready and state.requirements.dining_evidence_policy == "verified_only"


def test_missing_food_and_stale_diet_evidence_cannot_pass():
    req = TripRequirements(dietary_requirement="vegetarian", dining_evidence_policy="verified_only")
    plan = _plan()
    plan["items"][1]["fact_evidence"] = {
        "identity": {"verification_status": "verified"},
        "diet:vegetarian": {
            "verification_status": "verified",
            "value_kind": "observed",
            "freshness_status": "stale",
            "source_url": "https://www.openstreetmap.org/node/123",
        },
    }
    assert any("尚无素食" in issue for issue in hard_day_issues(plan, req))
    plan["items"] = [item for item in plan["items"] if item["item_type"] != "food"]
    assert any("未提供餐饮安排" in issue for issue in hard_day_issues(plan, req))
