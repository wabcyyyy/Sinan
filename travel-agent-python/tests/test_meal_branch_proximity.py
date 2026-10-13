"""连锁店名解析到远处分店 + 餐次过晚（2026-10-12 真实端到端 e2e2 严格模式第 1 天）。

轨迹：模型只写「くら寿司」，按 (城市, 名字) 解析落到距新宿酒店 19 km 的町田店并借走
它的 diet:vegan 票；转场修正随后按 19 km 把晚餐顺延到 22:19，校验不看餐次时段，
当天仍判 SUCCEEDED。回归锁定：
1. 远处分店换成当天附近的同名分店（限框重查，取最近）；
2. 附近没有同名分店就撤销落地——不借远处分店的证据，也不再驱动转场顺延；
3. 餐饮开始时间越过晚餐时段，是阻断交付的硬伤。
"""

from __future__ import annotations

from typing import Any

from app.agent.generation.content import landing
from app.agent.generation.content.landing import MEAL_BRANCH_MAX_DISTANCE_M, drop_refuted_items, relocate_far_meals
from app.agent.generation.content.reflect import LATE_MEAL_MARK, hard_day_issues, is_late_meal, validate_plans
from app.agent.generation.output.facts import build_lookup, field_fact_evidence, pool_row_for
from app.agent.grounding import existence, nearby_branch
from app.agent.grounding.existence import ResolveResult
from app.agent.grounding.grounding_evidence import evidence_row, issue_evidence
from app.agent.grounding.grounding_labels import FAR_BRANCH_SOURCE, label_for_generated_item, label_for_landed_item
from app.agent.grounding.nearby_branch import resolve_poi_near

SHINJUKU_HOTEL = (35.6938, 139.7034)
SHINJUKU_GYOEN = (35.6852, 139.7100)
MACHIDA_KURA = (35.5427, 139.4466)  # 距新宿约 19 km
SHINJUKU_KURA = (35.6905, 139.7000)


def _day(dinner: dict) -> list[dict]:
    return [
        {
            "item_type": "attraction",
            "poi_name": "新宿御苑",
            "latitude": SHINJUKU_GYOEN[0],
            "longitude": SHINJUKU_GYOEN[1],
            "source": "opentripmap",
            "verification_status": "partially_verified",
        },
        dinner,
        {
            "item_type": "hotel",
            "poi_name": "新宿华盛顿酒店",
            "latitude": SHINJUKU_HOTEL[0],
            "longitude": SHINJUKU_HOTEL[1],
        },
    ]


def _far_kura() -> dict:
    return {
        "item_type": "food",
        "poi_name": "くら寿司",
        "latitude": MACHIDA_KURA[0],
        "longitude": MACHIDA_KURA[1],
        "poi_id": "osm:node:1174641596",
        "address": "町田市",
        "source": "nominatim",
        "start_time": "18:00",
        "end_time": "19:30",
    }


def _kura_row(coords: tuple[float, float], node: int, diets: list[str]) -> dict[str, Any]:
    return {
        "name": "くら寿司",
        "display_name": "くら寿司, 東京都",
        "latitude": coords[0],
        "longitude": coords[1],
        "aliases": ["くら寿司"],
        "localized_name": None,
        "place_id": f"osm:node:{node}",
        "source_url": f"https://www.openstreetmap.org/node/{node}",
        "dietary_options": diets,
        "category_tags": {"amenity": "restaurant"},
    }


# ---- 就近约束 -------------------------------------------------------------------


def test_far_chain_branch_is_relocated_to_nearby_branch(monkeypatch):
    near = ResolveResult(
        state=existence.VERIFIED,
        provider="nominatim",
        name="くら寿司",
        localized_name="藏寿司",
        external_id="osm:node:42",
        latitude=SHINJUKU_KURA[0],
        longitude=SHINJUKU_KURA[1],
        address="新宿区",
    )
    near = existence.issue_resolve_ticket(near, "くら寿司", "东京")
    calls: list[tuple[str, list]] = []

    def _near(name, city, anchors, max_distance_m):
        calls.append((name, anchors))
        assert max_distance_m == MEAL_BRANCH_MAX_DISTANCE_M
        return near

    monkeypatch.setattr(nearby_branch, "resolve_poi_near", _near)
    items = _day(_far_kura())
    relocate_far_meals(items, city="东京")

    dinner = items[1]
    assert (dinner["latitude"], dinner["longitude"]) == SHINJUKU_KURA
    assert dinner["poi_id"] == "osm:node:42"
    assert dinner["address"] == "新宿区"
    assert dinner["source"] == "nominatim"
    assert dinner["poi_name"] == "藏寿司"
    # 锚点是当天的景点与酒店，不含餐饮自身
    assert calls == [("くら寿司", [SHINJUKU_GYOEN, SHINJUKU_HOTEL])]


def test_nearby_lookup_falls_back_to_the_name_the_model_wrote(monkeypatch):
    """local_ground 已把条目改成 OSM 中文名；日本店的 OSM 名是日文，要用票里记下的原写名重查。"""
    far_row = {
        "name": "藏寿司",
        "city": "东京",
        "source": "nominatim",
        "external_id": "osm:node:1174641596",
        "latitude": MACHIDA_KURA[0],
        "longitude": MACHIDA_KURA[1],
        "aliases": ["くら寿司"],
    }
    issue_evidence(far_row)
    queries: list[str] = []

    def _near(name, city, anchors, max_distance_m):
        queries.append(name)
        if name != "くら寿司":
            return ResolveResult.unknown("no_branch_near_day")
        near = ResolveResult(
            state=existence.VERIFIED,
            provider="nominatim",
            name="藏寿司",
            external_id="osm:node:42",
            latitude=SHINJUKU_KURA[0],
            longitude=SHINJUKU_KURA[1],
        )
        return existence.issue_resolve_ticket(near, name, city)

    monkeypatch.setattr(nearby_branch, "resolve_poi_near", _near)
    items = _day({**_far_kura(), "poi_name": "藏寿司", "evidence_id": far_row["evidence_id"]})
    relocate_far_meals(items, city="东京")
    assert queries == ["藏寿司", "くら寿司"]
    assert items[1]["poi_id"] == "osm:node:42"
    assert items[1]["poi_name"] == "藏寿司", "附近分店没有中文名时保留当前写法"


def test_far_branch_without_nearby_branch_is_ungrounded_and_borrows_no_evidence(monkeypatch):
    # 远处分店的票已签（带 diet:vegan）——撤销落地后不能再挂到这条餐饮上
    issue_evidence(
        {
            "name": "くら寿司",
            "city": "东京",
            "source": "nominatim",
            "external_id": "osm:node:1174641596",
            "latitude": MACHIDA_KURA[0],
            "longitude": MACHIDA_KURA[1],
            "dietary_options": ["vegan"],
            "source_url": "https://www.openstreetmap.org/node/1174641596",
        }
    )
    monkeypatch.setattr(nearby_branch, "resolve_poi_near", lambda *a, **k: ResolveResult.unknown("no_branch_near_day"))
    items = _day(_far_kura())
    relocate_far_meals(items, city="东京")

    dinner = items[1]
    assert dinner["latitude"] is None and dinner["longitude"] is None
    assert dinner["poi_id"] is None and dinner["address"] is None
    assert dinner["source"] == FAR_BRANCH_SOURCE
    assert dinner["verification_status"] == "unverified"
    label = label_for_landed_item(dinner, "东京")
    evidence = field_fact_evidence(dinner, label, cost_observed=False, open_time_observed=False)
    assert not any(key.startswith("diet:") for key in evidence)


def _pool_hit_far_kura() -> dict:
    """真实复验轨迹（2026-10-12 trip 30/31）：远处分店来自 Overpass 池，ground 已写标签。"""
    return {
        **_far_kura(),
        "source": "overpass",
        "verification_status": "partially_verified",
        "value_kind": "observed",
    }


def test_far_pool_hit_is_corrected_too(monkeypatch):
    """城市级检索池里混着远处分店，模型照抄池内店名并不知道它在哪——池内命中同样要校正。"""
    monkeypatch.setattr(nearby_branch, "resolve_poi_near", lambda *a, **k: ResolveResult.unknown("no_branch_near_day"))
    items = _day(_pool_hit_far_kura())
    relocate_far_meals(items, city="东京")
    dinner = items[1]
    assert dinner["latitude"] is None and dinner["source"] == FAR_BRANCH_SOURCE
    generated = label_for_generated_item()
    assert (dinner["verification_status"], dinner["value_kind"]) == (
        generated.verification_status,
        generated.value_kind,
    )


def test_relocated_pool_hit_is_relabelled_from_the_nearby_branch_ticket(monkeypatch):
    near = ResolveResult(
        state=existence.VERIFIED,
        provider="nominatim",
        name="くら寿司",
        external_id="osm:node:42",
        latitude=SHINJUKU_KURA[0],
        longitude=SHINJUKU_KURA[1],
    )
    near = existence.issue_resolve_ticket(near, "くら寿司", "东京")
    monkeypatch.setattr(nearby_branch, "resolve_poi_near", lambda *a, **k: near)
    items = _day(_pool_hit_far_kura())
    relocate_far_meals(items, city="东京")
    dinner = items[1]
    assert dinner["poi_id"] == "osm:node:42" and dinner["source"] == "nominatim"
    assert dinner["verification_status"] == "partially_verified"


def test_pool_row_refill_skips_corrected_items():
    """单日链与 assembly 都按名回填候选快照：撤销过的、换过分店的条目不能被同名远处行装回去。"""
    far_row = {"id": "osm:node:1174641596", "name": "くら寿司", "source": "overpass", "dietary_options": ["vegan"]}
    lookup = build_lookup(None, [far_row], None)
    detached = {"poi_name": "くら寿司", "source": FAR_BRANCH_SOURCE, "poi_id": None}
    relocated = {"poi_name": "くら寿司", "source": "nominatim", "poi_id": "osm:node:42"}
    same_entity = {"poi_name": "くら寿司", "source": "overpass", "poi_id": "osm:node:1174641596"}
    unmatched = {"poi_name": "くら寿司", "source": "llm.open_day", "poi_id": None}
    assert pool_row_for(lookup, detached) is None
    assert pool_row_for(lookup, relocated) is None
    assert pool_row_for(lookup, same_entity) == far_row
    assert pool_row_for(lookup, unmatched) == far_row, "未落地的同名项照旧按名回填（池注入截断外的行）"


def test_nearby_meal_and_dayless_anchor_are_left_alone(monkeypatch):
    def _boom(*args, **kwargs):
        raise AssertionError("不该触发就近重查")

    monkeypatch.setattr(nearby_branch, "resolve_poi_near", _boom)
    near_meal = {**_pool_hit_far_kura(), "latitude": SHINJUKU_KURA[0], "longitude": SHINJUKU_KURA[1]}
    items = _day(near_meal)
    relocate_far_meals(items, city="东京")
    assert items[1]["latitude"] == SHINJUKU_KURA[0] and items[1]["source"] == "overpass"

    # 坐标不是服务端落地的（source 不在权威值域）：不归本规则
    unsourced = {**_far_kura(), "source": None}
    items = _day(unsourced)
    relocate_far_meals(items, city="东京")
    assert items[1]["latitude"] == MACHIDA_KURA[0]

    # 当天没有已落地的景点/酒店：没有参照就不猜
    only_meal = [_far_kura()]
    relocate_far_meals(only_meal, city="东京")
    assert only_meal[0]["latitude"] == MACHIDA_KURA[0]


def test_drop_refuted_items_finishes_with_nearby_branch_correction(monkeypatch):
    """两条链路都经 drop_refuted_items 进 fix_transfer_gaps：就近校正挂在它收尾，顺序才有保证。"""
    monkeypatch.setattr(nearby_branch, "resolve_poi_near", lambda *a, **k: ResolveResult.unknown("no_branch_near_day"))
    monkeypatch.setattr(landing, "_resolve", lambda name, city: None)
    kept = drop_refuted_items(_day(_far_kura()), city="东京")
    assert len(kept) == 3
    assert kept[1]["latitude"] is None and kept[1]["source"] == FAR_BRANCH_SOURCE


def test_lookup_failure_falls_back_to_ungrounding(monkeypatch):
    def _broken(*args, **kwargs):
        raise RuntimeError("nominatim down")

    monkeypatch.setattr(nearby_branch, "resolve_poi_near", _broken)
    items = _day(_far_kura())
    relocate_far_meals(items, city="东京")
    assert items[1]["latitude"] is None and items[1]["source"] == FAR_BRANCH_SOURCE


# ---- 限框重查 -------------------------------------------------------------------


def test_resolve_poi_near_picks_nearest_branch_inside_box_and_issues_ticket(monkeypatch):
    seen: dict[str, Any] = {}

    def _geocode(name, city=None, namedetails=False, viewbox=None):
        if viewbox is None:  # pick_row 的目的地闸取城市中心也会走这里
            return []
        seen["viewbox"] = viewbox
        # 框是近似的：故意混入框外的町田店，判据仍按到锚点的实际距离
        return [
            _kura_row(MACHIDA_KURA, 1174641596, ["vegan"]),
            _kura_row((35.7000, 139.7700), 77, []),  # 约 6 km 外（秋叶原方向）
            _kura_row(SHINJUKU_KURA, 42, []),
        ]

    monkeypatch.setattr(existence.places, "geocode_place_rows", _geocode)
    monkeypatch.setattr(existence.settings, "nominatim_enabled", True)

    result = resolve_poi_near("くら寿司", "东京", [SHINJUKU_GYOEN, SHINJUKU_HOTEL], MEAL_BRANCH_MAX_DISTANCE_M)
    assert result.grounded
    assert result.external_id == "osm:node:42"
    assert result.dietary_options == ()
    south, west, north, east = seen["viewbox"]
    assert south < SHINJUKU_GYOEN[0] < north and west < SHINJUKU_HOTEL[1] < east
    assert north - south < 0.2, "框按锚点外扩 5 km，不是整城"
    ticket = evidence_row({"name": "くら寿司", "source": result.provider, "evidence_id": result.evidence_id})
    assert ticket is not None and ticket["dietary_options"] == []


def test_resolve_poi_near_without_branch_in_range_is_unknown(monkeypatch):
    monkeypatch.setattr(
        existence.places,
        "geocode_place_rows",
        lambda name, city=None, namedetails=False, viewbox=None: [_kura_row(MACHIDA_KURA, 1, ["vegan"])],
    )
    monkeypatch.setattr(existence.settings, "nominatim_enabled", True)
    result = resolve_poi_near("くら寿司", "东京", [SHINJUKU_HOTEL], MEAL_BRANCH_MAX_DISTANCE_M)
    assert not result.grounded
    assert result.reason == "no_branch_near_day"


def test_resolve_poi_near_needs_provider_and_anchors(monkeypatch):
    monkeypatch.setattr(existence.settings, "nominatim_enabled", False)
    assert resolve_poi_near("くら寿司", "东京", [SHINJUKU_HOTEL], 5000).reason == "near_lookup_unavailable"
    monkeypatch.setattr(existence.settings, "nominatim_enabled", True)
    assert resolve_poi_near("くら寿司", "东京", [], 5000).reason == "near_lookup_unavailable"
    assert resolve_poi_near("  ", "东京", [SHINJUKU_HOTEL], 5000).reason == "near_lookup_unavailable"


def test_resolve_poi_near_provider_error_is_unknown(monkeypatch):
    def _broken(*args, **kwargs):
        raise RuntimeError("timeout")

    monkeypatch.setattr(existence.places, "geocode_place_rows", _broken)
    monkeypatch.setattr(existence.settings, "nominatim_enabled", True)
    assert resolve_poi_near("くら寿司", "东京", [SHINJUKU_HOTEL], 5000).reason == "near_lookup_error"


def test_resolve_poi_near_respects_existence_budget(monkeypatch):
    monkeypatch.setattr(existence.settings, "nominatim_enabled", True)
    monkeypatch.setattr(existence, "budget_allows", lambda: False)
    assert resolve_poi_near("くら寿司", "东京", [SHINJUKU_HOTEL], 5000).reason == "budget_exhausted"


def test_geocode_viewbox_is_bounded_and_keyed_separately(monkeypatch):
    from app.agent.data import places

    captured: list[tuple[str, dict]] = []

    class _Client:
        def call(self, key, fn):
            captured.append((key, fn()))
            return ["脏项", {"name": "缺坐标"}, {"name": "くら寿司", "lat": "35.69", "lon": "139.70"}]

    monkeypatch.setattr(places.settings, "nominatim_enabled", True)
    monkeypatch.setattr(places, "_nominatim_client", _Client())
    monkeypatch.setattr(places, "fetch_json", lambda client, http, url, params, headers: dict(params))

    rows = places.geocode_place_rows("くら寿司", "东京", namedetails=True)
    assert [row["name"] for row in rows or []] == ["くら寿司"], "非 dict / 缺坐标的应答行被跳过"
    places.geocode_place_rows("くら寿司", "东京", namedetails=True, viewbox=(35.6, 139.6, 35.8, 139.8))
    (plain_key, plain), (box_key, boxed) = captured
    assert "viewbox" not in plain and "bounded" not in plain
    assert boxed["viewbox"] == "139.60000,35.80000,139.80000,35.60000"
    assert boxed["bounded"] == "1"
    assert plain_key != box_key, "限框查询不能命中不限框的缓存"


# ---- 餐次过晚 -------------------------------------------------------------------


def test_is_late_meal_boundaries():
    assert not is_late_meal("21:30")
    assert is_late_meal("21:31")
    assert is_late_meal("22:19")
    assert is_late_meal("00:30")
    assert not is_late_meal("05:00")
    assert not is_late_meal("15:00"), "午后茶点不归本规则"
    assert not is_late_meal(None) and not is_late_meal("") and not is_late_meal("晚上")


def _late_dinner_plan() -> dict:
    return {
        "day_no": 1,
        "items": [
            {"item_type": "attraction", "poi_name": "新宿御苑", "start_time": "09:00", "end_time": "12:00"},
            {"item_type": "food", "poi_name": "午餐", "start_time": "12:10", "end_time": "13:00", "cost": 80},
            {"item_type": "attraction", "poi_name": "东京都厅", "start_time": "13:30", "end_time": "17:30"},
            {"item_type": "food", "poi_name": "くら寿司", "start_time": "22:19", "end_time": "23:49", "cost": 120},
        ],
    }


def test_late_dinner_is_a_hard_delivery_issue():
    issues, _log = validate_plans([_late_dinner_plan()])
    assert any(LATE_MEAL_MARK in issue and "くら寿司(22:19)" in issue for issue in issues)
    assert any(LATE_MEAL_MARK in issue for issue in hard_day_issues(_late_dinner_plan()))

    on_time = _late_dinner_plan()
    on_time["items"][-1].update(start_time="18:00", end_time="19:30")
    assert not any(LATE_MEAL_MARK in issue for issue in hard_day_issues(on_time))
