"""输出落地阶段的权威事实回填与溯源标注。

知识库永远只是补充证据：本次候选快照里命中的条目才回填坐标/票价/营业时间并给出
质量背书；未命中的模型自选地点必须如实标为生成值、出发前复核。

背书判定本身不在这里（也不该在这里）：三条链路共用
`grounding_labels.label_for_*`，本模块只负责"回填哪些字段 + 登记来源记录"。

边界形状（G-1.6）：权威行（只读）用 generation_core.PoiFactRow TypedDict
推断已知键类型；item 是**每步可缺键的可变草稿**，开放 dict[str, Any] 才是
它的诚实类型（TypedDict(total=False) 的读写严格度与这条链不匹配）。
"""

from __future__ import annotations

from typing import Any, cast

from app.agent.generation.rules.generation_core import PoiFactRow
from app.agent.generation.rules.transfer_time import parse_time
from app.agent.grounding.facts import has_coord
from app.agent.grounding.grounding_labels import (
    ItemLabel,
    apply_label,
    has_valid_coords,
    is_trusted_row,
    label_for_evidence_row,
    label_for_landed_item,
    source_record_for,
)
from app.schemas.trip import FactEvidence, SourceRecord


def field_fact_evidence(
    item: dict[str, Any],
    label: ItemLabel,
    *,
    cost_observed: bool,
    open_time_observed: bool,
) -> dict[str, FactEvidence]:
    """字段级证据票（GROUND-2）：identity/cost/open_time 各自独立记票。

    此前整条链只发 identity 一张票（或行级 partially_verified），开放模式命中
    候选池的条目其票价/开放时间即使全部来自模型世界知识，也随行级徽章被读成
    「有据」。现在 cost/open_time 只有真正从权威候选行回填才是 observed；模型
    直写一律 unverified/generated + 出发前复核。字段没有值就不发票。
    """
    identity = FactEvidence(
        source_ref=label.source,
        provider=label.source,
        retrieved_at=label.source_updated_at,
        verification_status="verified" if (label.endorsed and item.get("poi_id")) else "unverified",
        value_kind="observed" if label.endorsed else "generated",
        freshness_status=label.freshness_status,
        review_requirement=label.review_requirement,
    )

    def _ticket(observed: bool) -> FactEvidence:
        if observed:
            return FactEvidence(
                source_ref=label.source,
                provider=label.source,
                retrieved_at=label.source_updated_at,
                verification_status="verified",
                value_kind="observed",
                freshness_status=label.freshness_status,
                review_requirement=label.review_requirement,
            )
        return FactEvidence(
            verification_status="unverified",
            value_kind="generated",
            freshness_status="unknown",
            review_requirement="before_departure",
        )

    evidence: dict[str, FactEvidence] = {"identity": identity}
    if item.get("cost") is not None:
        evidence["cost"] = _ticket(cost_observed)
    if item.get("open_time"):
        evidence["open_time"] = _ticket(open_time_observed)
    return evidence


def build_lookup(
    candidates: list[dict[str, Any]] | None, foods: list[dict[str, Any]] | None, hotels: list[dict[str, Any]] | None
) -> dict[str, PoiFactRow]:
    """名字 → 本次候选快照的权威事实行。

    酒店同样属于权威事实源：若遗漏，格式化阶段会把已知城市的酒店误判为开放模式
    LLM 生成，导致来源和质量状态失真。
    """
    lookup: dict[str, PoiFactRow] = {}
    for poi in (candidates or []) + (foods or []) + (hotels or []):
        name = poi.get("name")
        if name and name not in lookup:
            lookup[name] = cast(PoiFactRow, poi)
    return lookup


def apply_item_facts(
    item: dict[str, Any],
    poi: PoiFactRow | None,
    source_records: dict[str, SourceRecord],
    *,
    city: str = "",
) -> None:
    """按候选快照回填权威字段，并就地质标溯源与质量状态。"""
    label = label_for_evidence_row(poi) if poi else label_for_landed_item(item, city)
    # 来源不可信的行一个字段都不采纳：伪 source 不只让徽章失真，还能把真实景点
    # 指到调用方自选的坐标与 poi_id 上（P6）。
    cost_observed = False
    open_time_observed = False
    if poi and is_trusted_row(poi):
        # 0/0 是缺失坐标的哨兵值（store._row_payload 会把 NULL 写成 0.0），
        # 不能作为权威坐标回填，否则幻觉坐标获得权威背书。
        poi_lat = poi.get("latitude")
        poi_lng = poi.get("longitude")
        if (
            not has_coord(item.get("latitude"))
            and has_valid_coords(poi)
            and poi_lat is not None
            and poi_lng is not None
        ):
            item["latitude"] = float(poi_lat)
            item["longitude"] = float(poi_lng)
        if not item.get("poi_id"):
            item["poi_id"] = str(poi.get("id") or "")
        price = poi.get("ticket_price")
        if not item.get("cost") and price is not None:
            item["cost"] = float(price)
            cost_observed = True
        if item.get("open_time") is None:
            item["open_time"] = poi.get("open_time")
            open_time_observed = bool(item["open_time"])
    apply_label(item, label)
    # GROUND-2：字段级证据票就地构建（identity/cost/open_time 独立记票），
    # 模型直写的票价/时间不再随行级标签获得「有据」背书
    item["fact_evidence"] = field_fact_evidence(
        item, label, cost_observed=cost_observed, open_time_observed=open_time_observed
    )
    source_records.setdefault(label.source, source_record_for(label, poi))


def sync_duration_from_time_window(item: dict[str, Any]) -> None:
    """时间窗优先：库内典型时长可能与已排 start/end 冲突（西湖 480 vs 150）。"""
    start = parse_time(item.get("start_time"))
    end = parse_time(item.get("end_time"))
    if start is not None and end is not None and end > start:
        item["duration_min"] = end - start
