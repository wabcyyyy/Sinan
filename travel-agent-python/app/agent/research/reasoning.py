"""研究 Agent 的 LLM 推理：检索规划 + 证据充分性评估（阶段二）。

规划（plan_research）：根据研究任务卡输出检索策略——扩展偏好、补充自由关键词
（如具体景点/菜系/酒店品牌）、检索规模；评估（evaluate_research）：判断证据是否
足够，不足时给出补充检索词，驱动子图最多再补一轮。

设计要点：
- LLM-only 口径不破：规划/评估只决定"查什么、够不够"，绝不生成行程内容；
- 任何异常（未配置 LLM / 网络 / 解析失败）都回退到确定性默认值，绝不阻塞研究；
- 证据为空时确定性判定"不足"并给默认补充词，省一次模型调用；
- 统一走可配置 fast 通道（llm_fast_model）；默认与主模型同款（config.py），
  切档才省钱——模块级函数可被单测 patch 注入 fixture。
"""

from __future__ import annotations

import json
import logging

from app.agent.core.json_utils import parse_llm_json_or_none
from app.agent.research.evidence import ResearchTask
from app.common.llm_client import get_llm_client
from app.common.model_registry import configured, model_for

logger = logging.getLogger(__name__)

_PLAN_SYSTEM = (
    "你是旅行信息研究员。根据研究任务输出检索计划 JSON，只规划检索不生成行程："
    '{"reasoning":"一句话说明检索思路","preferences":["扩展的偏好关键词(可空数组)"],'
    '"extra_keywords":["补充自由检索词(可空数组，如具体景点名/菜系/酒店品牌)"],'
    '"limit":数字(检索规模)}。字段缺失给 null。'
)

_EVAL_SYSTEM = (
    "你是旅行信息评估员。判断检索结果是否满足研究任务（数量、与偏好/预算/档位的匹配度），"
    "只输出 JSON："
    '{"sufficient":true或false,"reason":"一句话","extra_keywords":["不足时建议补充的检索词(可空数组)"]}。'
    "只评估证据充分性，不生成行程。"
)


# 证据为空时确定性补查的默认关键词（各域兜底，避免空结果直接无证据交付）
def _default_extra_keywords(task: ResearchTask) -> list[str]:
    if task.domain == "hotel":
        return [f"{task.city} 酒店"]
    if task.domain == "food":
        return [f"{task.city} 美食", f"{task.city} 特色餐厅"]
    prefs = [str(p) for p in (task.preferences or []) if p]
    return [f"{task.city} {p}" for p in prefs] or [f"{task.city} 景点"]


def plan_research(task: ResearchTask) -> dict:
    """输出检索计划 {preferences, extra_keywords, limit}；失败/未配置 LLM 返回空计划。

    空计划等价于阶段一的确定性检索参数，保证降级链路与旧行为一致。
    """
    if not configured("main"):
        return {}
    try:
        raw = get_llm_client().complete(
            f"研究任务：域={task.domain}，城市={task.city}，偏好={task.preferences}，"
            f"预算={task.budget}，酒店档位={task.hotel_tier}。请规划检索策略。",
            system_prompt=_PLAN_SYSTEM,
            temperature=0.2,
            max_tokens=300,
            model=model_for("fast"),
            json_mode=True,
        )
        data = parse_llm_json_or_none(raw) or {}
        return {
            "preferences": [str(x) for x in (data.get("preferences") or []) if x],
            "extra_keywords": [str(x) for x in (data.get("extra_keywords") or []) if x],
            "limit": int(data.get("limit") or 0) or 0,
        }
    except Exception as exc:
        logger.warning("research plan failed for %s/%s: %s", task.domain, task.city, exc)
        return {}


def _intent_keywords_covered(task: ResearchTask, items: list[dict]) -> bool:
    """确定性检查：任一证据 item 的 name/remark/tags 命中任一意图关键词（子串/casefold）。

    tags 可能是 list，str() 后子串匹配同样生效；无意图关键词视为已覆盖。
    """
    for poi in items:
        if not isinstance(poi, dict):
            continue
        haystack = " ".join(str(poi.get(key) or "") for key in ("name", "remark", "tags"))
        folded = haystack.casefold()
        if any(kw and str(kw).casefold() in folded for kw in task.intent_keywords):
            return True
    return False


def evaluate_research(task: ResearchTask, items: list[dict], round_no: int) -> dict:
    """评估证据充分性 {sufficient, extra_keywords}；异常按充分处理（不无限补查）。

    证据为空时确定性返回"不足"+默认补充词（省一次模型调用）；第 1 轮且任务卡
    携带意图关键词时，先做确定性的「意图关键词覆盖」检查——证据完全未命中任何
    意图词即前置短路判定不足（补充词=意图词∪域默认词），省一次模型调用；
    LLM 判定仅在结果非空、未被短路且已配置模型时发生。
    """
    if not items:
        return {"sufficient": False, "extra_keywords": _default_extra_keywords(task)}
    # M3-②（AD5）：意图覆盖确定性维度前置短路（round 1 且意图词非空时生效）
    if int(round_no) == 1 and task.intent_keywords and not _intent_keywords_covered(task, items):
        merged = list(dict.fromkeys(list(task.intent_keywords) + _default_extra_keywords(task)))
        return {"sufficient": False, "extra_keywords": merged}
    if not configured("main"):
        return {"sufficient": True, "extra_keywords": []}
    names = [str(p.get("name") or "") for p in items[:12]]
    try:
        raw = get_llm_client().complete(
            f"研究域={task.domain}；城市={task.city}；任务要求：偏好={task.preferences}，"
            f"预算={task.budget}，酒店档位={task.hotel_tier}。\n"
            f"第 {round_no} 轮检索到 {len(items)} 条：{json.dumps(names, ensure_ascii=False)}。"
            "判断是否足够，不足请给出补充检索词。",
            system_prompt=_EVAL_SYSTEM,
            temperature=0.1,
            max_tokens=240,
            model=model_for("fast"),
            json_mode=True,
        )
        data = parse_llm_json_or_none(raw) or {}
        return {
            "sufficient": bool(data.get("sufficient", True)),
            "extra_keywords": [str(x) for x in (data.get("extra_keywords") or []) if x],
        }
    except Exception as exc:
        logger.warning("research evaluate failed for %s/%s: %s", task.domain, task.city, exc)
        return {"sufficient": True, "extra_keywords": []}
