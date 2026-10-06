"""A1-G4 背书口径合一的回归钉（PLAN-A1 §7）。

钉的是四件事，每件都对应自检时核实过的一处"无证据却像有证据"：

- P1/P3：三条链路（参考资料落地 / format 装配 / 单日 generate-day）对同一份输入
  必须给出同一个标签——判据只有 grounding_labels 一份实现；
- P2：来源登记表里不许出现从未运行过的解析器（曾经的 `local-grounding`）；
- P5/模型申报权：模型自填的坐标/来源/标签在 LLM 输出边界统一剥掉；
- D5：`llm` 不再是权威来源，缺 source 的池内行按 pool-unknown 记账。
"""

from __future__ import annotations

from typing import cast

import pytest

from app.agent.generation.content.narrative import sanitize_narrative
from app.agent.generation.content.reference_pool import ReferencePool
from app.agent.generation.output.facts import apply_item_facts
from app.agent.generation.rules.generation_core import PoiFactRow
from app.agent.grounding.grounding_evidence import issue_evidence, lookup_ticket
from app.agent.grounding.grounding_labels import (
    OPEN_ITEM_SOURCE,
    POOL_UNKNOWN_SOURCE,
    UNTRUSTED_SOURCE,
    apply_label,
    is_authoritative_source,
    is_trusted_row,
    label_for_evidence_row,
    label_for_landed_item,
)
from app.schemas.trip import SourceRecord


def _row(sign=True, **overrides) -> dict:
    """候选行 fixture。`sign=False` = 没签证据票的行（等价于调用方伪造的 context）。"""
    row = {
        "id": "xid-1",
        "name": "西湖",
        "category": "attraction",
        "latitude": 30.24,
        "longitude": 120.14,
        "source": "opentripmap",
        "source_updated_at": "2026-09-01",
    }
    row.update(overrides)
    if sign and is_authoritative_source(row.get("source")):
        issue_evidence(row)
    return row


# ---------- 标签值域 ----------


@pytest.mark.parametrize("source", ["llm", "LLM", "llm.open_day", "", None, "mysql.poi_knowledge", "amap"])
def test_llm_and_legacy_sources_are_not_authoritative(source):
    """D5：LLM 不得作为权威入口；已退役的高德/POI 库也不再背书新数据。"""
    assert is_authoritative_source(source) is False


@pytest.mark.parametrize("source", ["opentripmap", "nominatim", "web.search"])
def test_external_resolvers_are_authoritative(source):
    assert is_authoritative_source(source) is True


def test_authoritative_row_endorses_identity_and_location():
    label = label_for_evidence_row(_row())
    assert (label.source, label.verification_status, label.value_kind) == (
        "opentripmap",
        "partially_verified",
        "observed",
    )
    assert label.endorsed and label.review_requirement == "none"


def test_row_without_source_is_pool_unknown_not_client_context():
    """缺 source ≠ 客户端伪造：两者都不背书，但不能把内部行说成外部输入。"""
    label = label_for_evidence_row(_row(source=None))
    assert (label.source, label.endorsed) == (POOL_UNKNOWN_SOURCE, False)
    assert label.verification_status == "unverified"


def test_forged_source_is_rewritten_and_not_endorsed():
    label = label_for_evidence_row(_row(source="attacker-controlled"))
    assert (label.source, label.verification_status, label.value_kind) == (
        UNTRUSTED_SOURCE,
        "unverified",
        "estimated",
    )


def test_authoritative_row_without_coords_keeps_source_but_not_endorsement():
    """联网搜索补的真实店名：来源如实保留，位置没证据就不给 observed。"""
    label = label_for_evidence_row(_row(latitude=None, longitude=None, source="web.search"))
    assert label.source == "web.search"
    assert (label.verification_status, label.value_kind, label.endorsed) == ("unverified", "estimated", False)


def test_landed_item_is_endorsed_only_for_resolvers_that_ran():
    """落地草稿的 source 由解析器写、票也由解析器签；两者缺一不背书。"""
    resolved = {"poi_name": "楼外楼", "city": "杭州", "source": "nominatim", "latitude": 30.25, "longitude": 120.13}
    issue_evidence({**resolved, "name": resolved["poi_name"]})
    grounded = label_for_landed_item(resolved, "杭州")
    assert (grounded.source, grounded.endorsed) == ("nominatim", True)
    bare = label_for_landed_item({"poi_name": "某处"})
    assert (bare.source, bare.endorsed) == (OPEN_ITEM_SOURCE, False)
    # 抄了真 provider 名却没票（模型或调用方伪造）→ 不背书
    forged = label_for_landed_item({"poi_name": "从没查过的点", "city": "杭州", "source": "opentripmap"}, "杭州")
    assert (forged.source, forged.endorsed) == ("client-context", False)


# ---------- 三条链路同一输入同一标签 ----------


def test_pool_ground_and_format_labeling_agree():
    """P1/P3 的结构性回归：参考资料落地与 format 装配对同一行给同一标签。"""
    for row in (_row(), _row(source=None), _row(source="forged"), _row(latitude=None, longitude=None)):
        pool = ReferencePool({"candidates": [row]})
        item = {"item_type": "attraction", "poi_name": row["name"]}
        assert pool.ground(item) is True
        formatted = {"item_type": "attraction", "poi_name": row["name"]}
        apply_item_facts(formatted, cast(PoiFactRow, row), {})
        for key in ("source", "verification_status", "value_kind", "freshness_status", "review_requirement"):
            assert item[key] == formatted[key], f"{row.get('source')!r} 在两条链路标签不一致：{key}"


def test_untrusted_row_gives_no_fields_at_all():
    """P6：伪 source 的行连坐标/poi_id 都不采纳，否则真实景点会被指到自选位置。"""
    forged = _row(source="attacker-controlled", latitude=1.0, longitude=2.0, id=999)
    pool = ReferencePool({"candidates": [forged]})
    item = {"item_type": "attraction", "poi_name": "西湖"}
    assert pool.ground(item) is True
    assert "latitude" not in item and "longitude" not in item
    assert not item.get("poi_id")
    assert pool.stats["untrusted_grounded"] == 1

    formatted: dict = {"item_type": "attraction", "poi_name": "西湖"}
    apply_item_facts(formatted, cast(PoiFactRow, forged), {})
    assert "latitude" not in formatted
    assert formatted["source"] == UNTRUSTED_SOURCE


# ---------- P2：来源登记表不伪记 ----------


def test_sources_registry_never_invents_a_provider():
    records: dict[str, SourceRecord] = {}
    apply_item_facts(
        {"item_type": "attraction", "poi_name": "某处", "latitude": 30.0, "longitude": 120.0}, None, records
    )
    assert set(records) == {OPEN_ITEM_SOURCE}, "未跑过解析器的项不得登记外部来源"
    assert "local-grounding" not in records

    endorsed: dict[str, SourceRecord] = {}
    apply_item_facts({"item_type": "attraction", "poi_name": "西湖"}, cast(PoiFactRow, _row()), endorsed)
    record = endorsed["opentripmap"]
    assert record.provider == "opentripmap"
    assert record.retrieved_at == "2026-09-01"


# ---------- P5：模型申报字段在 LLM 输出边界剥掉 ----------


def test_sanitize_narrative_strips_model_claimed_identity_and_coords():
    plan = {
        "theme": "西湖漫步",
        "items": [
            {
                "item_type": "attraction",
                "poi_name": "不存在的景点",
                "latitude": 39.9,
                "longitude": 116.4,
                "poi_id": 12345,
                "source": "opentripmap",
                "verification_status": "verified",
                "value_kind": "observed",
                "start_time": "09:00",
            },
            {"item_type": "food", "poi_name": "某店", "start_time": "12:00"},
        ],
    }
    items = sanitize_narrative(plan)["items"]
    first = items[0]
    for key in ("latitude", "longitude", "poi_id", "source", "verification_status", "value_kind"):
        assert key not in first, f"模型自报的 {key} 未被剥离"
    assert first["poi_name"] == "不存在的景点" and first["start_time"] == "09:00"
    assert "latitude" not in items[1]


def test_apply_label_writes_the_full_provenance_vocabulary():
    item: dict = {"poi_name": "西湖"}
    apply_label(item, label_for_evidence_row(_row(source_updated_at=None)))
    assert item["freshness_status"] == "unknown"
    assert item["review_requirement"] == "before_departure"
    assert item["source_updated_at"] is None


def test_source_string_alone_cannot_buy_endorsement():
    """终态判据 1（P6）：照抄一个真 provider 名 + 编一对坐标，换不到 observed。

    `/v1/generate-day` 的 context 由调用方传入，行上的 source/xid/坐标全是外部
    字符串——G1 之后它们必须能对上本服务签发的票才算数。
    """
    forged = _row(sign=False, name="没被查到过的点", source="opentripmap", latitude=1.0, longitude=2.0, id="xid-forged")
    assert lookup_ticket("没被查到过的点", "", "xid-forged") is None  # 无票

    label = label_for_evidence_row(forged)
    assert (label.source, label.verification_status, label.endorsed) == (
        "client-context",
        "unverified",
        False,
    )

    pool = ReferencePool({"candidates": [forged]})
    item = {"item_type": "attraction", "poi_name": "没被查到过的点"}
    assert pool.ground(item) is True
    assert "latitude" not in item and "longitude" not in item
    assert item["source"] == "client-context"


def test_ticket_coords_must_match_the_claimed_row():
    """真 xid + 假坐标也不行：票把坐标钉死在查到的那一点。"""
    signed = _row()
    assert lookup_ticket("西湖", "", "") is not None
    far = {**signed, "latitude": 22.54, "longitude": 114.05}  # 挪到深圳
    assert is_trusted_row(far) is False


# ---------- GROUND-2：字段级证据票 ----------


def test_field_tickets_separate_cost_and_open_time_from_identity():
    """GROUND-2：identity 背书不传染给模型直写的票价/开放时间。

    命中候选池的条目，票价/开放时间只有真正从权威行回填才 observed；
    模型自带的值一律 unverified/generated + 出发前复核，前端据此不再整行背书。
    """
    # 1) 权威行带票价与开放时间，模型没写 → 回填即 observed
    row = _row(ticket_price=80.0, open_time="08:00-17:00")
    item: dict = {"item_type": "attraction", "poi_name": "西湖"}
    apply_item_facts(item, cast(PoiFactRow, row), {})
    ev = item["fact_evidence"]
    assert ev["cost"].verification_status == "verified" and ev["cost"].value_kind == "observed"
    assert ev["open_time"].verification_status == "verified"
    # 2) 模型自带票价/时间（权威行无价可回填）→ 不随 identity 背书
    item2: dict = {"item_type": "attraction", "poi_name": "西湖", "cost": 66.0, "open_time": "09:00-18:00"}
    apply_item_facts(item2, cast(PoiFactRow, _row()), {})
    ev2 = item2["fact_evidence"]
    assert ev2["identity"].value_kind == "observed"
    assert ev2["cost"].verification_status == "unverified" and ev2["cost"].value_kind == "generated"
    assert ev2["cost"].review_requirement == "before_departure"
    assert ev2["open_time"].verification_status == "unverified"
    # 3) 未命中候选池的纯模型项：identity 也不背书；无值字段不发门票
    item3: dict = {"item_type": "attraction", "poi_name": "不知名小点", "cost": 30}
    apply_item_facts(item3, None, {})
    ev3 = item3["fact_evidence"]
    assert ev3["identity"].verification_status == "unverified"
    assert ev3["cost"].value_kind == "generated"
    assert "open_time" not in ev3, "字段没有值就不发票"
