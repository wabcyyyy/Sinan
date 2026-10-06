"""生成链路共享内核：产品口径与确定性 helper 的唯一真相源。

架构（已合并为一张图）：

| 模块 | 角色 |
|------|------|
| ``trip_graph`` | **唯一** LangGraph 编排图；``mode=day`` / ``mode=trip`` 分流 |
| ``day_stream`` | 单次生成「事实层」：开放模式 Prompt、坐标/价格落地、ReferencePool |
| ``workflow`` | 整段业务节点实现 + ``run_generate`` 门面 |
| ``day_workflow`` | 逐日门面 + ``generate_day_once`` 兼容再导出 |
| ``generation_core`` | 产品口径（N-1 晚/草案/重试常量/摊铺） |

改动产品规则只改本模块；改编排拓扑只改 ``trip_graph``。
"""

from __future__ import annotations

import copy
from collections.abc import Mapping
from typing import Any, TypedDict

# 单日/兼容路径的校验修复次数
MAX_FIX_ATTEMPTS = 2
# 开放研究失败总尝试（含首次）：失败 → 重试一次 → 待研究草案
MAX_GENERATION_ATTEMPTS = 2
# 逐日 LangGraph 单日修复上限（与 MAX_FIX_ATTEMPTS 对齐，避免两套常量漂移）
MAX_DAY_ATTEMPTS = MAX_FIX_ATTEMPTS
# Supervisor 证据缺口补查次数
MAX_REFILLS = 1

DRAFT_NOTE_SUFFIX = "待研究"

# 开放模式（LLM 世界知识定价）逐日 note 的统一后缀（GROUND-3 单一真源）：
# 单日链与整段链共用，避免同一行程混排两套口径
PRICE_ESTIMATE_NOTE_SUFFIX = "（价格为估算，请以现场或官方渠道为准）"

# TripItem 合法类型（与 schemas.trip 落库契约一致）
ALLOWED_ITEM_TYPES = frozenset({"attraction", "food", "hotel", "transport"})

# LLM 开放生成常吐出的扩展类型 → 归一到契约类型，避免 Pydantic 校验 500
_ITEM_TYPE_ALIASES = {
    "souvenir": "attraction",
    "activity": "attraction",
    "experience": "attraction",
    "shopping": "attraction",
    "shop": "attraction",
    "market": "attraction",
    "viewpoint": "attraction",
    "scenic": "attraction",
    "landmark": "attraction",
    "museum": "attraction",
    "park": "attraction",
    "temple": "attraction",
    "restaurant": "food",
    "meal": "food",
    "cafe": "food",
    "bar": "food",
    "snack": "food",
    "lodging": "hotel",
    "hostel": "hotel",
    "stay": "hotel",
    "traffic": "transport",
    "metro": "transport",
    "taxi": "transport",
    "walk": "transport",
}


def normalize_item_type(raw: Any) -> str:
    """把 LLM/客户端 item_type 归一到合法值；未知类型回退 attraction。"""
    t = str(raw or "").strip().lower()
    if t in ALLOWED_ITEM_TYPES:
        return t
    return _ITEM_TYPE_ALIASES.get(t, "attraction")


class PoiFactRow(TypedDict, total=False):
    """知识库权威行（候选快照/lookup）在落地边界的已知键形状（G-1.6）。"""

    id: int | str
    name: str
    latitude: float
    longitude: float
    ticket_price: float
    open_time: str
    duration_min: int
    address: str
    rating: float
    tags: str
    image: str
    source: str
    source_updated_at: str
    source_fetched_at: str
    avg_cost: float


def sanitize_itinerary_items(items: list[Any] | None) -> list[dict[str, Any]]:
    """过滤脏项并归一 item_type，供 format_output / day_stream 构造 TripItem 前调用。

    返回 dict[str, Any] 而非 TypedDict：落地链上每一步都允许缺键/写 None
    （可变开放草稿），TypedDict(total=False) 的读写严格度会在这条链上处处
    报错；键类型推断的收益放在只读的 PoiFactRow 上（facts 落地查询）。
    """
    out: list[dict[str, Any]] = []
    for item in filter_dirty_items(items):
        row = dict(item)
        row["item_type"] = normalize_item_type(row.get("item_type"))
        out.append(row)
    return out


def stay_nights(days: int | None) -> int:
    """产品定稿：N 天行程住 N-1 晚；最后一天不安排入住。"""
    try:
        d = int(days or 1)
    except (TypeError, ValueError):
        d = 1
    return max(d - 1, 0)


def day_needs_hotel(day_no: int, days: int | None, *, override: bool | None = None) -> bool:
    """某一天是否应安排酒店。``override`` 为客户端/上游显式 needs_hotel 时优先生效。"""
    if override is not None:
        return bool(override)
    nights = stay_nights(days)
    try:
        n = int(day_no)
    except (TypeError, ValueError):
        return False
    return 1 <= n <= nights


def hotel_prompt_clause(needs_hotel: bool, days: int | None) -> str:
    """多日/单日酒店 Prompt 条款（与 stay_nights 口径一致）。"""
    d = int(days or 1)
    if needs_hotel and d > 1:
        last_stay = max(d - 1, 0)
        return (
            f"第 1 至第 {last_stay} 天每天都要有同一家酒店的入住项"
            "（全程沿用同一家，默认不换店，仅前往远距离区域转点时可换），最后一天不安排入住"
        )
    if needs_hotel:
        return "安排 1 家酒店"
    return "按需安排酒店"


def day_hotel_clause(needs_hotel: bool) -> str:
    """单日 Prompt 中的酒店短句。"""
    return "安排 1 家酒店；" if needs_hotel else "今日无需安排酒店；"


def count_hotel_nights_in_budget(plan_day_no: int, days: int | None, item_type: str) -> bool:
    """预算/落库是否计入该酒店项：末日酒店在多日行程中不计（N-1 晚口径）。"""
    if item_type != "hotel":
        return False
    d = int(days or 1)
    n = int(plan_day_no or 0)
    return not (d > 1 and n >= d)


def spread_hotels(plans: list[dict], nights: int | None = None, days: int | None = None) -> int:
    """全程同一家酒店：把首个酒店项复制到仍缺入住的晚次。

    不改内容来源（仍是 LLM 选的那家店），只做确定性摊铺。
    """
    if nights is None:
        nights = stay_nights(days if days is not None else _max_day_no(plans))
    if nights <= 0 or not plans:
        return 0
    source: dict | None = None
    for plan in plans:
        for item in plan.get("items") or []:
            if item.get("item_type") == "hotel":
                source = item
                break
        if source:
            break
    if not source:
        return 0
    added = 0
    for plan in plans:
        day_no = int(plan.get("day_no") or 0)
        if day_no < 1 or day_no > nights:
            continue
        items = plan.setdefault("items", [])
        if any(it.get("item_type") == "hotel" for it in items):
            continue
        clone = copy.deepcopy(source)
        clone["sort_no"] = len(items)
        items.append(clone)
        added += 1
    return added


def meal_slot_of(start_time: str | None, end_time: str | None = None) -> str | None:
    """按开始时间粗分餐次：lunch(10:30-14:30) / dinner(17:00-21:30) / breakfast(05:00-10:30)。"""
    raw = str(start_time or "").strip()
    if not raw or ":" not in raw:
        return None
    try:
        hour, minute = raw.split(":", 1)
        mins = int(hour) * 60 + int(minute)
    except ValueError:
        return None
    if 5 * 60 <= mins < 10 * 60 + 30:
        return "breakfast"
    if 10 * 60 + 30 <= mins < 14 * 60 + 30:
        return "lunch"
    if 17 * 60 <= mins <= 21 * 60 + 30:
        return "dinner"
    return "other"


def has_double_lunch(foods: list[dict]) -> bool:
    """当日是否出现两顿午餐（两条餐饮都落在午间窗口）。"""
    lunches = [f for f in foods or [] if meal_slot_of(f.get("start_time")) == "lunch"]
    return len(lunches) >= 2


def fill_zero_costs(plans: list[dict], lookup: dict[str, dict]) -> int:
    """餐饮/酒店 cost=0 时用知识库权威价覆盖（模型常把未知价写成 0）。

    0 价补水的唯一实现（G-1.3 ③：原 workflow 与 trip_stream._prepare_day 双份
    内联收敛于此；治因——图/流式双生成链路本身——归 G-2.5）。

    注意：RAG/知识库中的 ticket_price=0 与 None 含义不同——None 表示未知，
    0 对景点表示免费；对 food/hotel 的 0 一律视为未知并回落 avg_cost。
    """
    filled = 0
    for plan in plans:
        for item in plan.get("items") or []:
            if item.get("item_type") not in ("food", "hotel"):
                continue
            try:
                cost = float(item.get("cost")) if item.get("cost") is not None else None
            except (TypeError, ValueError):
                cost = None
            if cost not in (0, 0.0, None):
                continue
            name = str(item.get("poi_name") or "").strip()
            poi = lookup.get(name) or {}
            price = poi.get("ticket_price")
            if price is None or price == 0:
                price = poi.get("avg_cost")
            if price is None or price == 0:
                continue
            try:
                item["cost"] = float(price)
                filled += 1
            except (TypeError, ValueError):
                continue
    return filled


def estimate_plans_total(
    plans: list[dict], persons: int, days: int | None, consumption: dict | None = None, rooms: int | None = None
) -> dict:
    """按与 Java BudgetEngine 对齐的口径估算行程总价（用于超支硬约束）。

    门票/餐饮：单价 × 人数；酒店：单价 × 房间数（默认 2 人间）；交通：城市日均。
    """
    p = max(int(persons or 1), 1)
    d = max(int(days or len(plans or []) or 1), 1)
    room_n = rooms if rooms is not None else max((p + 1) // 2, 1)
    ticket = 0.0
    meal = 0.0
    hotel = 0.0
    for plan in plans or []:
        day_no = int(plan.get("day_no") or 0)
        for item in plan.get("items") or []:
            try:
                cost = float(item.get("cost") or 0)
            except (TypeError, ValueError):
                cost = 0.0
            t = str(item.get("item_type") or "")
            if t == "attraction":
                ticket += cost
            elif t == "food":
                meal += cost
            elif t == "hotel" and count_hotel_nights_in_budget(day_no, days or len(plans or []), "hotel"):
                hotel += cost
    transport = float((consumption or {}).get("transport_price", 35.0) or 35.0)
    total = round(ticket * p + meal * p + hotel * room_n + transport * d * p, 2)
    return {
        "门票": round(ticket * p, 2),
        "餐饮": round(meal * p, 2),
        "酒店": round(hotel * room_n, 2),
        "交通": round(transport * d * p, 2),
        "合计": total,
    }


def draft_day_plans(city: str, days: int, reason: str) -> list[dict]:
    """LLM 失败后的「待研究草案」：如实空日，绝不冒充生成结果。"""
    try:
        n = int(days)
    except (TypeError, ValueError):
        n = 1
    n = max(n, 1)
    return [
        {"day_no": day_no, "note": f"{city}第{day_no}天{DRAFT_NOTE_SUFFIX}", "items": []} for day_no in range(1, n + 1)
    ]


def filter_dirty_items(items: list[Any] | None) -> list[dict]:
    """过滤非 dict / 无 poi_name 的脏项，避免下游崩溃。"""
    out: list[dict] = []
    for item in items or []:
        if isinstance(item, dict) and str(item.get("poi_name") or "").strip():
            out.append(item)
    return out


# 模型不得申报的字段（D6=C + G4）：点位身份、位置与溯源标签一律由服务端解析器写。
# 允许模型自填坐标/来源等于给编造的名字发一张真地图钉——落地链"已有坐标就不
# 解析"的判据会被直接绕过，幻觉点位从此不再经过任何存在性检查。
_MODEL_CLAIM_KEYS = (
    "latitude",
    "longitude",
    "poi_id",
    "poiId",
    "xid",
    "source",
    "source_updated_at",
    "sourceUpdatedAt",
    "verification_status",
    "verificationStatus",
    "value_kind",
    "valueKind",
    "freshness_status",
    "freshnessStatus",
    "review_requirement",
    "reviewRequirement",
)


def has_model_claims(item: Mapping[str, Any]) -> bool:
    """这一项是否试图自报身份/位置/标签（剥离与计数共用同一判据）。"""
    return any(item.get(key) not in (None, "", [], {}) for key in _MODEL_CLAIM_KEYS)


def strip_model_claims(item: dict[str, Any]) -> None:
    """就地剥掉模型自报的身份/位置/标签字段。"""
    for key in _MODEL_CLAIM_KEYS:
        item.pop(key, None)


def _max_day_no(plans: list[dict]) -> int:
    m = 0
    for plan in plans:
        try:
            m = max(m, int(plan.get("day_no") or 0))
        except (TypeError, ValueError):
            continue
    return m or 1
