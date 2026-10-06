"""受控工具目录：登记层（G-2.6 按域拆包，机制见 core.py）。

导入本模块即完成全部工具登记（handlers + register 调用都在这里）。
`app/agent/tools/registry/__init__.py` 先导入 core 再导入本模块，因此任何
`from app.agent.tools.registry import registry` 拿到的都是登记完整的单例。

handler 一律调用期经 tools 模块属性解析真实函数（`tools.xxx(...)` 晚绑定），
mock.patch.object 零修改生效——**不得**在注册期绑定函数对象。
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from typing import Any

from app.agent.tools.registry.core import ToolSpec, registry
from app.common.addons import addons
from app.common.config import settings
from app.schemas.trip import MAX_TRIP_DAYS


def _search_pois(**params: Any) -> dict[str, Any]:
    # 运行时导入，确保测试 monkeypatch 和应用热更新仍作用于真实工具函数。
    from app.agent.tools import impl as tools

    city = params["city"]
    # 景点和餐饮检索彼此独立；并行执行可把高德/RAG 的等待从串行叠加
    # 降为单次最长等待。两者仍由同一个只读 Registry 工具统一审计。
    # worker 数调用期读 settings（PR-11 并发配置化，原硬编码 3）。
    with ThreadPoolExecutor(max_workers=settings.poi_search_workers, thread_name_prefix="poi-search") as pool:
        # 内层线程不继承 contextvars（R3-F1）：不 copy 的话 worker 里 current_limits()=None
        # （检索被放行不记账）、trace recorder=None（事件静默丢）。提交点（主线程）逐
        # submit 各 copy 一份——并发下同一 Context.run 不可重入；姿势同 research/supervisor。
        attractions_future = pool.submit(
            copy_context().run, tools.search_attractions, city, params.get("preferences", []), params.get("limit", 30)
        )
        foods_future = pool.submit(copy_context().run, tools.search_foods, city, params.get("food_limit", 10))
        consumption_future = pool.submit(copy_context().run, tools.get_consumption, city)
        return {
            "attractions": attractions_future.result(),
            "foods": foods_future.result(),
            "consumption": consumption_future.result(),
        }


def _search_hotel_options(**params: Any) -> list[dict]:
    from app.agent.tools import impl as tools

    return tools.search_hotels(params["city"], params.get("limit", 6))


def _get_route_matrix(**params: Any) -> dict:
    from app.agent.data.route_service import get_route_matrix

    return get_route_matrix(params["items"], mode=params.get("mode", settings.route_mode))


def _find_nearby_pois(**params: Any) -> list[dict]:
    from app.agent.tools.impl import find_nearby_pois

    return find_nearby_pois(
        params["city"],
        name=params.get("name"),
        latitude=params.get("latitude"),
        longitude=params.get("longitude"),
        limit=params.get("limit", 5),
        radius_m=params.get("radius_m"),
        category=params.get("category"),
    )


# ---- G-1.4：tools.py 全部工具函数入册（handler 一律调用期经 tools 模块属性
# 解析真实函数——`tools.xxx(...)` 晚绑定，mock.patch.object 零修改生效）----


def _search_local_poi_handler(**params: Any) -> list[dict]:
    from app.agent.tools import impl as tools

    return tools.search_local_poi(params["city"], params["name"], category=params.get("category"))


def _search_attractions_handler(**params: Any) -> list[dict]:
    from app.agent.tools import impl as tools

    return tools.search_attractions(params["city"], params.get("preferences") or [], params.get("limit", 30))


def _search_foods_handler(**params: Any) -> list[dict]:
    from app.agent.tools import impl as tools

    return tools.search_foods(params["city"], params.get("limit", 10))


def _search_hotels_handler(**params: Any) -> list[dict]:
    from app.agent.tools import impl as tools

    return tools.search_hotels(params["city"], params.get("limit", 6))


def _get_poi_detail_handler(**params: Any) -> dict | None:
    from app.agent.tools import impl as tools

    return tools.get_poi_detail(params["city"], params["name"])


def _get_consumption_handler(**params: Any) -> dict | None:
    from app.agent.tools import impl as tools

    return tools.get_consumption(params["city"])


def _search_flight_quotes_handler(**params: Any) -> list[dict]:
    # 航班报价自成模块（tools/flight_quotes.py，规模棘轮拆出）；晚绑定语义与
    # `tools.xxx` 相同——调用期读模块属性，patch.object(flight_quotes, ...) 生效
    from app.agent.tools import flight_quotes

    return flight_quotes.search_flight_quotes(
        params["origin_city"],
        params["city"],
        params.get("start_date"),
        params.get("end_date"),
        params.get("limit", 3),
    )


def _poi_image_handler(**params: Any) -> str | None:
    from app.agent.tools import impl as tools

    return tools.poi_image(params.get("name"), params["city"])


def _attach_poi_images_handler(**params: Any) -> list[dict]:
    from app.agent.tools import impl as tools

    return tools.attach_poi_images(params["plan"], params["city"])


def _web_search_places_handler(**params: Any) -> list[dict]:
    # INV-9：外部调用必须有超时与响应上限——search_places_via_web 走 llm_client
    # （自带超时/上限/预算检查 _check_budget），非裸 httpx。
    from app.agent.data.web_search import search_places_via_web

    return search_places_via_web(
        params["city"],
        params["category"],
        limit=params.get("limit", 4),
        budget_tier=params.get("budget_tier"),
        intent_keywords=params.get("intent_keywords") or [],
    )


registry.register(
    ToolSpec(
        name="search_pois",
        version="1.0",
        description="检索目的地景点、餐饮、酒店和消费信息",
        parameters={
            "type": "object",
            "properties": {
                "city": {"type": "string"},
                "preferences": {"type": "array"},
                "limit": {"type": "integer"},
                "food_limit": {"type": "integer"},
                "hotel_limit": {"type": "integer"},
            },
            "required": ["city"],
            "additionalProperties": False,
        },
        read_only=True,
        risk_level="low",
        timeout_seconds=15,
        max_calls=2,
        retry_policy={"max_retries": 1},
        requires_confirmation=False,
        idempotent=True,
        handler=_search_pois,
    )
)
registry.register(
    ToolSpec(
        name="search_hotel_options",
        version="1.0",
        description="检索可枚举的酒店候选",
        parameters={
            "type": "object",
            "properties": {"city": {"type": "string"}, "limit": {"type": "integer"}},
            "required": ["city"],
            "additionalProperties": False,
        },
        read_only=True,
        risk_level="low",
        timeout_seconds=10,
        max_calls=3,
        retry_policy={"max_retries": 1},
        requires_confirmation=False,
        idempotent=True,
        handler=_search_hotel_options,
    )
)
registry.register(
    ToolSpec(
        name="get_route_matrix",
        version="1.0",
        description="获取同日地点之间的交通时间矩阵",
        parameters={
            "type": "object",
            "properties": {"items": {"type": "array"}, "mode": {"type": "string"}},
            "required": ["items"],
            "additionalProperties": False,
        },
        read_only=True,
        # 逐日构建矩阵：最长行程（MAX_TRIP_DAYS 天）在"校验→修复"循环下最多
        # 被调用 4 轮（多日：reflect 2 次 + format 1 次；单日：3 次 + format 1 次），
        # 预算必须覆盖该最坏情况，否则开启路线服务后 4 天以上行程必然超预算报错。
        risk_level="low",
        timeout_seconds=8,
        max_calls=4 * MAX_TRIP_DAYS,
        retry_policy={"max_retries": 0},
        requires_confirmation=False,
        idempotent=True,
        handler=_get_route_matrix,
    )
)
registry.register(
    ToolSpec(
        name="find_nearby_pois",
        version="1.0",
        description="在权威知识库中查找给定坐标或地点名称附近（同城）的真实 POI 近邻",
        parameters={
            "type": "object",
            "properties": {
                "city": {"type": "string"},
                "name": {"type": "string"},
                "latitude": {"type": "number"},
                "longitude": {"type": "number"},
                "limit": {"type": "integer"},
                "radius_m": {"type": "integer"},
                "category": {"type": "string"},
            },
            "required": ["city"],
            "additionalProperties": False,
        },
        read_only=True,
        risk_level="low",
        timeout_seconds=5,
        max_calls=8,
        retry_policy={"max_retries": 0},
        requires_confirmation=False,
        idempotent=True,
        handler=_find_nearby_pois,
    )
)


registry.register(
    ToolSpec(
        name="search_local_poi",
        version="1.0",
        description="按名称在权威知识库解析单个本地 POI（补查/grounding 用）",
        parameters={
            "type": "object",
            "properties": {"city": {"type": "string"}, "name": {"type": "string"}, "category": {"type": "string"}},
            "required": ["city", "name"],
            "additionalProperties": False,
        },
        read_only=True,
        risk_level="low",
        timeout_seconds=5,
        max_calls=16,
        retry_policy={"max_retries": 0},
        requires_confirmation=False,
        idempotent=True,
        handler=_search_local_poi_handler,
    )
)
registry.register(
    ToolSpec(
        name="search_attractions",
        version="1.0",
        description="检索目的地景点候选（RAG 向量召回 + 权威库回退）",
        parameters={
            "type": "object",
            "properties": {"city": {"type": "string"}, "preferences": {"type": "array"}, "limit": {"type": "integer"}},
            "required": ["city"],
            "additionalProperties": False,
        },
        read_only=True,
        risk_level="low",
        timeout_seconds=15,
        max_calls=8,
        retry_policy={"max_retries": 0},
        requires_confirmation=False,
        idempotent=True,
        handler=_search_attractions_handler,
    )
)
registry.register(
    ToolSpec(
        name="search_foods",
        version="1.0",
        description="检索目的地餐饮候选",
        parameters={
            "type": "object",
            "properties": {"city": {"type": "string"}, "limit": {"type": "integer"}},
            "required": ["city"],
            "additionalProperties": False,
        },
        read_only=True,
        risk_level="low",
        timeout_seconds=10,
        max_calls=8,
        retry_policy={"max_retries": 0},
        requires_confirmation=False,
        idempotent=True,
        handler=_search_foods_handler,
    )
)
registry.register(
    ToolSpec(
        name="search_hotels",
        version="1.0",
        description="检索目的地酒店候选（保留完整可枚举集）",
        parameters={
            "type": "object",
            "properties": {"city": {"type": "string"}, "limit": {"type": "integer"}},
            "required": ["city"],
            "additionalProperties": False,
        },
        read_only=True,
        risk_level="low",
        timeout_seconds=10,
        max_calls=8,
        retry_policy={"max_retries": 0},
        requires_confirmation=False,
        idempotent=True,
        handler=_search_hotels_handler,
    )
)
registry.register(
    ToolSpec(
        name="get_poi_detail",
        version="1.0",
        description="查询单个 POI 的知识库详情",
        parameters={
            "type": "object",
            "properties": {"city": {"type": "string"}, "name": {"type": "string"}},
            "required": ["city", "name"],
            "additionalProperties": False,
        },
        read_only=True,
        max_calls=8,
        risk_level="low",
        timeout_seconds=5,
        retry_policy={"max_retries": 0},
        requires_confirmation=False,
        idempotent=True,
        handler=_get_poi_detail_handler,
    )
)
registry.register(
    ToolSpec(
        name="get_consumption",
        version="1.0",
        description="查询目的地人均消费水位",
        parameters={
            "type": "object",
            "properties": {"city": {"type": "string"}},
            "required": ["city"],
            "additionalProperties": False,
        },
        max_calls=8,
        read_only=True,
        risk_level="low",
        timeout_seconds=5,
        retry_policy={"max_retries": 0},
        requires_confirmation=False,
        idempotent=True,
        handler=_get_consumption_handler,
    )
)
registry.register(
    ToolSpec(
        name="poi_image",
        version="1.0",
        description="查询单个 POI 的配图 URL（本地快照优先）",
        parameters={
            "type": "object",
            "properties": {"name": {"type": "string"}, "city": {"type": "string"}},
            "required": ["city"],
            "additionalProperties": False,
        },
        read_only=True,
        risk_level="low",
        timeout_seconds=10,
        max_calls=16,
        retry_policy={"max_retries": 0},
        requires_confirmation=False,
        idempotent=True,
        handler=_poi_image_handler,
    )
)
registry.register(
    ToolSpec(
        name="attach_poi_images",
        version="1.0",
        description="为整份行程点位批量补图",
        parameters={
            "type": "object",
            "properties": {"plan": {"type": "array"}, "city": {"type": "string"}},
            "required": ["plan", "city"],
            "additionalProperties": False,
        },
        read_only=True,
        risk_level="low",
        timeout_seconds=15,
        max_calls=4,
        retry_policy={"max_retries": 0},
        requires_confirmation=False,
        idempotent=True,
        handler=_attach_poi_images_handler,
    )
)
registry.register(
    ToolSpec(
        name="web_search_places",
        version="1.0",
        description="联网补充真实地点名（证据不足时的补池通道；addon=web_search 门控）",
        parameters={
            "type": "object",
            "properties": {
                "city": {"type": "string"},
                "category": {"type": "string"},
                "limit": {"type": "integer"},
                "intent_keywords": {"type": "array"},
            },
            "required": ["city", "category"],
            "additionalProperties": False,
        },
        read_only=True,
        risk_level="medium",
        timeout_seconds=15,
        max_calls=6,
        retry_policy={"max_retries": 0},
        requires_confirmation=False,
        idempotent=True,
        handler=_web_search_places_handler,
        # G-1.4 的 when 钩子在此接线：addon 关闭 → 不列入工具面、invoke 报未注册
        when=lambda _ctx: addons.is_enabled("web_search"),
    )
)
registry.register(
    ToolSpec(
        name="search_flight_quotes",
        version="1.0",
        description="查询往返航班聚合报价（需出发地；未映射/无报价时如实返回空）",
        parameters={
            "type": "object",
            "properties": {
                "origin_city": {"type": "string"},
                "city": {"type": "string"},
                "start_date": {"type": "string"},
                "end_date": {"type": "string"},
                "limit": {"type": "integer"},
            },
            "required": ["origin_city", "city"],
            "additionalProperties": False,
        },
        read_only=True,
        risk_level="low",
        timeout_seconds=12,
        max_calls=2,
        # 重试只上幂等 GET，且**不叠第二层**：data 层 flight_prices 客户端已按
        # L1 纪律重试 1 次（重试放大外呼量），注册表层再加一层等于翻倍。
        retry_policy={"max_retries": 0},
        requires_confirmation=False,
        idempotent=True,
        handler=_search_flight_quotes_handler,
    )
)
