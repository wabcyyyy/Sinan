"""面向 Agent 的地点检索工具集（OTM 为主 + 联网搜索补池 + Nominatim 兜底）。

职责：
- search_attractions：OTM 半径检索（坐标/分类/热度）+ 偏好 kinds 排序 + 头部详情；
- search_foods / search_hotels：OTM 同源分类池（海外覆盖尚可）+ 联网搜索补真实店名；
- search_local_poi：名称→坐标解析（Nominatim），grounding 与补池共用；
- 把前端展示偏好标签展开为 OTM kinds 关键词并据此排序；
- get_consumption：城市消费基准（city_consumption，唯一保留的本地事实表）。

事实边界（与旧权威库的本质差异）：
- OTM/Nominatim **没有票价/营业时间/评分**——这些字段恒为空，由 LLM 估价并按
  `estimated` 如实标注，行程页以地图深链引导用户出发前核实；
- OTM 仅 en/ru 语言，返回名称为英文；中文名由生成链路的 LLM 对齐，
  `existence.same_entity` 的字符重叠口径对中英混排仍然有效（英文名含于中文介绍时），
  跨脚本写法（浅草寺/淺草寺）只有 provider 的 name:* 别名能对上。

依赖：places（OTM/Nominatim/深链）、city_reference（城市字典/消费基准）、
web_search（联网补池）。**零本地语料、零向量库。**

同一实体判定已提到 `app/agent/grounding/existence.py`（G-1.2 提级的下一步：它现在是
存在性判定的组成部分，池内命中也要过它）。
"""

import logging
import re

from app.agent.data import city_reference, places, web_search
from app.agent.data.city_center import city_center
from app.agent.grounding.existence import same_entity
from app.agent.grounding.grounding_evidence import issue_evidence
from app.agent.runtime.trace import traced
from app.common.config import settings
from app.common.external_client import BACKGROUND, ExternalClient, fetch_json
from app.common.http_client import contact_user_agent, image_client

logger = logging.getLogger(__name__)

# OTM kinds → 我们品类概念（半径检索按此收窄；OTM 本质只有"旅游向"数据）。
# 取值全部对真实 API 实测过（2026-09-30，成都 30.66,104.06 → 200）：注意住宿类
# 上游拼的是 **accomodations**（一个 m，OTM 自己的拼写错误）；按"正确英语"拼
# accommodations 会 400 Unknown category name——整个酒店域 OTM 池归零（2026-09-18
# 量测文档与旧 mock 注释都把这个 400 误记成"免费 key 不支持"，实为拼写）。合法
# 集合由 tests/test_tools_kinds.py 锁死，改动须先对真实 API 复测并同步该测试。
_OTM_KINDS_BY_CATEGORY: dict[str, tuple[str, ...]] = {
    "attraction": ("interesting_places",),
    "food": ("foods",),
    "hotel": ("accomodations",),
    "activity": ("theatres_and_entertainments", "amusements", "cultural"),
}

# 前端展示标签 → OTM kinds/名称/简介 关键词（排序加权用，不做硬过滤）
PREFERENCE_KEYWORDS = {
    "人文历史": ("historic", "cultural", "museums", "monuments", "religious", "burial"),
    "自然风光": ("natural", "greenery", "parks", "gardens", "beaches", "geological"),
    "美食": ("foods", "restaurants"),
    "网红出片": ("architecture", "bridges", "towers", "skyscrapers", "viewpoints"),
    "主题娱乐": ("amusements", "theatres", "aquariums", "zoos", "theme"),
    "购物": ("shops", "malls", "markets", "bazaars"),
}


def _expand(preferences: list[str]) -> list[str]:
    """把前端展示标签展开为 kinds/名称关键词；未映射的原样保留。"""
    out: list[str] = []
    for p in preferences or []:
        out.extend(PREFERENCE_KEYWORDS.get(p, (p,)))
    return out


def _match_preferences(row: dict, keywords: list[str]) -> bool:
    haystack = " ".join(str(row.get(key) or "") for key in ("kinds", "name", "intro", "tags")).lower()
    return any(str(keyword).lower() in haystack for keyword in keywords)


def _sort_by_preferences(rows: list[dict], preferences: list[str]) -> list[dict]:
    if not preferences:
        return rows
    keywords = _expand(preferences)
    preferred = [row for row in rows if _match_preferences(row, keywords)]
    if len(preferred) >= 6:
        return preferred + [row for row in rows if row not in preferred]
    return rows


def _as_candidate(row: dict, city: str) -> dict:
    """外部地点行 → 生成链路候选：补 id（外部 ID 落 itinerary_item.poi_id）与来源标记。

    票价/时长/营业时间数据源不再提供，恒缺——下游全部防御式读取，
    缺失即由 LLM 估价（value_kind=estimated）。
    """
    candidate = dict(row)
    candidate.setdefault("id", row.get("xid") or row.get("name"))
    candidate.setdefault("city", city)
    candidate.setdefault("ticket_price", None)
    candidate.setdefault("duration_min", None)
    candidate.setdefault("open_time", None)
    # 联网补池行的 LLM 估价统一落到 avg_cost（酒店/餐饮的既有价格消费口径）
    if candidate.get("avg_cost") is None and row.get("estimated_cost") is not None:
        candidate["avg_cost"] = row["estimated_cost"]
    candidate["_authoritative"] = True
    # 取到数据的当场签发证据票：之后任何链路引用这行时，背书凭的是票，
    # 不是行上那个可以被任何调用方照抄的 source 字符串（PLAN-A1 G1）。
    issue_evidence(candidate)
    return candidate


def _otm_pool(city: str, category: str, limit: int) -> list[dict]:
    """OTM 半径池：城市定位 → 分类半径检索 → 头部详情补齐；任一步不可用返回空。"""
    center = city_center(city)
    if not center:
        return []
    kinds = ",".join(_OTM_KINDS_BY_CATEGORY.get(category, ()))
    rows = places.search_places_near(
        center["latitude"],
        center["longitude"],
        city=city,
        kinds=kinds or None,
        category=category,
        limit=max(limit, settings.otm_limit),
    )
    return [_as_candidate(row, city) for row in places.enrich_with_details(rows)]


def _web_pool(city: str, category: str, limit: int) -> list[dict]:
    """联网搜索补池：真实店名（中文），不保证坐标；关闭/失败返回空。"""
    rows = web_search.search_places_via_web(city, category, limit=limit)
    return [_as_candidate(row, city) for row in rows]


def _merge_by_name(*pools: list[dict]) -> list[dict]:
    """多池按名称合并去重（先到先得：OTM 行带坐标优先，web 行补店名/估价）。"""
    merged: list[dict] = []
    seen: set[str] = set()
    for pool in pools:
        for row in pool:
            name = str(row.get("name") or "").strip()
            if not name or name in seen:
                continue
            seen.add(name)
            merged.append(row)
    return merged


@traced("tool", "poi.search_attractions")
def search_attractions(city: str, preferences: list[str], limit: int = 30) -> list[dict]:
    rows = _merge_by_name(_otm_pool(city, "attraction", limit), _web_pool(city, "attraction", limit))
    return _sort_by_preferences(rows, preferences)[:limit]


@traced("tool", "poi.search_foods")
def search_foods(city: str, limit: int = 10) -> list[dict]:
    return _merge_by_name(_otm_pool(city, "food", limit), _web_pool(city, "food", limit))[:limit]


@traced("tool", "poi.search_hotels")
def search_hotels(city: str, limit: int = 6) -> list[dict]:
    return _merge_by_name(_otm_pool(city, "hotel", limit), _web_pool(city, "hotel", limit))[:limit]


def get_poi_detail(city: str, name: str) -> dict | None:
    """名称→坐标/地址解析（Nominatim，接受中文）。查不到返回 None，上层如实降级。"""
    hit = places.geocode_place(str(name or "").strip(), city)
    if not hit:
        return None
    row = _as_candidate(
        {
            "name": str(name or "").strip() or hit["name"],
            "latitude": hit["latitude"],
            "longitude": hit["longitude"],
            "address": hit.get("display_name") or None,
            "source": hit.get("source") or "nominatim",
        },
        city,
    )
    row.pop("popularity", None)
    row.pop("kinds", None)
    return row


@traced("tool", "poi.search_local_poi")
def search_local_poi(city: str, name: str, *, category: str | None = None) -> list[dict]:
    """按名称解析单个点位（原权威库检索位，现为 Nominatim 解析）。

    解析结果必须过名称相似度门槛：外部地理编码对任意查询都会尽力返回，
    不设门槛会把「不存在的地点」错误锚定到无关坐标。
    """
    row = get_poi_detail(city, name)
    if row and same_entity(str(name or ""), str(row.get("name") or "")):
        return [row]
    return []


@traced("tool", "poi.find_nearby")
def find_nearby_pois(
    city: str,
    name: str | None = None,
    latitude: float | None = None,
    longitude: float | None = None,
    limit: int = 5,
    radius_m: int | None = None,
    category: str | None = None,
) -> list[dict]:
    """外部地点层的同城近邻（OTM 半径检索，真实坐标）。

    坐标优先使用显式传入值；仅给名称时经 Nominatim 解析锚点，并把锚点自身从
    结果中排除（"西湖附近"不应包含西湖）。锚点解析不到时返回空列表——
    **不伪造"附近推荐"**。
    """
    if latitude is None or longitude is None:
        anchor = get_poi_detail(city, str(name or ""))
        if not anchor:
            return []
        latitude = anchor.get("latitude")
        longitude = anchor.get("longitude")
        if latitude in (None, 0.0) or longitude in (None, 0.0):
            return []
    kinds = ",".join(_OTM_KINDS_BY_CATEGORY.get(category or "attraction", ()))
    rows = places.search_places_near(
        float(latitude),
        float(longitude),
        city=city,
        kinds=kinds or None,
        category=category or "attraction",
        limit=limit + 4,
        radius_m=radius_m,
    )
    out = [
        _as_candidate(row, city) for row in rows if not (name and same_entity(str(name), str(row.get("name") or "")))
    ]
    for row in out:
        # 契约键：PoiNearbyItem._distance_m（wire 键固定，见 schemas.agent_ops）
        if row.get("dist_m") is not None:
            row["_distance_m"] = row.pop("dist_m")
    return out[:limit]


@traced("tool", "poi.get_consumption")
def get_consumption(city: str) -> dict | None:
    return city_reference.get_city_consumption(city)


def workbench_search(city: str, keywords: str = "", category: str | None = None, limit: int = 30) -> list[dict]:
    """加点工作台检索：OTM 分类池 + 联网补池，按键词过滤（名称/简介/地址/kinds）。"""
    cat = category or "attraction"
    if cat == "attraction":
        pool = search_attractions(city, [], limit)
    elif cat == "food":
        pool = search_foods(city, limit)
    elif cat == "hotel":
        pool = search_hotels(city, limit)
    else:
        pool = _merge_by_name(_otm_pool(city, cat, limit), _web_pool(city, cat, limit))[:limit]
    kw = str(keywords or "").strip().lower()
    if not kw:
        return pool[:limit]
    return [
        row
        for row in pool
        if kw in " ".join(str(row.get(key) or "") for key in ("name", "intro", "address", "kinds")).lower()
    ][:limit]


# 图片通道（G-3.2 外部调用基类）：超时 8s、响应上限 256KB、成功缓存 6h、
# 负结果 5min（图库偶尔限流时要能较快重试）；后台车道节流。
_image_client: ExternalClient = ExternalClient(
    name="poi_image",
    ttl_seconds=6 * 3600,
    negative_ttl_seconds=300,
    max_response_bytes=256 * 1024,
    timeout_seconds=8,
)


_wiki_client: ExternalClient = ExternalClient(
    name="poi_image_wiki",
    ttl_seconds=6 * 3600,
    negative_ttl_seconds=300,
    max_response_bytes=256 * 1024,
    timeout_seconds=4,
)


def _unsplash_image(name: str) -> str | None:
    """从 Unsplash 检索 POI 实景照片。查询词只准用点位名——纪律与两份实现为何并存，
    统一记在 services/poi_photo.py 的模块 docstring（单一说明处，不在此复述）。
    """
    query = str(name or "").strip()
    key = ExternalClient.resolve_key(settings.unsplash_access_key)
    if not query or not key:
        return None

    def _load() -> str | None:
        payload = fetch_json(
            _image_client,
            image_client(),
            "https://api.unsplash.com/search/photos",
            params={"query": query[:60], "per_page": 1, "orientation": "landscape", "content_filter": "high"},
            headers={"Authorization": f"Client-ID {key}"},
        )
        results = (payload or {}).get("results") if isinstance(payload, dict) else None
        if results and isinstance(results[0], dict):
            return (results[0].get("urls") or {}).get("regular") or (results[0].get("urls") or {}).get("full")
        return None

    return _image_client.call(f"unsplash:{query[:60]}", _load, lane=BACKGROUND)


def _wikipedia_image(name: str) -> str | None:
    """按名称从中文维基百科检索真实照片（知名景点覆盖好，餐厅等可能无图）。"""
    title = re.sub(r"[（(][^（）()]*[)）]", "", name or "").strip()
    if not title:
        return None

    def _load() -> str | None:
        payload = fetch_json(
            _wiki_client,
            image_client(),
            "https://zh.wikipedia.org/w/api.php",
            params={
                "action": "query",
                "generator": "search",
                "gsrsearch": title,
                "gsrlimit": "1",
                "prop": "pageimages",
                "piprop": "thumbnail",
                "pithumbsize": "500",
                "format": "json",
            },
            headers={"User-Agent": contact_user_agent()},  # BESEC-2：UA 单一工厂
        )
        query = payload.get("query") if isinstance(payload, dict) else None
        pages = (query or {}).get("pages") or {}
        for page in pages.values():
            thumb = (page.get("thumbnail") or {}).get("source")
            if thumb:
                return thumb
        return None

    return _wiki_client.call(f"wiki:{title}", _load, lane=BACKGROUND)


def poi_image(name: str | None, city: str) -> str | None:
    """取 POI 图片：优先维基百科真实照片，其次 Unsplash 实景图。

    都取不到时返回 None（**不再回退高德实拍**）——前端拿到 404 后落本地分类
    占位图，比一张错的地图截图更诚实。

    缓存只有 ExternalClient 那一层（自带 TTL 与负结果短 TTL），不再叠进程内 dict。
    """
    if not name:
        return None
    url: str | None = None
    if settings.poi_image_wiki:
        url = _wikipedia_image(name)
    return url or _unsplash_image(name)


def attach_poi_images(plan: list[dict], city: str) -> list[dict]:
    """为行程项补充 POI 图片：仅对景点/餐饮且尚未有 image 的项走维基/图库检索。"""
    for day in plan:
        for item in day.get("items") or []:
            if item.get("item_type") in ("attraction", "food") and not item.get("image"):
                item["image"] = poi_image(item.get("poi_name"), city)
    return plan
