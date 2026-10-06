"""来源背书的唯一判定：外部证据行 / 模型自选项 → 线级标签（PLAN-A1 G4）。

为什么单独成模块：同一份「这行证据该换什么徽章」的判定此前有三份实现，且互不一致：

- `ReferencePool.ground`：做值域校验，并对缺坐标的行降级；
- `formatting/facts.apply_item_facts`：**不看值域**，池里缺 source 也照落
  partially_verified/observed；
- `day_stream` 的单日标签：缺 source 默认成 `"llm"`，而 `"llm"` 在值域内 →
  行自己给自己背书。

结果是同一个行程项拿什么徽章取决于它走了哪条链路——即"徽章可伪造"。本模块是
唯一实现，三条链路共用；改背书口径只改这里。

边界（有意不含的部分）：本模块只回答「已有证据怎么标」，不回答「这个名字是否
真的存在」。后者由存在性解析器给出判定（`app/agent/grounding/existence.py`）：解析器可
插拔、结论是三值的（证实/证伪/未判定），而标签值域是线级契约，两者强行合并会
让"provider 明确说没有"和"没查"共用一个字段。

`AUTHORITATIVE_SOURCE_PREFIXES` 是**字符串值域**校验：它是 G1 之前的过渡口径
（能挡住调用方伪造 source，挡不住"本服务没跑过解析器却写着解析器的名字"）。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

from app.agent.grounding.grounding_evidence import verify_evidence
from app.schemas.trip import SourceRecord

# 权威来源值域：只有这些前缀的 source 才允许为行程项背书
# （verification_status=partially_verified / value_kind=observed）。
# /v1/generate-day 的 context 由 HTTP 调用方传入，属于不可信输入；若不做
# 值域校验，调用方可伪造 "opentripmap" 让幻觉事实获得外部背书。
# POI 库退役后，外部来源即 OpenTripMap / Nominatim（真实坐标与分类）与
# 联网搜索（真实店名）。"llm" 不在值域内（D5：LLM 不得作为权威入口）。
AUTHORITATIVE_SOURCE_PREFIXES = ("opentripmap", "nominatim", "web.search")
# 来源不在值域内（多为调用方传入的伪造值）时统一改写为该标记并降级。
UNTRUSTED_SOURCE = "client-context"
# 池内行丢了 source：既不背书，也不谎称是客户端传入。
POOL_UNKNOWN_SOURCE = "pool-unknown"
# 未命中任何证据的模型自选点位来源。
OPEN_ITEM_SOURCE = "llm.open_day"

VerificationStatus = Literal["verified", "partially_verified", "unverified"]
ValueKind = Literal["observed", "estimated", "generated"]


@dataclass(frozen=True)
class ItemLabel:
    """一行证据对应的线级标签；`endorsed` 才允许 observed 徽章。"""

    source: str
    source_updated_at: str | None
    verification_status: VerificationStatus
    value_kind: ValueKind
    freshness_status: Literal["fresh", "stale", "unknown"]
    review_requirement: Literal["none", "before_departure"]
    endorsed: bool


def is_authoritative_source(source: object) -> bool:
    text = str(source or "").strip()
    return bool(text) and text.startswith(AUTHORITATIVE_SOURCE_PREFIXES)


def has_valid_coords(row: Mapping[str, Any]) -> bool:
    """坐标存在且非 0/0（0/0 是缺失坐标的哨兵值，不是有效位置）。

    参数用 Mapping 而非 dict：调用方既有开放 dict（落地草稿），也有
    TypedDict（PoiFactRow 权威行）——TypedDict 可赋给 Mapping，不可赋给 dict。
    """
    lat, lng = row.get("latitude"), row.get("longitude")
    if lat is None or lng is None:
        return False
    try:
        return abs(float(lat)) > 1e-6 and abs(float(lng)) > 1e-6
    except (TypeError, ValueError):
        return False


def _degraded(source: str, updated_at: str | None) -> ItemLabel:
    return ItemLabel(
        source=source,
        source_updated_at=updated_at,
        verification_status="unverified",
        value_kind="estimated",
        freshness_status="unknown",
        review_requirement="before_departure",
        endorsed=False,
    )


def is_trusted_row(row: Mapping[str, Any]) -> bool:
    """这行能不能作为事实被采信：来源在值域内 **且** 对得上本服务签发的证据票。

    光看字符串值域是 G1 之前的过渡口径——它挡得住"随手写个 provider 名"，
    挡不住"照抄一个真 provider 名 + 一对编造的坐标"（/v1/generate-day 的
    context 就是调用方传进来的）。票才是"本服务确实查到过"的凭据。
    """
    if not is_authoritative_source(row.get("source")):
        return False
    return verify_evidence(row)


def label_for_evidence_row(row: Mapping[str, Any]) -> ItemLabel:
    """候选池行 / 解析器产出行 → 标签。

    两道闸：来源必须可信（值域 + 有票，否则改写为 client-context），且必须带
    有效坐标（否则行上残留的位置是模型自填的，不随其它字段一起获得 observed）。
    """
    declared = str(row.get("source") or "").strip()
    if not declared:
        return _degraded(POOL_UNKNOWN_SOURCE, None)
    updated_at = str(row.get("source_updated_at") or "") or None
    if not is_trusted_row(row):
        return _degraded(UNTRUSTED_SOURCE, None)
    if not has_valid_coords(row):
        # 来源真实但没有坐标：事实仍来自外部，只是位置不可背书。
        return _degraded(declared, updated_at)
    return ItemLabel(
        source=declared,
        source_updated_at=updated_at,
        verification_status="partially_verified",
        value_kind="observed",
        freshness_status="fresh" if updated_at else "unknown",
        review_requirement="none" if updated_at else "before_departure",
        endorsed=True,
    )


def label_for_generated_item() -> ItemLabel:
    """开放模式里未命中任何证据的模型自选点位：待复核的生成/估算值。

    value_kind 用 estimated 而非 generated：该字段在本链路里说的是"这行的数字
    是估的"（quality 的 estimated_count、前端「估算信息」徽章都按它取），三条
    链路的现状一致，本期不动这个轴。
    """
    return ItemLabel(
        source=OPEN_ITEM_SOURCE,
        source_updated_at=None,
        verification_status="unverified",
        value_kind="estimated",
        freshness_status="unknown",
        review_requirement="before_departure",
        endorsed=False,
    )


def apply_label(item: dict[str, Any], label: ItemLabel) -> None:
    """把标签写进落地草稿（字段名与 TripItem 契约一致）。"""
    item["source"] = label.source
    item["source_updated_at"] = label.source_updated_at
    item["verification_status"] = label.verification_status
    item["value_kind"] = label.value_kind
    item["freshness_status"] = label.freshness_status
    item["review_requirement"] = label.review_requirement


def label_for_landed_item(item: Mapping[str, Any], city: str = "") -> ItemLabel:
    """落地草稿自身的标签（未命中候选池的那批点位）。

    item 上的 `source` 只可能由本服务的解析器写上（模型自报的来源字段在 LLM
    输出边界已被剥离），而解析成功时同步签了票——所以这里仍然是"有票才背书"。
    `city` 用于查票（票按 (城市, 名字) 记账）。
    """
    if not is_authoritative_source(item.get("source")):
        return label_for_generated_item()
    row = {**item, "name": item.get("poi_name") or item.get("name") or "", "city": item.get("city") or city}
    return label_for_evidence_row(row)


def source_record_for(label: ItemLabel, row: Mapping[str, Any] | None = None) -> SourceRecord:
    """标签 → 来源记录（GenerateResponse.sources 的去重登记）。

    provider 只能取标签里的来源：此前未命中分支只要项上有坐标就补一条
    `provider="local"` 的 "local-grounding" 记录，而那条路径从未运行过任何
    解析器——给编造的事实做溯源，比不溯源更糟。
    """
    retrieved = str((row or {}).get("source_fetched_at") or label.source_updated_at or "") or None
    return SourceRecord(
        source_id=label.source,
        storage_source=label.source,
        provider=label.source,
        retrieved_at=retrieved,
        expires_at=None,
    )
