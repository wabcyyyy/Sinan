"""三域研究 Agent 的领域配置（工厂模式的差异点）。

职责：
- DomainConfig 描述一个研究域的全部差异：检索工具名、任务→检索参数映射、缺口判定；
- 检索函数**只存注册表工具名、经 registry 派发**（G-1.4），保证单测
  patch.object(tools, "search_*") 持续生效——这也是既有测试注入契约的一部分。

实现要点：
- handler 调用期才读 tools 模块属性，直接 import 函数对象会让 patch 被 import 时绑定绕开；
- 缺口判定是纯规则（演示级），阶段二升级为 LLM 评估时只替换 finalize 节点。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from app.agent.research.evidence import ResearchDomain, ResearchTask

# 各域检索工具的默认规模：景点 30 / 美食 10 / 酒店 6（酒店保留完整枚举候选集，
# limit 只影响兜底分支的截断，与 tools.search_hotels 行为一致）。
DEFAULT_LIMITS: dict[ResearchDomain, int] = {
    "attraction": 30,
    "food": 10,
    "hotel": 6,
}


def _attraction_params(task: ResearchTask) -> dict:
    return {"city": task.city, "preferences": task.preferences, "limit": task.limit or DEFAULT_LIMITS["attraction"]}


def _food_params(task: ResearchTask) -> dict:
    return {"city": task.city, "limit": task.limit or DEFAULT_LIMITS["food"]}


def _hotel_params(task: ResearchTask) -> dict:
    return {"city": task.city, "limit": task.limit or DEFAULT_LIMITS["hotel"]}


@dataclass(frozen=True)
class DomainConfig:
    domain: ResearchDomain
    tool_name: str
    params: Callable[[ResearchTask], dict]
    # 缺口判定：items 不足时的提示；返回 None 表示无缺口
    gap_hint: Callable[[list[dict]], str | None]


def _attraction_gap(items: list[dict]) -> str | None:
    return None if items else "未检索到景点候选，请模型凭知识补充真实地点"


def _food_gap(items: list[dict]) -> str | None:
    return None if items else "未检索到餐饮候选，请模型凭知识补充真实餐厅"


def _hotel_gap(items: list[dict]) -> str | None:
    if not items:
        return "未检索到酒店候选，请模型凭知识补充当地酒店"
    return None


DOMAINS: dict[ResearchDomain, DomainConfig] = {
    "attraction": DomainConfig(
        domain="attraction",
        tool_name="search_attractions",
        params=_attraction_params,
        gap_hint=_attraction_gap,
    ),
    "food": DomainConfig(
        domain="food",
        tool_name="search_foods",
        params=_food_params,
        gap_hint=_food_gap,
    ),
    "hotel": DomainConfig(
        domain="hotel",
        tool_name="search_hotels",
        params=_hotel_params,
        gap_hint=_hotel_gap,
    ),
}
