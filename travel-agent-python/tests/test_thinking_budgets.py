"""生成改走全局 low 思考后的输出预算与后处理回归（2026-10-12）。

思考 token 计入 completion（max_tokens）：小额度调用会被思考吃满、正文为空
（finish_reason=length）。本文件验证调用预算及后处理行为，不衡量真实模型质量。
同批修的判重误删：同一检索里两家不同餐厅相距 <80m 时，晚餐被当成重复删掉且不补。
"""

from __future__ import annotations

from typing import Any

from app.agent.core.poi_identity import PoiSeenRegistry
from app.agent.generation.content.landing import drop_used_items
from app.agent.generation.content.reference_pool import ReferencePool
from app.agent.grounding import core_name


class _SpyLLM:
    def __init__(self) -> None:
        self.kwargs: dict[str, Any] = {}

    def complete(self, *args: Any, **kwargs: Any) -> str:
        self.kwargs = kwargs
        return '{"core": "ソラノイロ"}'


def test_core_name_budget_leaves_room_for_thinking(monkeypatch):
    """原 60 档实测 4/4 被思考吃满、正文为空；抽取须给出容纳思考的额度。"""
    spy = _SpyLLM()
    core_name.reset_core_name_cache()
    monkeypatch.setattr(core_name, "configured", lambda role: True)
    monkeypatch.setattr(core_name, "get_llm_client", lambda role: spy)
    assert core_name.core_name_via_llm("新宿御苑前 ソラノイロ", "东京") == "ソラノイロ"
    assert spy.kwargs["max_tokens"] >= 1024
    core_name.reset_core_name_cache()


# 东京实测：FOOD HALL BLAST（OSM way）与 PLANT BASED（OSM node）相距约 5 米
_BLAST = (35.69062, 139.70043)
_PLANT = (35.69066, 139.70045)


def _pool() -> ReferencePool:
    rows: list[dict[str, Any]] = [
        {"id": "osm:node:12060629549", "name": "PLANT BASED", "source": "overpass"},
        {"id": "osm:way:232497186", "name": "FOOD HALL BLAST", "source": "overpass"},
    ]
    for row, (lat, lng) in zip(rows, (_PLANT, _BLAST), strict=True):
        row.update(latitude=lat, longitude=lng)
    return ReferencePool({"foods": rows})


def _meal(name: str, poi_id: str | None, point: tuple[float, float]) -> dict:
    return {"poi_name": name, "item_type": "food", "poi_id": poi_id, "latitude": point[0], "longitude": point[1]}


def test_distinct_pool_restaurants_in_same_building_are_both_kept():
    pool = _pool()
    items = [
        _meal("PLANT BASED", "osm:node:12060629549", _PLANT),
        _meal("FOOD HALL BLAST", "osm:way:232497186", _BLAST),
    ]
    kept = drop_used_items(items, [], day_no=2, ref_pool=pool)
    assert [i["poi_name"] for i in kept] == ["PLANT BASED", "FOOD HALL BLAST"]


def test_coordinate_channel_still_merges_food_variants_without_distinct_pool_identity():
    """一侧没有池内身份（模型改写的名字经地理编码落到旁边）：仍按距离判重。"""
    pool = _pool()
    items = [
        _meal("PLANT BASED", "osm:node:12060629549", _PLANT),
        _meal("植物基餐厅 新宿", "nominatim:1", _BLAST),
    ]
    kept = drop_used_items(items, [], day_no=1, ref_pool=pool)
    assert [i["poi_name"] for i in kept] == ["PLANT BASED"]
    # 不传池（旧调用口径）时行为不变
    both = [_meal("PLANT BASED", "osm:node:12060629549", _PLANT), _meal("FOOD HALL BLAST", "osm:way:232497186", _BLAST)]
    assert len(drop_used_items(both, [], day_no=1)) == 1


def test_pool_identity_exemption_is_food_only_and_same_entity_still_duplicate():
    seen = PoiSeenRegistry()
    seen.register("东京都厅第一本厅舍", "attraction", *_PLANT, entity="otm:1")
    assert seen.is_duplicate("Tokyo City Hall Tower II", "attraction", *_BLAST, entity="otm:2"), "景点同楼合并语义不变"
    seen.register("PLANT BASED", "food", *_PLANT, entity="osm:node:1")
    assert seen.is_duplicate("PLANT BASED 新宿", "food", *_BLAST, entity="osm:node:1"), "同一池行仍是重复"
    assert not seen.is_duplicate("FOOD HALL BLAST", "food", *_BLAST, entity="osm:way:2")


def test_intent_distill_budget_leaves_room_for_thinking(monkeypatch):
    """原 300 档实测 3/3 被思考吃满、正文为空，意图摘要静默丢失。"""
    from app.agent.core import intent as intent_mod

    spy = _SpyLLM()
    monkeypatch.setattr(intent_mod, "get_llm_client", lambda role: spy)
    intent_mod.distill_intent("东京两天慢节奏，不去寺庙")
    assert spy.kwargs["max_tokens"] >= 2048
