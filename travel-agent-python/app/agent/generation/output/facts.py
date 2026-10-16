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

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from app.agent.generation.rules.generation_core import PoiFactRow
from app.agent.generation.rules.transfer_time import parse_time
from app.agent.grounding.grounding_evidence import evidence_row
from app.agent.grounding.grounding_labels import (
    FAR_BRANCH_SOURCE,
    ItemLabel,
    apply_label,
    has_valid_coords,
    is_authoritative_source,
    is_trusted_row,
    label_for_evidence_row,
    label_for_landed_item,
    source_record_for,
)
from app.schemas.trip import FactEvidence, SourceRecord


def _field_ticket(label: ItemLabel, snapshot: Mapping[str, Any] | None, observed: bool) -> FactEvidence:
    if observed and snapshot:
        return FactEvidence(
            source_ref=str(snapshot["evidence_id"]),
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


def field_fact_evidence(
    item: dict[str, Any],
    label: ItemLabel,
    *,
    cost_observed: bool,
    open_time_observed: bool,
    evidence_id: str | None = None,
    city: str | None = None,
) -> dict[str, FactEvidence]:
    """字段级证据票（GROUND-2）：identity/cost/open_time 各自独立记票。

    此前整条链只发 identity 一张票（或行级 partially_verified），开放模式命中
    候选池的条目其票价/开放时间即使全部来自模型世界知识，也随行级徽章被读成
    「有据」。现在 cost/open_time 只有真正从权威候选行回填才是 observed；模型
    直写一律 unverified/generated + 出发前复核。字段没有值就不发票。

    `city` 给出时票必须与它对得上（两边都有值且不同才判不符，口径与
    `grounding_labels.label_for_landed_item` 一致）：连锁品牌在不同城市同名同
    source，只按 name+source+evidence_id 取票会让一座城市的证据替另一座城市的
    同名实体说话（T5a 已知缺陷，这里就地补齐，不改 schema）。
    """
    snapshot = (
        evidence_row({"name": str(item.get("poi_name") or ""), "source": label.source, "evidence_id": evidence_id})
        if is_authoritative_source(label.source)
        else None
    )
    if snapshot is not None and city and snapshot.get("city") and str(snapshot["city"]) != str(city):
        snapshot = None
    # 引用本次取回的字段快照，不能只凭同名或 provider 字符串认领证据。
    source_ref = str(snapshot["evidence_id"]) if snapshot else None
    identity_verified = snapshot is not None and is_authoritative_source(label.source)
    identity = FactEvidence(
        source_ref=source_ref,
        provider=label.source,
        retrieved_at=label.source_updated_at,
        verification_status="verified" if identity_verified else "unverified",
        value_kind="observed" if identity_verified else "generated",
        freshness_status=label.freshness_status,
        review_requirement=label.review_requirement,
    )

    evidence: dict[str, FactEvidence] = {"identity": identity}
    if snapshot:
        identity.source_url = snapshot.get("source_url")
        evidence.update(_external_tag_evidence(snapshot))
    if has_valid_coords(item):
        evidence["coordinates"] = _field_ticket(
            label,
            snapshot,
            label.endorsed
            and snapshot is not None
            and item.get("latitude") == snapshot.get("latitude")
            and item.get("longitude") == snapshot.get("longitude"),
        )
    if item.get("cost") is not None:
        evidence["cost"] = _field_ticket(
            label, snapshot, cost_observed and snapshot is not None and item.get("cost") == snapshot.get("ticket_price")
        )
    if item.get("open_time"):
        evidence["open_time"] = _field_ticket(
            label,
            snapshot,
            open_time_observed and snapshot is not None and item.get("open_time") == snapshot.get("open_time"),
        )
    return evidence


def _external_tag_evidence(snapshot: Mapping[str, Any]) -> dict[str, FactEvidence]:
    """证据票的 diet/category 标签 → 独立字段票（observed）。

    只把外部实际取回的标签原样透传，键形如 "diet:vegetarian" /
    "category:amenity=place_of_worship"；是否构成排除类别违例由确定性检查器判，
    这里不做语义结论。
    """

    def _tagged() -> FactEvidence:
        return FactEvidence(
            source_ref=str(snapshot["evidence_id"]),
            source_url=snapshot.get("source_url"),
            provider=snapshot.get("source"),
            verification_status="verified",
            value_kind="observed",
            freshness_status="unknown",
            review_requirement="before_departure",
        )

    evidence = {
        f"diet:{diet}": _tagged() for diet in (snapshot.get("dietary_options") or ()) if diet in ("vegetarian", "vegan")
    }
    evidence.update(
        {f"category:{key}={value}": _tagged() for key, value in (snapshot.get("category_tags") or {}).items()}
    )
    return evidence


_Row = TypeVar("_Row", bound=Mapping[str, Any])


def pool_row_for(lookup: Mapping[str, _Row], item: dict[str, Any]) -> _Row | None:
    """按名取本次候选快照行，用于回填坐标/价格与标签；与条目落地实体对不上时不取。

    两种情况跳过：就近校正撤销过落地的远处分店（FAR_BRANCH_SOURCE）——按名回填会
    把远处分店的坐标与饮食证据原样装回来；条目已落到另一个实体（poi_id 与同名行
    不同，如换成了附近分店）——同名行是别的分店，它的标签与票不属于这一条。
    """
    if item.get("source") == FAR_BRANCH_SOURCE:
        return None
    row = lookup.get(str(item.get("poi_name") or ""))
    if row is None:
        return None
    item_id, row_id = str(item.get("poi_id") or ""), str(row.get("id") or "")
    if item_id and row_id and item_id != row_id:
        return None
    return row


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
    if poi and is_trusted_row(poi):
        poi = cast(PoiFactRow, evidence_row(poi))
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
        if has_valid_coords(poi) and poi_lat is not None and poi_lng is not None:
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
        item,
        label,
        cost_observed=cost_observed,
        open_time_observed=open_time_observed,
        evidence_id=str((poi or item).get("evidence_id") or "") or None,
        city=city or None,
    )
    source_records.setdefault(label.source, source_record_for(label, poi))


def sync_duration_from_time_window(item: dict[str, Any]) -> None:
    """时间窗优先：库内典型时长可能与已排 start/end 冲突（西湖 480 vs 150）。"""
    start = parse_time(item.get("start_time"))
    end = parse_time(item.get("end_time"))
    if start is not None and end is not None and end > start:
        item["duration_min"] = end - start
