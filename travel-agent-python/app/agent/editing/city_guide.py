"""城市引导：管家式自然对话，省份/模糊输入自动推荐省域内热门城市。"""

import json
import logging

from app.common.llm_client import get_llm_client

logger = logging.getLogger(__name__)


def run_city_guide(req: dict) -> dict:
    client = get_llm_client()
    system = (
        "你是一位见多识广的旅行管家，正在和用户聊天帮他确定目的地。"
        "用户可能输入省份、区域、模糊想法或具体城市。你的任务："
        "1. 用户明确想去某个具体城市（无论是否在热门缓存里，如「我想去绍兴」）→ kind=city，"
        "city 填该城市名，message 用一句话确认，suggestions 留空，不要反问；"
        "2. 省份/区域/模糊想法 → 严格在该省/区域（模糊时按意图最近的省）内推荐：kind=city，"
        "city 填该省最热门的旅游城市（西藏→拉萨/日喀则；云南→丽江/大理/昆明；新疆→乌鲁木齐/喀什；"
        "福建→厦门/福州/泉州；浙江→杭州/宁波/乌镇所在的嘉兴；广东→广州/深圳/汕头等；任何省都能给出省会或顶级旅游城市）；"
        "若用户输入本身就是具体城市（kind=city, city 与输入一致）也归此路径。"
        "message 简短说明为什么选这里，结尾加一句「想换成其他城市可以点「AI 帮我选」手动调整」；"
        "suggestions 给 1~2 个省域内备选城市 {name,reason} 供改选；"
        "3. 实在无法判断（如输入完全无意义）→ kind=unclear，友好地请TA说更多。"
        "语气要求：自然、热情、有画面感；禁止出现「支持列表」「暂未开通」「服务」这类系统腔；"
        "不要跨省硬塞；不要重复同一个城市；推荐词用「我更推荐」「尤其适合你」这种管家口吻。"
        '只输出 JSON：{"kind":"province|city|unclear","city":null或规范城市名,'
        '"message":"2~3句自然对话","suggestions":[{"name":"城市","reason":"一句话卖点"}]}'
    )
    messages = [{"role": "system", "content": system}]
    for h in (req.get("history") or [])[-8:]:
        role = "user" if h.get("role") == "user" else "assistant"
        if h.get("content"):
            messages.append({"role": role, "content": str(h["content"])[:600]})
    messages.append(
        {
            "role": "user",
            "content": f"用户输入：{req.get('input')}",
        }
    )
    raw = client.chat(messages, temperature=0.5, max_tokens=800)
    text = raw.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1].rsplit("```", 1)[0]
    try:
        data = json.loads(text[text.find("{") : text.rfind("}") + 1])
    except (json.JSONDecodeError, ValueError):
        # 模型输出无花括号/截断时切片为空串会抛 JSONDecodeError；按"意图不明"
        # 的引导语义降级，而不是让异常冒泡成 API 500。
        return {"kind": "unclear", "city": None, "message": "想去哪里玩？说说你的想法～", "suggestions": []}
    if not isinstance(data, dict):
        return {"kind": "unclear", "city": None, "message": "想去哪里玩？说说你的想法～", "suggestions": []}

    suggestions = []
    for s in data.get("suggestions") or []:
        if isinstance(s, dict) and s.get("name"):
            suggestions.append({"name": str(s["name"]), "reason": str(s.get("reason") or "")[:60]})
        elif isinstance(s, str):
            suggestions.append({"name": s, "reason": ""})

    city = data.get("city")
    kind = str(data.get("kind") or "unclear")
    if city and kind in {"province", "city"}:
        kind = "city"
    return {
        "kind": kind,
        "city": city,
        "message": str(data.get("message") or "想去哪里玩？说说你的想法～"),
        "suggestions": suggestions[:3],
    }
