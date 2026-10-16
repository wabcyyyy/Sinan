"""发布 P1 回归：用户硬要求不能停留在软偏好，分店/饮食标签须有外部依据。"""

import json

import pytest

from app.agent.core.poi_identity import same_entity
from app.agent.data import overpass, places
from app.agent.editing import clarify
from app.agent.generation.content.reference_pool import ReferencePool
from app.agent.generation.content.reflect import hard_day_issues
from app.agent.generation.content.requirement_directives import requirements_directives_clause
from app.agent.generation.orchestration import day_workflow
from app.agent.generation.output.facts import field_fact_evidence
from app.agent.generation.rules.constraint_checks import check_constraints
from app.agent.generation.rules.day_policy import day_policy_for
from app.agent.generation.rules.generation_core import strip_model_claims
from app.agent.grounding import existence, facts
from app.agent.grounding.grounding_evidence import evidence_row, issue_evidence, verify_evidence
from app.agent.grounding.grounding_labels import apply_label, label_for_evidence_row, label_for_landed_item
from app.agent.tools import impl as tools
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
    ticket = evidence_row({"name": item["poi_name"], "source": item["source"], "evidence_id": item["evidence_id"]})
    assert ticket and ticket["dietary_options"] == ["vegetarian"]
    label = label_for_landed_item(item, "东京")
    item["fact_evidence"] = field_fact_evidence(
        item, label, cost_observed=False, open_time_observed=False, evidence_id=item["evidence_id"]
    )
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


# ---- T5b：检索类别不得改写用途（改类型绕不过饮食闸） --------------------------------

_STRICT_DIET = TripRequirements(dietary_requirement="vegetarian", dining_evidence_policy="verified_only")


def _overpass_restaurant(node: int = 1, *, diet: str | None = None, category: str | None = None) -> dict:
    """Overpass 真实形状的餐饮行（纯归一，不打网络），再走生产入口当场签票。

    `_normalize` 给的是 **OSM 主分类**（`category="amenity"`）；`category=` 参数模拟
    同一家店从别的检索域回来时行上的分类字段（OTM 口径 / 调用方传入的 context 行）。
    """
    tags = {"name": "PLANT BASED", "amenity": "restaurant", "addr:street": "新宿"}
    if diet:
        tags[f"diet:{diet}"] = "yes"
    row = overpass._normalize({"type": "node", "id": node, "lat": 35.6905, "lon": 139.7, "tags": tags})
    assert row is not None
    if category is not None:
        row["category"] = category
    return tools._as_candidate(row, "东京")


def _landed_dining_item(pool_row: dict, *, declared: str = "food") -> dict:
    """把一行参考资料**放进景点域池**落地，返回单日链给交付闸的那个条目形状。"""
    pool = ReferencePool({"candidates": [pool_row]})
    item = {
        "item_type": declared,
        "poi_name": str(pool_row["name"]),
        "start_time": "12:30",
        "end_time": "13:30",
        "cost": 50,
    }
    assert pool.ground(item) is True, "池内命中是前提（否则走的是按名解析那条链）"
    label = label_for_evidence_row(pool_row)
    apply_label(item, label)
    item["fact_evidence"] = field_fact_evidence(
        item,
        label,
        cost_observed=False,
        open_time_observed=False,
        evidence_id=str(pool_row.get("evidence_id") or "") or None,
        city="东京",
    )
    return item


def _diet_verdict(item: dict) -> list[str]:
    plan = _plan()
    plan["items"][1] = item
    return hard_day_issues(plan, _STRICT_DIET)


@pytest.mark.parametrize("category", [None, "attraction", "amenity", "tourism", "food"])
def test_retrieval_category_cannot_strip_the_dining_purpose(category):
    """用途由条目自己决定：饮食行经景点域回来仍是一餐，饮食闸照旧查它（T5b）。"""
    row = _overpass_restaurant(category=category)
    item = _landed_dining_item(row)
    assert item["item_type"] == "food", f"检索类别 {category!r} 把用餐用途改写掉了"
    assert item["item_type"] in {"attraction", "food", "hotel", "transport"}, "item_type 始终在契约值域内"


@pytest.mark.parametrize("category", ["food", "attraction"])
def test_category_alone_cannot_unblock_an_evidence_less_meal(category):
    """只改行上的分类字段，判据一字不差：类型变化不是解除硬约束的通道。"""
    verdicts = _diet_verdict(_landed_dining_item(_overpass_restaurant(category=category)))
    assert any("尚无素食可用性依据" in issue for issue in verdicts)
    assert verdicts == _diet_verdict(_landed_dining_item(_overpass_restaurant(category="attraction")))


def test_model_typing_a_restaurant_as_sightseeing_still_gets_diet_checked():
    """反向同样封住：模型把餐厅写成景点时，落到的实体是用餐实体就升为 food。"""
    item = _landed_dining_item(_overpass_restaurant(), declared="attraction")
    assert item["item_type"] == "food"
    assert any("尚无素食可用性依据" in issue for issue in _diet_verdict(item))


def test_evidenced_vegetarian_meal_from_the_attraction_domain_can_pass():
    """有真实 diet 标签时才放行——证明这条链确实做了饮食检查，不是恒拦或恒放。"""
    row = _overpass_restaurant(diet="vegetarian")
    item = _landed_dining_item(row)
    assert item["item_type"] == "food"
    assert item["fact_evidence"]["diet:vegetarian"].value_kind == "observed"
    assert _diet_verdict(item) == []
    plan = _plan()
    plan["items"][1] = item
    assert hard_delivery_blockers(1, DailyPlan.model_validate(plan), _STRICT_DIET) == []


def test_ticket_does_not_back_a_same_named_entity_in_another_city():
    """票按 name+source+evidence_id 取回后还要与当日城市对得上（T5a 已知缺陷就地补）。"""
    row = {
        "name": "PLANT BASED",
        "city": "东京",
        "source": "overpass",
        "external_id": "osm:node:9",
        "latitude": 35.6905,
        "longitude": 139.7,
        "dietary_options": ["vegetarian"],
        "source_url": "https://www.openstreetmap.org/node/9",
    }
    issue_evidence(row)
    label = label_for_evidence_row(row)
    item = {"poi_name": "PLANT BASED", "item_type": "food", "cost": 50}
    borrowed = field_fact_evidence(
        item, label, cost_observed=False, open_time_observed=False, evidence_id=row["evidence_id"], city="大阪"
    )
    assert borrowed["identity"].verification_status == "unverified"
    assert not any(key.startswith("diet:") for key in borrowed)
    same_city = field_fact_evidence(
        item, label, cost_observed=False, open_time_observed=False, evidence_id=row["evidence_id"], city="东京"
    )
    assert same_city["identity"].verification_status == "verified"
    assert same_city["diet:vegetarian"].value_kind == "observed"


# ---- T5a：排程前定资格（先选对实体，再交给模型排） ----------------------------------


def _food_row(name: str, *, diet: str | None = None, lat: float = 35.6905, lng: float = 139.7) -> dict:
    return {
        "id": name,
        "name": name,
        "city": "东京",
        "source": "overpass",
        "latitude": lat,
        "longitude": lng,
        "dietary_options": [diet] if diet else [],
    }


def test_un_evidenced_dining_rows_are_not_offered_when_one_is_evidenced():
    """饮食行不再只是"排得靠前"：不合格的行根本不进资料块，模型选不到。"""
    rows = [_food_row("无证餐厅"), _food_row("素食食堂", diet="vegetarian")]
    pool = ReferencePool({"foods": rows}, requirements=_STRICT_DIET)
    block = pool.block()
    assert "素食食堂" in block and "无证餐厅" not in block
    assert pool.stats["dining_dropped"] == 1
    assert pool.ground({"item_type": "food", "poi_name": "无证餐厅"}) is False, "被摘除的行不能再生效"


def test_pool_empties_nothing_when_no_row_is_eligible():
    """全部不合格（Overpass 没覆盖到）时原样注入并如实计数，不把"没查到"伪装成"没有餐厅"。"""
    rows = [_food_row("甲"), _food_row("乙")]
    pool = ReferencePool({"foods": rows}, requirements=_STRICT_DIET)
    assert len(pool.references) == 2
    assert pool.stats["dining_unqualified"] == 2 and "dining_dropped" not in pool.stats


def test_review_candidates_policy_keeps_every_dining_row():
    """用户明确选择"先给候选、我自己核实"后，资格筛子按同一个口径松开。"""
    permissive = _STRICT_DIET.model_copy(update={"dining_evidence_policy": "review_candidates"})
    pool = ReferencePool(
        {"foods": [_food_row("无证餐厅"), _food_row("素食食堂", diet="vegetarian")]}, requirements=permissive
    )
    assert len(pool.references) == 2


def test_far_branch_is_not_offered_when_a_nearby_branch_is():
    """当天锚点（已选酒店）已知时，远处分店在排程前就被摘掉——与就近校正同一半径口径。"""
    anchors = [(35.6938, 139.7034)]
    pool = ReferencePool(
        {
            "foods": [
                _food_row("新宿店", diet="vegetarian"),
                _food_row("町田店", diet="vegetarian", lat=35.5427, lng=139.4466),
            ]
        },
        requirements=_STRICT_DIET,
        anchors=anchors,
    )
    block = pool.block()
    assert "新宿店" in block and "町田店" not in block
    assert pool.stats["dining_dropped"] == 1


def test_for_day_takes_anchors_from_the_chosen_hotel_only():
    """单日链两处池构造走同一入口：锚点=当日已选酒店；别的天已成功点位不算今天的参照。"""
    req = GenerateDayRequest(
        city="东京",
        chosen_hotel="新宿华盛顿酒店",
        requirements_struct=_STRICT_DIET,
        context={
            "hotels": [{"name": "新宿华盛顿酒店", "source": "opentripmap", "latitude": 35.6938, "longitude": 139.7034}],
            "foods": [
                _food_row("新宿店", diet="vegetarian"),
                _food_row("町田店", diet="vegetarian", lat=35.5427, lng=139.4466),
            ],
            "used_points": [("别的天景点", "attraction", 35.72, 139.78)],
        },
    )
    pool = ReferencePool.for_day(req)
    assert [str(row["name"]) for row in pool.references] == ["新宿店", "新宿华盛顿酒店"], "远处分店不进资料块"
    # 没有选定酒店就没有当天锚点：距离这一条不判（只按饮食证据筛）
    no_hotel = req.model_copy(update={"chosen_hotel": None})
    names = [str(row["name"]) for row in ReferencePool.for_day(no_hotel).references]
    assert "町田店" in names, "used_points 是别的天已成功点位，不能当今天的参照来筛距离"
