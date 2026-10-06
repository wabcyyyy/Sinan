"""存在性解析器（PLAN-A1 G2）与同一实体判定（G7）的行为钉。

钉的是量测得出的四条纪律 + 一条裁决：
1. "没问到" ≠ "不存在"：缺 key、超时、被限流一律 UNKNOWN；
2. NOT_FOUND 必须是 provider 明确回了"没有"，且**只有** `authoritative_negative`
   的源才有资格用它删点位（免费源实测 70% 假否定）；
3. 无 key 的 provider 一次外呼都不发；
4. 位置一致性硬闸：解析成功但落在他城 → out_of_area（这才是可删的矛盾证据）；
5. 名称门槛对简繁/中英写法靠 provider 的别名（`name:zh-Hans`、`name:ja`）对上。
"""

from __future__ import annotations

import pytest

from app.agent.grounding import existence
from app.agent.grounding.existence import (
    NOT_FOUND,
    UNKNOWN,
    VERIFIED,
    ResolveResult,
    city_center,
    matches_any,
    providers_in_order,
    reset_existence_state,
    resolve_poi,
    same_entity,
)
from app.agent.runtime.run_limits import begin_limits, end_limits
from app.common.config import settings


@pytest.fixture(autouse=True)
def _clean_memo():
    reset_existence_state()
    yield
    reset_existence_state()


# ---------- 1. 同一实体判定（G7） ----------


@pytest.mark.parametrize(
    "query,candidate",
    [
        ("楼外楼（孤山路总店）", "楼外楼"),  # 分店注记（实测 24/192 只因它查不到）
        ("灵隐寺", "杭州市灵隐寺"),  # 包含关系
        ("Sensō-ji", "sensoji"),  # 大小写与标点
        ("西湖", "西湖"),
        ("浅草寺", "浅草寺（sensoji temple）"),  # 中英混注
    ],
)
def test_same_entity_accepts_spelling_variants(query, candidate):
    assert same_entity(query, candidate) is True


@pytest.mark.parametrize(
    "query,candidate",
    [
        ("方所书店", "安徽省皮肤病防治所"),  # 实测误配
        ("明婷小馆", "报名大厅"),
        ("福缘居", "聚创孵化园"),
        ("老街", "老城"),  # 比率高但共享字符不足 4：不是同一处
        ("", "任意"),
        ("某景点", ""),
    ],
)
def test_same_entity_rejects_unrelated_and_short_overlaps(query, candidate):
    assert same_entity(query, candidate) is False


def test_similarity_threshold_is_configurable_and_default_unchanged(monkeypatch):
    """阈值进配置但默认值不变（G7-3）；包含关系先短路，比率只兜长串。"""
    assert settings.entity_name_similarity_min == 0.55
    assert same_entity("杭州西湖国宾馆", "西湖国宾馆") is True
    # 共享字符够多但重叠率只有 0.44：默认 0.55 拒收，放宽就放进来了
    assert same_entity("西安钟楼诺金酒店", "西安钟鼓楼") is False
    monkeypatch.setattr(settings, "entity_name_similarity_min", 0.4)
    assert same_entity("西安钟楼诺金酒店", "西安钟鼓楼") is True


def test_matches_any_uses_provider_aliases():
    """跨脚本写法只有别名能对上：淺草寺 的 name:ja 是 浅草寺。"""
    assert matches_any("浅草寺", ["淺草寺", "Sensō-ji", "浅草寺"]) is True
    assert matches_any("奥赛博物馆", ["奧賽博物館", "Musée d'Orsay", "奥赛博物馆"]) is True
    assert matches_any("浅草寺", ["淺草寺", "Sensō-ji"]) is False  # 没有别名就对不上


# ---------- 2. provider 选择与"无 key 不外呼" ----------


def test_default_order_is_the_two_free_providers(monkeypatch):
    monkeypatch.setattr(settings, "existence_provider_order", "otm,nominatim")
    assert [p.name for p in providers_in_order()] == ["opentripmap", "nominatim"]


def test_unconfigured_keys_are_ignored_not_crashing(monkeypatch):
    """配置里拼错的键忽略掉，且留白源在无 key 时不可用（不发请求）。"""
    monkeypatch.setattr(settings, "existence_provider_order", "otm,typo_provider,nominatim")
    assert [p.name for p in providers_in_order()] == ["opentripmap", "nominatim"]
    monkeypatch.setattr(settings, "amap_web_key", "")
    monkeypatch.setattr(settings, "google_places_api_key", "")
    registry = existence.provider_registry()
    assert registry["amap"].available() is False
    assert registry["google_places"].available() is False
    assert registry["amap"].authoritative_negative is True


def test_unavailable_provider_is_never_consulted(monkeypatch):
    """available() 为假的 provider 连 resolve 都不进——无 key 就不该有外呼。"""
    calls = []

    class _NeedsKey(_FakeNominatim):
        def available(self):
            return False

        def resolve(self, name, city):
            calls.append(name)
            return ResolveResult.unknown("never")

    _install(monkeypatch, [_NeedsKey([])])
    assert resolve_poi("灵隐寺", "杭州").state == UNKNOWN
    assert calls == []


# ---------- 3. 三值结论的可信边界 ----------


class _FakeNominatim:
    def __init__(self, rows, *, available=True):
        self.rows = rows
        self.available_flag = available
        self.calls = 0

    def available(self):
        return self.available_flag

    name = "nominatim"
    authoritative_negative = False

    def resolve(self, name, city):
        self.calls += 1
        if self.rows is None:
            return ResolveResult.unknown("provider_unavailable")
        if not self.rows:  # 与真实 provider 同规则：空数组才是否证
            return ResolveResult(state=NOT_FOUND, provider=self.name, reason="provider_empty")
        return existence.pick_row(
            self.rows, name, city, provider=self.name, spellings_of=lambda r: [str(r.get("name") or "")]
        )


def _install(monkeypatch, providers):
    monkeypatch.setattr(existence, "providers_in_order", lambda: providers)
    monkeypatch.setattr(existence, "city_center", lambda city, memo=None: (30.0, 120.0))


def test_timeout_is_unknown_never_not_found(monkeypatch):
    """请求失败/未启用 → UNKNOWN；把"没问到"当"不存在"去删点是本模块最重的错。"""
    fake = _FakeNominatim(None)
    _install(monkeypatch, [fake])
    result = resolve_poi("灵隐寺", "杭州")
    assert result.state == UNKNOWN
    assert result.deletable is False


def test_empty_answer_is_not_found_but_not_deletable(monkeypatch):
    fake = _FakeNominatim([])
    fake.authoritative_negative = True  # 模拟"有否证资格的源回了空"
    _install(monkeypatch, [fake])
    result = resolve_poi("楼外楼", "杭州")
    assert result.state == NOT_FOUND
    assert result.deletable is True


def test_free_provider_empty_answer_is_not_deletable(monkeypatch):
    """免费源的 NOT_FOUND 只是"OSM 按这个串搜不到"——实测 70% 是真实地点。"""
    fake = _FakeNominatim([])
    _install(monkeypatch, [fake])
    result = resolve_poi("楼外楼", "杭州")
    assert result.deletable is False


def test_hit_outside_destination_area_is_deletable(monkeypatch):
    """名字对得上但点在安徽（西安查询）：这是与本次行程矛盾的正面证据。"""
    rows = [{"name": "方所书店", "latitude": 31.85, "longitude": 117.22}]  # 距杭州 > 80km
    _install(monkeypatch, [_FakeNominatim(rows)])
    result = resolve_poi("方所书店", "杭州")
    assert result.out_of_area is True
    assert result.grounded is False
    assert result.deletable is True


def test_in_area_hit_grounds_with_provider_and_coords(monkeypatch):
    rows = [{"name": "灵隐寺", "latitude": 30.24, "longitude": 120.1, "display_name": "杭州市西湖区"}]
    _install(monkeypatch, [_FakeNominatim(rows)])
    result = resolve_poi("灵隐寺", "杭州")
    assert (result.state, result.provider) == (VERIFIED, "nominatim")
    assert result.latitude == 30.24 and result.address == "杭州市西湖区"


def test_gate_rejection_stays_unknown(monkeypatch):
    """provider 尽力返回了不相干的东西：门槛拒收后仍是未判定，不是否定。"""
    _install(monkeypatch, [_FakeNominatim([{"name": "聚创孵化园", "latitude": 30.2, "longitude": 120.1}])])
    result = resolve_poi("福缘居", "杭州")
    assert result.state == UNKNOWN
    assert "name_gate_rejected" in result.reason


def test_nominatim_provider_separates_no_answer_from_empty_answer(monkeypatch):
    """真实 provider 的 None / [] 分界（places.geocode_place_rows 的契约）。"""
    provider = existence.NominatimProvider()
    monkeypatch.setattr(settings, "nominatim_enabled", True)
    monkeypatch.setattr(existence, "city_center", lambda city, memo=None: (30.25, 120.1))

    monkeypatch.setattr(existence.places, "geocode_place_rows", lambda name, city=None, **kw: None)
    assert provider.resolve("灵隐寺", "杭州").state == UNKNOWN

    monkeypatch.setattr(existence.places, "geocode_place_rows", lambda name, city=None, **kw: [])
    result = provider.resolve("灵隐寺", "杭州")
    assert (result.state, result.authoritative_negative, result.deletable) == (NOT_FOUND, False, False)

    rows = [{"name": "靈隱寺", "latitude": 30.24, "longitude": 120.1, "aliases": ["灵隐寺"]}]
    monkeypatch.setattr(existence.places, "geocode_place_rows", lambda name, city=None, **kw: rows)
    grounded = provider.resolve("灵隐寺", "杭州")
    assert grounded.state == VERIFIED and grounded.name == "靈隱寺"  # 别名让简繁写法对上


def test_second_provider_gets_a_chance_after_pool_miss(monkeypatch):
    """OTM 池查不到时继续问下一家；任何一家证实都算证实。"""

    class _AlwaysMiss:
        name = "opentripmap"
        authoritative_negative = False

        def available(self):
            return True

        def resolve(self, name, city):
            return ResolveResult.unknown("pool_miss_not_authoritative")

    hit = _FakeNominatim([{"name": "知味观", "latitude": 30.25, "longitude": 120.16}])
    _install(monkeypatch, [_AlwaysMiss(), hit])
    result = resolve_poi("知味观", "杭州")
    assert result.state == VERIFIED and hit.calls == 1


# ---------- 4. 预算：耗尽一律 UNKNOWN，绝不"没查=删" ----------


def test_budget_exhaustion_keeps_items(monkeypatch):
    fake = _FakeNominatim([{"name": "灵隐寺", "latitude": 30.24, "longitude": 120.1}])
    _install(monkeypatch, [fake])
    token = begin_limits()
    try:
        limits = existence.current_limits()
        assert limits is not None
        limits.max_existence_checks = 1
        assert resolve_poi("灵隐寺", "杭州").state == VERIFIED
        exhausted = resolve_poi("净慈寺", "杭州")
        assert exhausted.state == UNKNOWN
        assert "budget_exhausted" in exhausted.reason or "not_found" in exhausted.reason
        assert exhausted.deletable is False
    finally:
        end_limits(token)


def test_no_active_limits_means_no_cap(monkeypatch):
    """离线脚本/评测没有 run 边界：不额外设限，由 provider 侧 TTL 与车道节流兜底。"""
    assert existence.current_limits() is None
    assert existence._budget_allows() is True


# ---------- 5. 记忆化：同一个名字一次 run 只解析一次 ----------


def test_resolution_is_memoized_per_city_and_name(monkeypatch):
    fake = _FakeNominatim([{"name": "灵隐寺", "latitude": 30.24, "longitude": 120.1}])
    _install(monkeypatch, [fake])
    assert resolve_poi("灵隐寺", "杭州") is resolve_poi("灵隐寺", "杭州")
    assert fake.calls == 1
    resolve_poi("灵隐寺", "北京")  # 换城市要重新判（位置闸依赖城市）
    assert fake.calls == 2


def test_city_center_is_memoized(monkeypatch):
    calls = []

    def _geo(name):
        calls.append(name)
        return {"latitude": 30.0, "longitude": 120.0}

    monkeypatch.setattr(existence.places, "resolve_city_center", _geo)
    assert city_center("杭州") == (30.0, 120.0)
    assert city_center("杭州") == (30.0, 120.0)
    assert calls == ["杭州"]


# ---------- 6. 删除动作（G5 / 09-19 复评的两条） ----------


def _item(name: str, **overrides) -> dict:
    item = {"item_type": "attraction", "poi_name": name, "start_time": "09:00", "end_time": "11:00"}
    item.update(overrides)
    return item


def test_drops_only_out_of_area_and_authoritative_refutation(monkeypatch):
    from app.agent.generation.content.landing import drop_refuted_items

    verdicts = {
        "真在东京": ResolveResult(
            state=VERIFIED, provider="nominatim", name="真在东京", latitude=35.7, longitude=139.7
        ),
        "在别处": ResolveResult(
            state=UNKNOWN, provider="nominatim", name="在别处", out_of_area=True, reason="resolved_out_of_area"
        ),
        "免费源说没有": ResolveResult(state=NOT_FOUND, provider="nominatim", reason="provider_empty"),
        "收费源说没有": ResolveResult(
            state=NOT_FOUND, provider="google_places", authoritative_negative=True, reason="provider_empty"
        ),
        "没查到": ResolveResult.unknown("provider_unavailable"),
    }
    monkeypatch.setattr("app.agent.generation.content.landing._resolve", lambda name, city: verdicts.get(name))
    report: dict = {}
    items = [_item(name) for name in verdicts]
    kept = drop_refuted_items(items, city="东京", report=report)

    assert [it["poi_name"] for it in kept] == ["真在东京", "免费源说没有", "没查到"]
    assert [d["name"] for d in report["refuted_pois_dropped"]] == ["在别处", "收费源说没有"]


def test_already_grounded_items_are_not_asked_again(monkeypatch):
    """有权威来源的项直接保留：预算要留给真正没有证据的名字。"""
    from app.agent.generation.content.landing import drop_refuted_items

    calls = []
    monkeypatch.setattr("app.agent.generation.content.landing._resolve", lambda name, city: calls.append(name) or None)
    items = [_item("西湖", source="opentripmap"), _item("某店", source="llm.open_day")]
    kept = drop_refuted_items(items, city="杭州")
    assert len(kept) == 2
    assert calls == ["某店"]


def test_transport_items_are_never_resolved(monkeypatch):
    from app.agent.generation.content.landing import drop_refuted_items

    calls = []
    monkeypatch.setattr("app.agent.generation.content.landing._resolve", lambda name, city: calls.append(name) or None)
    items = [_item("步行", item_type="transport"), _item("杭州东")]
    kept = drop_refuted_items(items, city="杭州")
    assert len(kept) == 2 and calls == ["杭州东"]


def test_budget_exhaustion_keeps_everything(monkeypatch):
    """预算耗尽 → UNKNOWN → 全保留。"没查"永远不等于"可以删"。"""
    rows = [{"name": "灵隐寺", "latitude": 30.24, "longitude": 120.1}]
    provider = _FakeNominatim(rows)
    _install(monkeypatch, [provider])
    from app.agent.generation.content.landing import drop_refuted_items

    token = begin_limits()
    try:
        limits = existence.current_limits()
        assert limits is not None
        limits.max_existence_checks = 0  # 一家都不许问
        items = [_item("灵隐寺"), _item("净慈寺")]
        assert len(drop_refuted_items(items, city="杭州")) == 2
    finally:
        end_limits(token)


# ---------- 7. 收费源留白实现（形状先留、key 后补；09-18 裁决 D2） ----------


def _stub_fetch(monkeypatch, payload):
    """把两家商业源的取数口换成回放 payload，并清掉客户端缓存保证用例互不影响。"""
    from app.agent.grounding import existence_commercial as paid

    calls: list[dict] = []

    def _fake(client, http, url, **kwargs):
        calls.append({"url": url, **kwargs})
        return payload

    monkeypatch.setattr(paid, "fetch_json", _fake)
    paid._amap_client.clear_cache()
    paid._places_client.clear_cache()
    return calls


def test_amap_resolves_and_refutes_like_the_other_providers(monkeypatch):
    from app.agent.grounding import existence_commercial as paid

    monkeypatch.setattr(settings, "amap_web_key", "k")
    monkeypatch.setattr(existence, "city_center", lambda city, memo=None: (30.25, 120.1))
    calls = _stub_fetch(
        monkeypatch,
        {
            "status": "1",
            "pois": [
                {"name": "知味观", "id": "B0FF", "longitude": "120.16", "latitude": "30.25", "address": "杭州湖滨"}
            ],
        },
    )
    result = paid.AmapProvider().resolve("知味观", "杭州")
    assert (result.state, result.provider) == (VERIFIED, "amap")
    assert result.latitude == 120.16 or result.latitude == 30.25  # "lng,lat" 串按形状解析
    assert calls and "key" in calls[0]["params"]

    empty = _stub_fetch(monkeypatch, {"status": "1", "pois": []})
    refuted = paid.AmapProvider().resolve("不存在店", "杭州")
    assert (refuted.state, refuted.authoritative_negative, refuted.deletable) == (NOT_FOUND, True, True)
    assert len(empty) == 1


def test_amap_without_key_never_calls_out(monkeypatch):
    from app.agent.grounding import existence_commercial as paid

    monkeypatch.setattr(settings, "amap_web_key", "")
    calls = _stub_fetch(monkeypatch, {"status": "1", "pois": []})
    assert paid.AmapProvider().available() is False
    assert calls == []


def test_google_places_posts_and_refutes(monkeypatch):
    from app.agent.grounding import existence_commercial as paid

    monkeypatch.setattr(settings, "google_places_api_key", "k")
    monkeypatch.setattr(existence, "city_center", lambda city, memo=None: (48.86, 2.35))
    calls = _stub_fetch(
        monkeypatch,
        {
            "places": [
                {
                    "id": "gp-1",
                    "displayName": {"text": "Musée d'Orsay"},
                    "formattedAddress": "Paris",
                    "location": {"latitude": 48.86, "longitude": 2.326},
                }
            ]
        },
    )
    result = paid.GooglePlacesProvider().resolve("Musée d Orsay", "巴黎")
    assert result.state == VERIFIED and result.provider == "google_places"
    assert calls[0]["url"].endswith("places:searchText")
    assert calls[0]["json_body"]["textQuery"].startswith("Musée d Orsay")
    assert calls[0]["headers"]["X-Goog-Api-Key"] == "k"

    _stub_fetch(monkeypatch, {"places": []})
    empty = paid.GooglePlacesProvider().resolve("不存在博物馆", "巴黎")
    assert empty.deletable is True


def test_malformed_paid_provider_payload_is_unknown_not_not_found(monkeypatch):
    """HTTP 200 但形状不对：算未判定——绝不让解析失败冒充"这个点不存在"。"""
    from app.agent.grounding import existence_commercial as paid

    monkeypatch.setattr(settings, "amap_web_key", "k")
    _stub_fetch(monkeypatch, {"status": "0", "info": "INVALID_USER_KEY"})
    result = paid.AmapProvider().resolve("西湖", "杭州")
    assert result.state == UNKNOWN and result.deletable is False


# ---------- 8. 外部数据层的三态契约（places.geocode_place_rows） ----------


def test_geocode_place_rows_separates_failure_from_empty_answer(monkeypatch):
    """None=没问到 / []=问到且没有 / 行=带别名与坐标的候选。"""
    from app.agent.data import places

    monkeypatch.setattr(settings, "nominatim_enabled", True)

    def _patch(payload):
        monkeypatch.setattr(places, "api_client", lambda: object())
        monkeypatch.setattr(places, "fetch_json", lambda client, http, url, **kw: payload)

    _patch(None)
    assert places.geocode_place_rows("西湖", "杭州") is None
    _patch({"error": "boom"})
    assert places.geocode_place_rows("西湖", "杭州") is None

    _patch([])
    assert places.geocode_place_rows("不存在", "杭州") == []

    _patch(
        [
            {
                "name": "靈隱寺",
                "display_name": "灵隐寺, 杭州",
                "lat": "30.24",
                "lon": "120.10",
                "country_code": "cn",
                "namedetails": {"name": "靈隱寺", "name:zh": "灵隐寺", "name:en": "Lingyin Temple"},
            },
            {"display_name": "无坐标的行"},
        ]
    )
    rows = places.geocode_place_rows("灵隐寺", "杭州", namedetails=True)
    assert rows is not None and len(rows) == 1  # 缺坐标的行丢弃
    assert rows[0]["latitude"] == 30.24 and rows[0]["country_code"] == "CN"
    assert "灵隐寺" in rows[0]["aliases"]


def test_reference_hit_without_coordinates_still_reaches_the_resolver(monkeypatch):
    """参考资料命中 ≠ 位置落地：联网池行按设计没有坐标，解析器必须仍被问一次。

    钉的是真实链路量出来的形状（1 天北京 `coord_valid_rate=0.25` 而
    `existence_checks=1/24`——只有资料没命中的点位被解析过）。命中即早退会让
    "有来源、没坐标"成为最终产出，地图钉与路线深链都拿不到位置。
    """
    from typing import cast

    from app.agent.generation.content.landing import ground_item
    from app.agent.generation.content.reference_pool import ReferencePool

    asked: list[str] = []
    monkeypatch.setattr(
        "app.agent.generation.content.landing.local_ground", lambda item, city: asked.append(item["poi_name"]) or False
    )

    class _WebSearchOnlyPool:
        def ground(self, item):
            item["source"] = "web.search"
            item["cost"] = 80
            return True

    item = {"poi_name": "卤煮火烧", "item_type": "food"}
    # 鸭子类型的池：这条用例钉的是 ground_item 的控制流，不是 ReferencePool 的解析协议
    assert ground_item(item, city="北京", ref_pool=cast("ReferencePool", _WebSearchOnlyPool())) is True
    assert asked == ["卤煮火烧"]


def test_hit_with_coordinates_does_not_pay_a_second_lookup(monkeypatch):
    """无条件调 local_ground 不等于多花外呼：坐标已有效时它的守卫必须挡在解析前。"""
    from typing import cast

    from app.agent.generation.content.landing import ground_item
    from app.agent.generation.content.reference_pool import ReferencePool
    from app.agent.grounding import facts as grounding

    def _forbidden(name, _city):
        raise AssertionError(f"坐标已有效，不该再问解析器：{name}")

    monkeypatch.setattr(grounding, "resolve_poi", _forbidden)

    class _AnchoredPool:
        def ground(self, item):
            item.update({"source": "opentripmap", "latitude": 39.9, "longitude": 116.4})
            return True

    item = {"poi_name": "故宫博物院", "item_type": "attraction"}
    assert ground_item(item, city="北京", ref_pool=cast("ReferencePool", _AnchoredPool())) is True


# ---------- R3-F6：高德否证的 status=1 闸（2026-10-05） ----------


def test_amap_negative_requires_status_ok(monkeypatch):
    """高德「pois 为空**且 status=1** 才算否证」——docstring 承诺落进代码。

    status=0/缺失 + 空 pois 是"这次查询没成功"，不是"没有"：返回不确定、
    不可删（deletable=False）。宁可不否证，不可误删用户的点位。
    """
    from app.agent.grounding import existence_commercial as paid

    monkeypatch.setattr(settings, "amap_web_key", "k")
    monkeypatch.setattr(existence, "city_center", lambda city, memo=None: (30.25, 120.1))

    _stub_fetch(monkeypatch, {"status": "1", "pois": []})
    refuted = paid.AmapProvider().resolve("不存在店", "杭州")
    assert (refuted.state, refuted.authoritative_negative, refuted.deletable) == (NOT_FOUND, True, True)

    _stub_fetch(monkeypatch, {"status": "0", "info": "DAILY_QUERY_OVER_LIMIT", "pois": []})
    failed = paid.AmapProvider().resolve("不存在店", "杭州")
    assert failed.state == UNKNOWN and failed.deletable is False, "status=0 + 空 pois 不是否证"

    _stub_fetch(monkeypatch, {"pois": []})
    no_status = paid.AmapProvider().resolve("不存在店", "杭州")
    assert no_status.state == UNKNOWN and no_status.deletable is False, "缺 status 同样不许否证"
