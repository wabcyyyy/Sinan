"""备选池：候选组装、类别地板与上限、缺口补齐（G-2.2 自 generators 拆出）。

- build_suggestions：把模型建议 + 权威候选 + 体验类补入组装成备选池条目；
  dest_city 给定时经 suggestion_city 城市归属校验（BIZ-3 三链同一真源）；
- fill_suggestion_gaps：按类别地板与上限补齐缺口（含确定性兜底槽位）；
- 低质场所关键词与建议数量契约是本模块的规则常量。
"""

import logging

from app.agent.core.poi_identity import norm_ws_key
from app.agent.generation.content.suggestion_city import filter_suggestions_by_city
from app.agent.runtime.trace import record_event

logger = logging.getLogger(__name__)

TIME_SLOTS = [
    ("09:00", "11:30"),
    ("13:30", "16:00"),
    ("19:00", "20:30"),
]
THREE_ATTRACTION_SLOTS = [
    ("09:00", "11:00"),
    ("13:30", "15:30"),
    ("16:00", "18:00"),
]

# 低质/与旅行体验无关的场所：在 prompt 层约束模型不要选入行程与备选池
LOW_QUALITY_KEYWORDS = (
    "舞厅",
    "歌厅",
    "夜总会",
    "网吧",
    "棋牌",
    "麻将",
    "农贸",
    "菜市场",
    "菜场",
    "批发",
    "招待所",
    "五金",
    "建材",
    "汽配",
    "维修",
    "废品",
    "殡葬",
)

SUGGESTION_LIMIT = 80
SUGGESTION_MIN_PER_CATEGORY = 4
SUGGESTION_MAX_PER_CATEGORY = 20
# shopping=商城/名店（产品语义）；souvenir 兼容历史
SUGGESTION_CATEGORIES = ("attraction", "activity", "food", "hotel", "shopping", "souvenir")


def _daily_attraction_target(days: int) -> int:
    """确定性兜底的每日景点数：总天数越长，每日安排越精简、节奏越舒适。"""
    # 两个景点能为跨城移动和用餐保留足够缓冲；短途也不以堆景点换取数量。
    return 2


def activity_floor(city: str) -> list[dict]:
    """体验类不在 Supervisor 三域研究里：联网搜索补入，保证「发现更多-体验」有地板。

    唯一实现（G-1.3 ④）：原在 _generate_open_plans 后处理与建议装配两处逐字重复。
    POI 库退役后体验类无本地池，走联网搜索取真实项目名（不保证坐标）。
    """
    from app.agent.data.web_search import search_places_via_web

    rows = search_places_via_web(city, "activity", limit=12)
    for row in rows:
        row.setdefault("id", row.get("name"))
        row["_authoritative"] = True
    return rows


def floor_suggestions(raw: list[dict], extra_pool: list[dict]) -> list[dict]:
    """备选池数量地板：主类尽量 ≥4、每类 ≤20；shopping=商城/名店。

    酒店/体验/美食也参与地板；体验类从 extra_pool 的 activity 或景点映射补充。
    """
    main_cats = ("attraction", "activity", "food", "hotel", "shopping")
    max_per = 20
    min_per = 4

    _norm_name = norm_ws_key

    by_cat: dict[str, list[dict]] = {}
    used_names: set[str] = set()
    for s in raw or []:
        if not isinstance(s, dict):
            continue
        cat = str(s.get("category") or "attraction")
        if cat == "souvenir":
            cat = "shopping"
        name = str(s.get("name") or s.get("poi_name") or "").strip()
        key = _norm_name(name)
        if name and key in used_names:
            continue
        if name:
            used_names.add(key)
        by_cat.setdefault(cat, []).append({**s, "category": cat})

    def _from_poi(poi: dict, cat: str) -> dict:
        name = str(poi.get("name") or "").strip()
        price = poi.get("ticket_price")
        if price is None or price == 0:
            price = poi.get("avg_cost")
        return {
            "name": name,
            "category": cat,
            "intro": (poi.get("description") or "")[:80] or None,
            "latitude": poi.get("latitude"),
            "longitude": poi.get("longitude"),
            "estimated_cost": float(price or 0),
            "source": poi.get("source"),
            "poi_id": str(poi.get("id") or "") or None,
        }

    for cat in main_cats:
        if len(by_cat.get(cat) or []) >= min_per:
            continue
        need = min_per - len(by_cat.get(cat) or [])
        pool_cat = cat
        if cat == "activity":
            pool_cat = "activity"
        for poi in extra_pool or []:
            if need <= 0:
                break
            if str(poi.get("category") or "") != pool_cat:
                continue
            name = str(poi.get("name") or "").strip()
            key = _norm_name(name)
            if not name or key in used_names:
                continue
            by_cat.setdefault(cat, []).append(_from_poi(poi, cat))
            used_names.add(key)
            need -= 1
        # 体验类知识库常为空：允许用未入程的高分景点/付费体验顶上
        if cat == "activity" and need > 0:
            for poi in extra_pool or []:
                if need <= 0:
                    break
                if str(poi.get("category") or "") != "attraction":
                    continue
                name = str(poi.get("name") or "").strip()
                key = _norm_name(name)
                if not name or key in used_names:
                    continue
                tags = str(poi.get("tags") or "")
                try:
                    rating = float(poi.get("rating") or 0)
                except (TypeError, ValueError):
                    rating = 0
                # 海外开放模式常无评分：无 rating 时只看标签，避免 activity 永远为 0
                if (
                    poi.get("rating") is not None
                    and rating < 4.3
                    and not any(
                        x in tags for x in ("体验", "演出", "潜水", "SPA", "spa", "冲浪", "剧场", "美术馆", "观景")
                    )
                ):
                    continue
                by_cat.setdefault(cat, []).append(_from_poi(poi, "activity"))
                used_names.add(key)
                need -= 1
    out: list[dict] = []
    for cat in ("attraction", "activity", "food", "hotel", "shopping"):
        out.extend((by_cat.get(cat) or [])[:max_per])
    return out


def build_suggestions(
    plans: list[dict],
    candidates: list[dict] | None,
    foods: list[dict] | None,
    hotels: list[dict] | None,
    raw_suggestions: list[dict] | None = None,
    limit: int = SUGGESTION_LIMIT,
    allow_external: bool = False,
    dest_city: str | None = None,
) -> list[dict]:
    """构建「发现更多」备选池：当初提供给模型的候选中、未排入行程的优质点位。

    优先采用模型给出的建议（含一句话介绍与预约提示），但会过滤掉已排入
    行程、不在候选池或命中低质关键词的条目；不足时用剩余候选确定性补齐。
    补齐按品类均衡进行：每类至少 SUGGESTION_MIN_PER_CATEGORY 条（候选池有
    对应来源时）、至多 SUGGESTION_MAX_PER_CATEGORY 条，剩余名额跨品类轮转
    分配，避免备选池被单一品类占满。

    allow_external=True（开放模式）时，不在候选池中的模型建议也放行：
    坐标/地址留空，由前端在加入行程前经地图检索补齐；低质关键词过滤仍然生效。
    dest_city 给定时（BIZ-3 单一真源）：模型建议先过 filter_suggestions_by_city
    城市归属校验——此前只有流式链过滤，trip/day 链会把外城点位混进备选池。
    """
    pool: dict[str, dict] = {}
    for poi in list(candidates or []) + list(foods or []) + list(hotels or []):
        name = str(poi.get("name") or "").strip()
        if name and name not in pool:
            pool[name] = poi

    used: set[str] = set()
    for plan in plans or []:
        for item in plan.get("items") or []:
            name = str(item.get("poi_name") or "").strip()
            if name:
                used.add(name)

    def _category_of(poi: dict, hinted: str | None = None) -> str:
        cat = str(hinted or poi.get("category") or "attraction")
        if cat == "souvenir":
            return "shopping"
        if cat in ("attraction", "activity", "food", "hotel", "shopping"):
            return cat
        if cat == "hotel" or "酒店" in cat or "住宿" in cat or "客栈" in cat:
            return "hotel"
        if "餐" in cat or "食" in cat or "小吃" in cat:
            return "food"
        if any(kw in cat for kw in ("购物", "商场", "百货", "市集", "市场", "商店")):
            return "shopping"
        return "attraction"

    def _entry(name: str, poi: dict, raw: dict | None = None) -> dict:
        raw = raw or {}
        price = poi.get("ticket_price")
        cost = raw.get("estimated_cost")
        category = _category_of(poi, raw.get("category"))
        # 购物类不估价：花多少取决于用户自己买什么，固定「人均 ¥5000」
        # 只会削弱可信度（前端对应展示「按店内消费为准」）。
        estimated = float(cost) if isinstance(cost, (int, float)) else (float(price) if price is not None else None)
        if category == "shopping":
            estimated = None
        return {
            "poi_id": str(poi.get("id") or "") or None,
            "name": name,
            "category": category,
            "address": poi.get("address"),
            "latitude": poi.get("latitude"),
            "longitude": poi.get("longitude"),
            "intro": str(raw.get("intro") or "").strip() or None,
            "need_reservation": bool(raw.get("need_reservation")),
            "estimated_cost": estimated,
        }

    buckets: dict[str, list[dict]] = {cat: [] for cat in SUGGESTION_CATEGORIES}
    counts: dict[str, int] = {cat: 0 for cat in SUGGESTION_CATEGORIES}
    seen: set[str] = set()

    def _admit(name: str, poi: dict | None, raw: dict | None = None) -> bool:
        """将一个候选点位收入对应品类桶；已用/重复/低质/超品类上限时拒绝。"""
        if not name or name in used or name in seen:
            return False
        if (poi is None and not allow_external) or any(kw in name for kw in LOW_QUALITY_KEYWORDS):
            return False
        entry = _entry(name, poi or {}, raw)
        cat = entry["category"]
        if counts[cat] >= SUGGESTION_MAX_PER_CATEGORY:
            return False
        seen.add(name)
        buckets[cat].append(entry)
        counts[cat] += 1
        return True

    # 1) 先从候选池按品类下限占位（酒店/体验等必须出现在「发现更多」）
    #    再放入模型建议——否则模型给出的景/餐会先占满 SUGGESTION_LIMIT，
    #    酒店下限补全永远执行不到（历史缺陷：酒店 tab 全空）。
    pool_items = list(pool.items())
    for cat in SUGGESTION_CATEGORIES:
        idx = 0
        while counts[cat] < SUGGESTION_MIN_PER_CATEGORY and idx < len(pool_items):
            name, poi = pool_items[idx]
            idx += 1
            if _category_of(poi) != cat:
                continue
            _admit(name, poi)

    # 2) 模型建议优先（保持模型给出的顺序与介绍文案）；dest_city 给定时先过
    #    城市归属校验（BIZ-3：三条链共用本函数，过滤随之全链生效）
    for raw in filter_suggestions_by_city(raw_suggestions or [], dest_city) if dest_city else (raw_suggestions or []):
        if not isinstance(raw, dict):
            continue
        name = str(raw.get("poi_name") or raw.get("name") or "").strip()
        _admit(name, pool.get(name), raw)

    # 3) 剩余名额跨品类轮转补齐，保持整体均衡（单类不超过上限）
    remaining = limit - sum(counts.values())
    progressed = True
    while remaining > 0 and progressed:
        progressed = False
        for cat in SUGGESTION_CATEGORIES:
            if remaining <= 0:
                break
            if counts[cat] >= SUGGESTION_MAX_PER_CATEGORY:
                continue
            for name, poi in pool_items:
                if name in used or name in seen:
                    continue
                if _category_of(poi) != cat:
                    continue
                if _admit(name, poi):
                    remaining -= 1
                    progressed = True
                    break

    # 4) 兜底：按品类轮转仍填不满（如全部候选已用尽）时放开品类均衡，
    #    将剩余可用候选收入尚未达上限的品类
    if remaining > 0:
        for name, poi in pool_items:
            if remaining <= 0:
                break
            if name in used or name in seen:
                continue
            if any(kw in name for kw in LOW_QUALITY_KEYWORDS):
                continue
            entry = _entry(name, poi)
            cat = entry["category"]
            if counts[cat] >= SUGGESTION_MAX_PER_CATEGORY:
                continue
            seen.add(name)
            buckets[cat].append(entry)
            counts[cat] += 1
            remaining -= 1

    # 按品类分组输出，组间顺序 attraction → activity → food → hotel → shopping
    results: list[dict] = []
    for cat in ("attraction", "activity", "food", "hotel", "shopping", "souvenir"):
        results.extend(buckets.get(cat) or [])
    return results[:limit]


def fill_suggestion_gaps(
    suggestions: list[dict],
    city: str,
    *,
    budget_tier: str | None = None,
    min_per_category: int = SUGGESTION_MIN_PER_CATEGORY,
) -> list[dict]:
    """类目不足时用联网搜索补齐「发现更多」地板（酒店/体验/美食优先）。

    候选池为空时原先的地板补齐会静默失败；本函数在池外再补一轮真实地点名，
    不改变已有条目，只追加缺口类目。
    """
    from app.agent.data.web_search import search_places_via_web, web_search_enabled

    if not web_search_enabled():
        return suggestions
    counts: dict[str, int] = {cat: 0 for cat in SUGGESTION_CATEGORIES}
    seen: set[str] = set()
    for s in suggestions or []:
        if not isinstance(s, dict):
            continue
        name = str(s.get("name") or s.get("poi_name") or "").strip()
        cat = str(s.get("category") or "attraction")
        if cat == "souvenir":
            cat = "shopping"
        if name:
            seen.add(name)
        if cat in counts:
            counts[cat] += 1

    filled = list(suggestions or [])
    # 优先用户最常反馈的缺口：酒店、体验、美食、购物
    for cat in ("hotel", "activity", "food", "shopping"):
        need = min_per_category - counts.get(cat, 0)
        if need <= 0:
            continue
        rows = search_places_via_web(city, cat, limit=need, budget_tier=budget_tier)
        for row in rows:
            name = str(row.get("name") or "").strip()
            if not name or name in seen:
                continue
            seen.add(name)
            filled.append(
                {
                    "poi_id": None,
                    "name": name,
                    "category": cat,
                    "address": None,
                    "latitude": None,
                    "longitude": None,
                    "intro": row.get("intro"),
                    "need_reservation": cat in ("hotel", "activity"),
                    "estimated_cost": row.get("estimated_cost"),
                    "used": False,
                }
            )
            counts[cat] = counts.get(cat, 0) + 1
    if filled != (suggestions or []):
        record_event(
            "decision",
            "suggestion_web_fill",
            metadata={
                "city": city,
                "before": len(suggestions or []),
                "after": len(filled),
                "counts": counts,
            },
        )
    return filled
