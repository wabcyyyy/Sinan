"""上线前 P1 修复回归（2026-10-11 内容 P1 轮）：外部证据覆盖、类别误伤、跨天重复。

三条真实失败轨迹的结构化回归（证据与根因见 docs/public/release-content-p1-fix-2026-10-11.md）：

1. **P1-A 饮食证据覆盖**：严格模式此前结构性不可能完成——研究池（OTM/联网）没有
   饮食标签面、描述性店名全串查询必空、池内已证实条目被位置前缀挡在精确匹配外。
   回归锁定：Overpass diet 池行进候选与证据票；同实体模糊落地；核心名重查。
2. **P1-C 排除类别误伤**："明治神宫外苑银杏并木"（公园）此前被"神宫"名称词表
   判成违例并触发修复连锁失败。回归锁定：外部类别证据双向判定（证伪名称猜测 +
   证实无词法命中的礼拜场所），无证据时保守判据原样保留。
3. **P1-B 跨天重复**："都厅展望室" vs "都厅南展望室"两侧都无坐标时名称通道抓不
   住；单日重试此前只登记名字不登记坐标。回归锁定：used_points 坐标通道。
"""

from __future__ import annotations

from typing import Any

from app.agent.data import overpass, places
from app.agent.generation.content.landing import drop_used_items
from app.agent.generation.content.reference_pool import ReferencePool
from app.agent.generation.output.facts import field_fact_evidence
from app.agent.generation.rules.constraint_checks import check_constraints
from app.agent.generation.rules.day_policy import day_policy_for
from app.agent.grounding.existence import NominatimProvider, resolve_poi
from app.agent.grounding.grounding_evidence import lookup_ticket
from app.agent.grounding.grounding_labels import is_trusted_row, label_for_landed_item
from app.agent.tools.impl import _as_candidate, search_foods
from app.schemas.trip_requirements import TripRequirements

# ---- P1-C：排除类别消费外部类别证据 ------------------------------------------


def _item(name: str, item_type: str = "attraction", evidence: dict | None = None) -> dict:
    item: dict[str, Any] = {"item_type": item_type, "poi_name": name, "start_time": "10:00", "end_time": "11:00"}
    if evidence is not None:
        item["fact_evidence"] = evidence
    return item


def _category_evidence(**tags: str) -> dict:
    return {
        "identity": {
            "verification_status": "verified",
            "value_kind": "observed",
            "source_url": "https://www.openstreetmap.org/way/1",
            "provider": "nominatim",
        },
        **{
            f"category:{key}={value}": {
                "verification_status": "verified",
                "value_kind": "observed",
                "source_url": "https://www.openstreetmap.org/way/1",
                "provider": "nominatim",
            }
            for key, value in tags.items()
        },
    }


REQ_TEMPLE = TripRequirements(excluded_categories=["temple"])
REQ_MUSEUM = TripRequirements(excluded_categories=["museum"])
_POLICY = day_policy_for(REQ_TEMPLE, 1)


def _excluded_report(items: list[dict], requirements: TripRequirements):
    return check_constraints({"items": items}, requirements, 1, day_policy_for(requirements, 1))


def test_park_with_leisure_evidence_is_not_flagged_as_temple():
    """名称含"神宫"但 OSM 证据是 leisure 园区：不再判违例（id=14 失败链的直接根因）。"""
    items = [_item("明治神宫外苑银杏并木", evidence=_category_evidence(amenity="park", leisure="pitch"))]
    report = _excluded_report(items, REQ_TEMPLE)
    check = next(c for c in report.checks if c.kind == "excluded_category")
    assert check.status == "pass"
    assert check.evidence_refs == ["osm-category"]


def test_worship_place_with_romanized_name_is_flagged_by_evidence():
    """英文名没带 temple 字样，但 OSM 标了神道礼拜场所：证据方向判违例。"""
    items = [_item("Meiji Jingu", evidence=_category_evidence(amenity="place_of_worship", religion="shinto"))]
    report = _excluded_report(items, REQ_TEMPLE)
    check = next(c for c in report.checks if c.kind == "excluded_category")
    assert check.status == "violation"
    assert check.item_refs == ["Meiji Jingu"]


def test_name_hit_without_evidence_stays_violation():
    """无类别证据时保守名称判据原样保留：门禁不因新路径放宽。"""
    items = [_item("明治神宫外苑银杏并木")]
    report = _excluded_report(items, REQ_TEMPLE)
    check = next(c for c in report.checks if c.kind == "excluded_category")
    assert check.status == "violation"
    assert check.evidence_refs == ["name-substring"]


def test_confirmed_temple_with_matching_name_stays_violation():
    """浅草寺：名称命中且证据确认为佛教礼拜场所 → 违例保持。"""
    items = [_item("浅草寺", evidence=_category_evidence(amenity="place_of_worship", religion="buddhist"))]
    check = next(c for c in _excluded_report(items, REQ_TEMPLE).checks if c.kind == "excluded_category")
    assert check.status == "violation"


def test_church_is_outside_temple_semantics():
    """教堂（christian）不在"寺庙/神社"语义内：不判违例。"""
    items = [_item("圣保罗教堂", evidence=_category_evidence(amenity="place_of_worship", religion="christian"))]
    check = next(c for c in _excluded_report(items, REQ_TEMPLE).checks if c.kind == "excluded_category")
    assert check.status == "pass"


def test_museum_detected_by_evidence_without_name_token():
    """tourism=museum 证据命中，英文名不带"博物馆"字样 → 违例。"""
    items = [_item("Tokyo National Museum Annex", evidence=_category_evidence(tourism="museum"))]
    check = next(c for c in _excluded_report(items, REQ_MUSEUM).checks if c.kind == "excluded_category")
    assert check.status == "violation"


def test_name_museum_hit_cleared_by_non_museum_evidence():
    """名称带"博物馆"但证据是商店（文创店取名"博物馆"）：证据推翻名称猜测。"""
    items = [_item("博物馆纪念品商店", item_type="food", evidence=_category_evidence(shop="gift"))]
    check = next(c for c in _excluded_report(items, REQ_MUSEUM).checks if c.kind == "excluded_category")
    assert check.status == "pass"


# ---- P1-A：类别证据与饮食标签进证据票与字段证据 ------------------------------


def test_osm_facts_extracts_category_tags():
    facts = places._osm_facts(
        {
            "osm_type": "way",
            "osm_id": "123",
            "category": "amenity",
            "type": "place_of_worship",
            "extratags": {"religion": "shinto", "diet:vegetarian": "yes", "wikidata": "Q1"},
        }
    )
    assert facts["category_tags"] == {"amenity": "place_of_worship", "religion": "shinto"}
    assert facts["dietary_options"] == ["vegetarian"]
    assert facts["source_url"] == "https://www.openstreetmap.org/way/123"


def test_ticket_carries_category_tags_and_field_evidence_emits_keys():
    _as_candidate(
        {
            "name": "明治神宮外苑",
            "latitude": 35.677,
            "longitude": 139.717,
            "source": "nominatim",
            "id": "osm:way:1",
            "source_url": "https://www.openstreetmap.org/way/1",
            "dietary_options": [],
            "category_tags": {"amenity": "park", "leisure": "pitch"},
        },
        "东京",
    )
    ticket = lookup_ticket("明治神宮外苑", external_id="osm:way:1")
    assert ticket is not None
    assert ("amenity", "park") in ticket.category_tags
    item = _item("明治神宮外苑")
    item["poi_id"] = "osm:way:1"
    item["latitude"], item["longitude"] = 35.677, 139.717
    item["source"] = "nominatim"  # 生产口径：local_ground 判定成功时写入服务端来源
    evidence = field_fact_evidence(
        item, label_for_landed_item(item, "东京"), cost_observed=False, open_time_observed=False
    )
    assert evidence["category:amenity=park"].value_kind == "observed"
    assert (evidence["category:leisure=pitch"].source_url or "").endswith("/way/1")
    assert "diet:vegetarian" not in evidence
    item["fact_evidence"] = evidence
    check = next(c for c in _excluded_report([item], REQ_TEMPLE).checks if c.kind == "excluded_category")
    assert check.status == "pass"


def test_overpass_diet_rows_normalize_into_candidate_rows(monkeypatch):
    from app.agent.tools import impl as tools

    monkeypatch.setattr(tools.overpass, "search_diet_tagged_places", lambda city, diet, limit=8: _diet_rows())

    monkeypatch.setattr(tools, "_otm_pool", lambda city, category, limit: [])
    monkeypatch.setattr(tools, "_web_pool", lambda city, category, limit: [])
    rows = search_foods("东京", limit=10, dietary="vegetarian")
    assert rows and rows[0]["name"] == "PLANT BASED"
    assert rows[0]["dietary_options"] == ["vegan", "vegetarian"]
    assert rows[0]["source"] == "overpass"
    assert rows[0]["id"] == "osm:node:9"
    assert rows[0]["place_id"] == "osm:node:9"


def test_overpass_pool_rows_are_ticketed_on_pool_entry(monkeypatch):
    """生产口径缺口：diet 池行必须当场签票。

    落地阶段只认证据票不认行上的 source 字符串；没签票的行 is_trusted_row 为假，
    poi_id 不会被 graft，随后按名重解析可能命中同一连锁店的另一个 OSM 元素
    （实测 2026-10-12：diet 标签在 node，重解析命中 way），饮食证据在源头断供。
    离线 mock 给所有 fixture 行统一签发，恰好掩盖过这个缺口。
    """
    from app.agent.tools import impl as tools

    monkeypatch.setattr(tools.overpass, "search_diet_tagged_places", lambda city, diet, limit=8: _diet_rows())
    monkeypatch.setattr(tools, "_otm_pool", lambda city, category, limit: [])
    monkeypatch.setattr(tools, "_web_pool", lambda city, category, limit: [])

    rows = search_foods("东京", limit=10, dietary="vegetarian")
    assert rows and is_trusted_row(rows[0]) is True

    ticket = lookup_ticket("PLANT BASED", "东京", external_id="osm:node:9")
    assert ticket is not None
    assert ticket.provider == "overpass"
    assert ticket.dietary_options == ("vegan", "vegetarian")
    assert (ticket.source_url or "").endswith("/node/9")


def _diet_rows() -> list[dict]:
    return [
        {
            "name": "PLANT BASED",
            "latitude": 35.69,
            "longitude": 139.70,
            "source": "overpass",
            "id": "osm:node:9",
            "source_url": "https://www.openstreetmap.org/node/9",
            "place_id": "osm:node:9",
            "dietary_options": ["vegan", "vegetarian"],
            "category_tags": {"amenity": "restaurant"},
        }
    ]


def test_search_foods_without_dietary_keeps_legacy_pools(monkeypatch):
    """不带饮食要求时不取 Overpass 池：非素食行程的候选面与旧版一致。"""
    called = {}
    from app.agent.tools import impl as tools

    monkeypatch.setattr(tools, "_otm_pool", lambda city, category, limit: [{"name": "普通店", "source": "opentripmap"}])
    monkeypatch.setattr(tools, "_web_pool", lambda city, category, limit: [])

    def _spy(city, diet, limit=8):
        called["hit"] = True
        return []

    monkeypatch.setattr(tools.overpass, "search_diet_tagged_places", _spy)
    rows = search_foods("东京", limit=10)
    assert [row["name"] for row in rows] == ["普通店"]
    assert "hit" not in called


# ---- P1-A：ReferencePool 同实体模糊落地 -------------------------------------


def test_poi_detail_ticket_carries_resolved_diet_and_category(monkeypatch):
    """点名解析路径同样要签出 diet/类别证据：与 Overpass 池同源的另一处断供。

    `get_poi_detail` 先前只搬 name/坐标/地址/source，把 `hit` 上已由 `_osm_facts`
    填好的证据字段丢掉，于是当场签的票没有饮食依据——严格模式在"按名解析"这条
    路上依旧无法交付。
    """
    from app.agent.data import places as places_data
    from app.agent.tools import impl as tools

    monkeypatch.setattr(
        places_data,
        "geocode_place",
        lambda name, city=None: {
            "name": "AFURI",
            "latitude": 35.67,
            "longitude": 139.71,
            "display_name": "AFURI, 港区, 東京都",
            "source": "nominatim",
            "dietary_options": ["vegetarian"],
            "category_tags": {"amenity": "restaurant"},
            "source_url": "https://www.openstreetmap.org/node/42",
            "place_id": "osm:node:42",
        },
    )

    row = tools.get_poi_detail("东京", "AFURI")
    assert row is not None
    ticket = lookup_ticket("AFURI", "东京", external_id=str(row["id"]))
    assert ticket is not None
    assert ticket.dietary_options == ("vegetarian",)
    assert ticket.category_tags == (("amenity", "restaurant"),)
    assert (ticket.source_url or "").endswith("/node/42")


def test_reference_pool_lands_prefixed_name_via_same_entity():
    """模型在已证实店名前加位置前缀：不再错过池行（此前"新宿御苑前 ソラノイロ"落空）。"""
    row = _as_candidate(
        {
            "name": "ソラノイロ",
            "latitude": 35.683,
            "longitude": 139.739,
            "source": "overpass",
            "id": "osm:node:5",
            "source_url": "https://www.openstreetmap.org/node/5",
            "dietary_options": ["vegetarian"],
            "category_tags": {"amenity": "restaurant"},
        },
        "东京",
    )
    pool = ReferencePool({"foods": [row]})
    item = {"item_type": "food", "poi_name": "新宿御苑前 ソラノイロ"}
    assert pool.ground(item) is True
    assert item["poi_name"] == "ソラノイロ"
    assert item["poi_id"] == "osm:node:5"
    assert item["latitude"] == 35.683
    evidence = field_fact_evidence(
        item, label_for_landed_item(item, "东京"), cost_observed=False, open_time_observed=False
    )
    assert evidence["diet:vegetarian"].value_kind == "observed"


def test_reference_pool_does_not_anchor_branch_name_to_brand_row():
    """分店名不因模糊匹配继承品牌池行（沿用分店背书纪律）。"""
    row = _as_candidate(
        {
            "name": "Tully's Coffee",
            "latitude": 35.69,
            "longitude": 139.70,
            "source": "overpass",
            "id": "osm:node:6",
            "source_url": "https://www.openstreetmap.org/node/6",
            "dietary_options": ["vegetarian"],
        },
        "东京",
    )
    pool = ReferencePool({"foods": [row]})
    item = {"item_type": "food", "poi_name": "Tully's Coffee 新宿中央公园店"}
    assert pool.ground(item) is False
    assert item["poi_name"] == "Tully's Coffee 新宿中央公园店"


# ---- P1-A：核心名重查（NominatimProvider） ----------------------------------


class _FakeLLM:
    def __init__(self, core: str) -> None:
        self.core = core

    def complete(self, *args: Any, **kwargs: Any) -> str:
        return f'{{"core": "{self.core}"}}'


def test_nominatim_retries_with_extracted_core_name(monkeypatch):
    """全串查询空 → fast 模型抽核心名 → 重查命中同一实体（"新宿御苑前 ソラノイロ"轨迹）。"""
    from app.agent.grounding import core_name, existence

    queries: list[str] = []

    def _geocode(name, city=None, namedetails=False):
        queries.append(name)
        if name == "ソラノイロ":
            return [
                {
                    "name": "Soranoiro",
                    "display_name": "Soranoiro, 千代田区, 東京都",
                    "latitude": 35.683,
                    "longitude": 139.739,
                    "aliases": ["ソラノイロ"],
                    "localized_name": None,
                    "category": "amenity",
                    "type": "restaurant",
                    "place_id": "osm:node:7",
                    "source_url": "https://www.openstreetmap.org/node/7",
                    "dietary_options": ["vegetarian"],
                    "category_tags": {"amenity": "restaurant"},
                }
            ]
        return []

    monkeypatch.setattr(existence.places, "geocode_place_rows", _geocode)
    monkeypatch.setattr(core_name, "configured", lambda role: True)
    monkeypatch.setattr(core_name, "get_role_client", lambda role: _FakeLLM("ソラノイロ"))
    monkeypatch.setattr(core_name, "model_for", lambda role: "fake-model")
    monkeypatch.setattr(existence.settings, "nominatim_enabled", True)

    result = NominatimProvider().resolve("新宿御苑前 ソラノイロ", "东京")
    assert result.grounded
    assert result.external_id == "osm:node:7"
    assert result.dietary_options == ("vegetarian",)
    assert queries[:2] == ["新宿御苑前 ソラノイロ", "ソラノイロ"]


def test_nominatim_name_gate_rejection_still_retries_core_name(monkeypatch):
    """全串有返回但过不了名称门槛（捞到别的实体）：仍应抽核心名重查一次，捞回同一实体。"""
    from app.agent.grounding import core_name, existence

    def _geocode(name, city=None, namedetails=False):
        if name == "ソラノイロ":
            return [
                {
                    "name": "Soranoiro",
                    "display_name": "Soranoiro, 千代田区, 東京都",
                    "latitude": 35.683,
                    "longitude": 139.739,
                    "aliases": ["ソラノイロ"],
                    "localized_name": None,
                    "category": "amenity",
                    "type": "restaurant",
                    "place_id": "osm:node:7",
                    "source_url": "https://www.openstreetmap.org/node/7",
                    "dietary_options": ["vegetarian"],
                    "category_tags": {"amenity": "restaurant"},
                }
            ]
        return [_other_row()]

    monkeypatch.setattr(existence.places, "geocode_place_rows", _geocode)
    monkeypatch.setattr(core_name, "configured", lambda role: True)
    monkeypatch.setattr(core_name, "get_role_client", lambda role: _FakeLLM("ソラノイロ"))
    monkeypatch.setattr(core_name, "model_for", lambda role: "fake-model")
    monkeypatch.setattr(existence.settings, "nominatim_enabled", True)

    result = NominatimProvider().resolve("新宿御苑前 ソラノイロ", "东京")
    assert result.grounded, result.reason
    assert result.external_id == "osm:node:7"
    assert result.dietary_options == ("vegetarian",)


def test_nominatim_core_retry_without_usable_core_keeps_rejection(monkeypatch):
    """抽不出核心名（模型返回空）：维持名称门槛的原判定，不为无效重试多付一次 1rps 查询。"""
    from app.agent.grounding import core_name, existence

    calls: list[str] = []
    monkeypatch.setattr(
        existence.places,
        "geocode_place_rows",
        lambda name, city, namedetails=False: calls.append(name) or [_other_row()],
    )
    monkeypatch.setattr(core_name, "configured", lambda role: True)
    monkeypatch.setattr(core_name, "get_role_client", lambda role: _FakeLLM(""))
    monkeypatch.setattr(core_name, "model_for", lambda role: "fake-model")
    monkeypatch.setattr(existence.settings, "nominatim_enabled", True)

    result = NominatimProvider().resolve("新宿御苑前 ソラノイロ", "东京")
    assert not result.grounded
    assert calls == ["新宿御苑前 ソラノイロ"]


def test_nominatim_core_retry_empty_second_pass_keeps_rejection(monkeypatch):
    """核心名重查仍空：结论不得因为换了查询写法就变成背书。"""
    from app.agent.grounding import core_name, existence

    def _geocode(name, city=None, namedetails=False):
        return [] if name == "ソラノイロ" else [_other_row()]

    monkeypatch.setattr(existence.places, "geocode_place_rows", _geocode)
    monkeypatch.setattr(core_name, "configured", lambda role: True)
    monkeypatch.setattr(core_name, "get_role_client", lambda role: _FakeLLM("ソラノイロ"))
    monkeypatch.setattr(core_name, "model_for", lambda role: "fake-model")
    monkeypatch.setattr(existence.settings, "nominatim_enabled", True)

    result = NominatimProvider().resolve("新宿御苑前 ソラノイロ", "东京")
    assert not result.grounded


def _other_row() -> dict[str, Any]:
    """与查询名不同实体的 Nominatim 行：过不了名称门槛，用于验证拒绝路径。"""
    return {
        "name": "東京タワー",
        "display_name": "Tokyo Tower, 港区, 東京都",
        "latitude": 35.658,
        "longitude": 139.745,
        "aliases": [],
        "localized_name": None,
        "category": "tourism",
        "type": "attraction",
        "place_id": "osm:node:99",
        "source_url": "https://www.openstreetmap.org/node/99",
    }


def test_nominatim_empty_without_fast_model_stays_not_found(monkeypatch):
    """未配置 fast 模型（离线 eval/CI）：行为与旧版一致，空结果仍 NOT_FOUND。"""
    from app.agent.grounding import core_name, existence

    monkeypatch.setattr(existence.places, "geocode_place_rows", lambda name, city, namedetails=False: [])
    monkeypatch.setattr(core_name, "configured", lambda role: False)
    monkeypatch.setattr(existence.settings, "nominatim_enabled", True)
    result = NominatimProvider().resolve("不存在的店", "东京")
    assert result.state == existence.NOT_FOUND
    assert result.reason == "provider_empty"


def test_resolve_poi_issues_ticket_with_category_tags(monkeypatch):
    """判定成功当场签发的票带类别证据：P1-C 检查器的数据源。"""
    from app.agent.grounding import existence

    def _geocode(name, city=None, namedetails=False):
        return [
            {
                "name": "明治神宮外苑",
                "display_name": "明治神宮外苑, 東京都",
                "latitude": 35.677,
                "longitude": 139.717,
                "aliases": ["明治神宫外苑"],
                "localized_name": "明治神宫外苑",
                "place_id": "osm:way:2",
                "source_url": "https://www.openstreetmap.org/way/2",
                "dietary_options": [],
                "category_tags": {"leisure": "sports_centre"},
            }
        ]

    monkeypatch.setattr(existence.places, "geocode_place_rows", _geocode)
    monkeypatch.setattr(existence.settings, "nominatim_enabled", True)
    monkeypatch.setattr(existence.settings, "existence_provider_order", "nominatim")

    result = resolve_poi("明治神宫外苑银杏并木", "东京")
    assert result.grounded
    assert result.category_tags == (("leisure", "sports_centre"),)
    ticket = lookup_ticket("明治神宫外苑", external_id="osm:way:2")
    assert ticket is not None
    assert ("leisure", "sports_centre") in ticket.category_tags


# ---- P1-B：跨天坐标通道 -----------------------------------------------------


def test_drop_used_items_catches_variant_by_registered_coords():
    """前日"都厅展望室"带坐标登记，当日名称变体落地同坐标 → 判重删除。"""
    items = [
        {"item_type": "attraction", "poi_name": "东京都厅南展望室", "latitude": 35.6896, "longitude": 139.6925},
    ]
    kept = drop_used_items(
        items,
        ["东京都厅展望室"],
        day_no=2,
        used_points=[("东京都厅展望室", "attraction", 35.6896, 139.6925)],
    )
    assert kept == []


def test_drop_used_items_name_channel_unchanged_without_points():
    """不带 used_points 时行为与旧版一致：同名判重照常，不同名不误删。"""
    items = [{"item_type": "attraction", "poi_name": "明治神宫外苑", "latitude": 35.677, "longitude": 139.717}]
    kept = drop_used_items(items, ["明治神宫外苑"], day_no=2)
    assert kept == []
    fresh = [{"item_type": "attraction", "poi_name": "明治神宫外苑", "latitude": 35.677, "longitude": 139.717}]
    kept2 = drop_used_items(fresh, ["东京都厅展望室"], day_no=2)
    assert kept2 == fresh


def test_drop_used_items_hotel_points_do_not_block_attraction():
    """酒店坐标不参与判重（登记表豁免语义）：同址酒店/景点各自成立。"""
    items = [{"item_type": "attraction", "poi_name": "京王广场酒店大楼", "latitude": 35.6836, "longitude": 139.7104}]
    kept = drop_used_items(
        items,
        ["京王广场酒店"],
        day_no=2,
        used_points=[("京王广场酒店", "hotel", 35.6836, 139.7104)],
    )
    assert kept == items


# ---- 研究贯通：ResearchTask → search_foods ----------------------------------


def test_food_research_params_carry_dietary():
    from app.agent.research.domains import DOMAINS
    from app.agent.research.evidence import ResearchTask

    task = ResearchTask(domain="food", city="东京", dietary="vegetarian")
    params = DOMAINS["food"].params(task)
    assert params["dietary"] == "vegetarian"
    assert params["city"] == "东京"


def test_supervisor_projects_dietary_into_tasks():
    from app.agent.research import supervisor
    from app.schemas.trip import GenerateRequest
    from app.schemas.trip_requirements import TripRequirements

    request = GenerateRequest(
        city="东京",
        days=2,
        persons=2,
        budget=3000,
        requirements_struct=TripRequirements(dietary_requirement="vegetarian"),
    )
    tasks = supervisor.decompose(request)
    food_task = next(task for task in tasks if task.domain == "food")
    assert food_task.dietary == "vegetarian"
    # 只有 food 域参数消费 dietary；attraction/hotel 的检索参数不带它
    from app.agent.research.domains import DOMAINS

    for domain in ("attraction", "hotel"):
        other_task = next(task for task in tasks if task.domain == domain)
        assert "dietary" not in DOMAINS[domain].params(other_task)


def test_overpass_disabled_returns_empty(monkeypatch):
    monkeypatch.setattr(overpass.settings, "overpass_enabled", False)
    assert overpass.search_diet_tagged_places("东京", "vegetarian") == []


def test_overpass_row_normalization(monkeypatch):
    """Overpass payload → 证据行：diet 标签/类别/坐标/OSM 链接齐备，无名行跳过。"""
    from app.agent.data import city_center as city_center_module

    payload = {
        "elements": [
            {
                "type": "node",
                "id": 42,
                "lat": 35.69,
                "lon": 139.70,
                "tags": {"name": "AFURI", "amenity": "restaurant", "diet:vegetarian": "yes"},
            },
            {"type": "way", "id": 43, "center": {"lat": 35.691, "lon": 139.701}, "tags": {"amenity": "cafe"}},
        ]
    }
    monkeypatch.setattr(overpass.settings, "overpass_enabled", True)
    monkeypatch.setattr(city_center_module, "city_center", lambda city: {"latitude": 35.68, "longitude": 139.73})
    monkeypatch.setattr(overpass._client, "call", lambda key, loader, lane=None: payload)
    rows = overpass.search_diet_tagged_places("东京", "vegetarian", limit=5)
    assert len(rows) == 1
    row = rows[0]
    assert row["name"] == "AFURI"
    assert row["dietary_options"] == ["vegetarian"]
    assert row["category_tags"]["amenity"] == "restaurant"
    assert row["id"] == "osm:node:42"
    assert row["source_url"] == "https://www.openstreetmap.org/node/42"


# ---- 判据边界：无证据/未知类别不冒充结论 ------------------------------------


def test_museum_exclusion_with_non_museum_evidence_passes():
    """排除博物馆但外部类别是普通观光点：证据判不了违例也不冒充 pass 依据。"""
    items = [_item("teamLab Borderless", evidence=_category_evidence(amenity="arts_centre"))]
    report = _excluded_report(items, REQ_MUSEUM)
    check = next(c for c in report.checks if c.kind == "excluded_category")
    assert check.status == "pass"
    assert check.item_refs == []


def test_reference_pool_match_without_name_is_no_match():
    """行程项没有名字：不进入同实体扫描，避免把空串匹配到任意池行。"""
    row = _as_candidate(
        {"name": "ソラノイロ", "latitude": 35.683, "longitude": 139.739, "source": "overpass", "id": "osm:node:5"},
        "东京",
    )
    pool = ReferencePool({"foods": [row]})
    assert pool.match({"item_type": "food", "poi_name": ""}) is None


def test_reference_block_announces_evidenced_diet():
    """池内已证实饮食标签随参考资料注入模型：严格模式有据可依的那一条通道。"""
    row = _as_candidate(
        {
            "name": "PLANT BASED",
            "latitude": 35.69,
            "longitude": 139.70,
            "source": "overpass",
            "id": "osm:node:9",
            "dietary_options": ["vegan", "vegetarian"],
        },
        "东京",
    )
    block = ReferencePool({"foods": [row]}).block()
    assert "外部饮食标签已证实:纯素、素食" in block


def test_issue_evidence_needs_name_and_provider():
    """无名或无 provider 的行不签票：不能给无法追溯的产出背书。"""
    from app.agent.grounding.grounding_evidence import issue_evidence

    assert issue_evidence({"source": "overpass", "id": "osm:node:1"}) is None
    assert issue_evidence({"name": "AFURI", "id": "osm:node:1"}) is None
