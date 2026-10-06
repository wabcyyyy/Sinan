"""整段生成工作流的**薄门面**（G-2.4 按蓝图拆分后的公开面）。

本模块只做转发与导出，不承载逻辑：
- 对外入口：run_generate（委托 trip_graph 统一图）、run_adjust（换一批）；
- 节点函数由 graph_nodes 实现、经本模块导出——trip_graph 经 ``workflow.X``
  调用，测试据此 monkeypatch，**改归属时须同步访问路径**；
- generate_open_plans 由 open_plans 实现（草案生成 / 失败重试 / 待研究降级三段）；
- format_output 由 formatting.assembly 实现；UnifiedAgentState 在 research.agent_state
  （PR-2 后全仓唯一图状态定义）。

**可 mock 契约（勿随意搬移）**：下列名字被测试经 `workflow.<name>` 注入桩，
必须在本模块命名空间可解析——settings、llm_open_day、llm_open_trip、
local_ground、generate_open_plans、以及全部节点函数。为此按「调用期解析」
语义直接绑定模块属性（`from ... import X` 后本模块自有 X）。
"""

from app.agent.generation.content.day_prompts import llm_open_trip
from app.agent.generation.orchestration.day_stream import llm_open_day
from app.agent.generation.orchestration.graph_nodes import (
    generate_itinerary,
    needs_fix,
    parse_requirements,
    reflect,
    research_pois,
    research_refill,
)
from app.agent.generation.orchestration.open_plans import generate_open_plans
from app.agent.generation.output.assembly import format_output
from app.agent.grounding.facts import local_ground
from app.agent.tools.impl import search_attractions, search_foods
from app.common.config import settings
from app.schemas.trip import AdjustRequest, AdjustResponse, GenerateRequest, GenerateResponse, PoiOption

__all__ = [
    "format_output",
    "generate_itinerary",
    "generate_open_plans",
    "llm_open_day",
    "llm_open_trip",
    "local_ground",
    "needs_fix",
    "parse_requirements",
    "reflect",
    "research_pois",
    "research_refill",
    "run_adjust",
    "run_generate",
    "search_attractions",
    "search_foods",
    "settings",
]


def run_generate(req: GenerateRequest) -> GenerateResponse:
    from app.agent.generation.orchestration.trip_graph import run_trip

    return run_trip(req)


def run_adjust(req: AdjustRequest) -> AdjustResponse:
    if req.item_type == "attraction":
        candidates = search_attractions(req.city, req.preferences)
    else:
        candidates = search_foods(req.city)
    seen = set()
    recommendations: list[PoiOption] = []
    for poi in candidates:
        name = poi.get("name")
        if not name or name == req.poi_name or name in seen:
            continue
        seen.add(name)
        # POI 行主键是 "id"，PoiOption 字段是 poi_id；直接 **poi 会因键名
        # 不匹配把 poi_id 静默丢成 None（前端"换一批"无法定位）。
        option_row = {**poi, "poi_id": poi.get("poi_id", poi.get("id"))}
        recommendations.append(PoiOption(**option_row))
        if len(recommendations) >= 5:
            break
    return AdjustResponse(city=req.city, current=req.poi_name, recommendations=recommendations)
