"""加点工作台的地点检索（服务层）：OTM 分类池 + 联网补池，无本地语料。

与 Agent 检索的关系：同一份 `app.agent.tools.impl.workbench_search`（OTM 半径池 +
联网搜索补池）——「工作台能加的点位」与「生成时能引用的点位」天然同源，不分叉。

空态如实：`coveredCities` 来自 city_geo 字典（supported-cities 同源），前端据此
区分「该城受支持但关键词没命中」与「该城不受支持」——不假装是「无结果」。
count 已无本地语料可数，恒为 null（字段保留是为了前端契约兼容）。
"""

from __future__ import annotations

from app.agent import workbench_search
from app.services import itinerary_city, llm_gateway_service

CATEGORY_LABELS: dict[str, str] = {"attraction": "景点", "food": "餐饮", "hotel": "住宿"}
MAX_LIMIT = 30


def _to_vo(row: dict) -> dict:
    ticket = row.get("ticket_price")
    avg = row.get("avg_cost")
    cost = ticket if ticket is not None else avg
    return {
        "id": row.get("id"),
        "name": row.get("name"),
        "category": row.get("category"),
        "address": row.get("address"),
        "latitude": row.get("latitude"),
        "longitude": row.get("longitude"),
        "rating": row.get("rating"),
        "tags": row.get("kinds"),
        "cost": cost,
        "source": row.get("source"),
    }


def search_local(user_id: int, city: str, keywords: str = "", category: str | None = None) -> dict:
    """加点工作台检索。user_id（BYOK 路由）：workbench_search → web_search 补池烧
    联网搜索 LLM，路由上下文随调用进入；配额闸在端点层（/api/pois 先过
    enforce_llm_budget 再进来，app/api/business/pois.py）。
    """
    with llm_gateway_service.route_scope(user_id):
        rows = workbench_search(city, keywords=keywords, category=category, limit=MAX_LIMIT)
    return {
        "city": city,
        "category": category,
        "items": [_to_vo(row) for row in rows],
        "coveredCities": [{"city": name, "count": None} for name in itinerary_city.supported_cities()],
    }
