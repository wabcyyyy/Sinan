"""研究 Agent 的 LLM 推理：检索规划 + 证据充分性评估（阶段二）。

规划（plan_research）：沿用规范化任务中的偏好与规模，不额外调用模型；
评估（evaluate_research）：判断实际检索证据是否
足够，不足时给出补充检索词，驱动子图最多再补一轮。

设计要点：
- 查询参数来自用户需求；模型只评估实际候选并建议缺口补查，绝不生成行程内容；
- 评估未配置/失败时保留未验证状态；取消与失权继续向上传播；
- 证据为空时确定性判定"不足"并给默认补充词，省一次模型调用；
- 统一走可配置 fast 通道（llm_fast_model）；默认与主模型同款（config.py），
  切档才省钱——模块级函数可被单测 patch 注入 fixture。
"""

from __future__ import annotations

import json
import logging

from app.agent.core.json_utils import parse_llm_json_or_none
from app.agent.research.evidence import ResearchTask
from app.agent.runtime.run_limits import TaskStopped
from app.common.llm_client import get_llm_client
from app.common.model_registry import configured

logger = logging.getLogger(__name__)

_EVAL_SYSTEM = (
    "你是旅行信息评估员。判断检索结果是否满足研究任务（数量、与偏好/预算/档位的匹配度），"
    "只输出 JSON："
    '{"sufficient":true或false或null,"reason":"一句话","extra_keywords":["不足时建议补充的检索词(可空数组)"]}。'
    "候选缺少判断所需字段时返回 null，不能凭地点名称猜测价格、档次或偏好匹配。"
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
    """直接使用已规范化任务；不再为每个域额外调用模型改写检索参数。

    意图词及必去名由检索节点统一追加，避免模型扩词重复消耗检索额度。
    模型仍评估实际检索结果，存在缺口时有界补查。
    """
    return {"preferences": list(task.preferences or []), "extra_keywords": [], "limit": task.limit}


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
    """评估证据充分性；无法判断时 sufficient=None，保留原因且不增加补查。

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
        return {"sufficient": None, "extra_keywords": [], "reason": "未配置研究评估模型，证据充分性未验证"}
    fields = ("name", "category", "ticket_price", "avg_cost", "tags", "remark", "dietary_options", "category_tags")
    candidates = [{key: p[key] for key in fields if p.get(key) is not None} for p in items[:12]]
    try:
        raw = get_llm_client("fast").complete(
            f"研究域={task.domain}；城市={task.city}；任务要求：偏好={task.preferences}，"
            f"预算={task.budget}，酒店档位={task.hotel_tier}。\n"
            f"第 {round_no} 轮检索到 {len(items)} 条：{json.dumps(candidates, ensure_ascii=False)}。"
            "判断是否足够，不足请给出补充检索词。",
            system_prompt=_EVAL_SYSTEM,
            temperature=0.1,
            max_tokens=240,
            json_mode=True,
        )
        data = parse_llm_json_or_none(raw) or {}
        sufficient = data.get("sufficient")
        if not isinstance(sufficient, bool):
            return {"sufficient": None, "extra_keywords": [], "reason": "研究评估未给出有效结论，证据充分性未验证"}
        return {
            "sufficient": sufficient,
            "extra_keywords": [str(x) for x in (data.get("extra_keywords") or []) if x],
        }
    except TaskStopped:
        raise
    except Exception as exc:
        logger.warning("research evaluate failed for %s/%s: %s", task.domain, task.city, exc)
        return {"sufficient": None, "extra_keywords": [], "reason": "研究评估失败，证据充分性未验证"}
