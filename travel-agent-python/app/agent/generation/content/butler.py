"""管家式讲解与景点详细介绍生成。

景点介绍（poi_intros）使用受限 Function Calling Loop：
模型可调用 Tool Registry 只读工具（search_pois）补充事实后再写介绍，
避免纯凭空编造；写入型工具不在 Registry 中，不可被调用。
"""

import json
import logging

from app.agent.tools.function_calling import FunctionCallingError, run_tool_call_loop
from app.common.llm_client import get_llm_client
from app.common.model_registry import model_for

logger = logging.getLogger(__name__)


def run_butler_note(req: dict) -> str:
    client = get_llm_client()
    preferences = "、".join(req.get("preferences") or []) or "无特别偏好"
    system = (
        "你是一对一专属旅游管家。行程已按用户偏好与特别要求安排完毕，请用管家的口吻向用户讲解你的安排思路，"
        "固定写成四段结构（400~600 字，段与段之间空一行）："
        "第一段讲这趟旅行的意图如何贯穿全程（若用户最初输入的是省份，先自然说明为什么首选该城市，"
        "并提到想去省内其他城市可随时调整；特别要求如何落实也并入本段）；"
        "第二段讲酒店选址与旅行意图的关系；第三段讲预算怎么花、哪里可以省；"
        "第四段给出出发前 1~2 条必做事项。"
        "第三段涉及金额时，只能引用下方「预算」字段与行程 items 里的 cost 数字，"
        "禁止自行编造或估算任何具体金额；预算数据未提供时不写具体数字，只谈取舍思路。"
        "亲切专业；当用户输入包含旅行意图与实际行程时，四段须与上述结构逐段对应、结合具体点位展开。"
        "不要用 markdown 标题符号，不要输出字面的反斜杠n或转义符。"
    )
    user = (
        f"用户最初输入的区域：{req.get('region_hint') or '同目的地'}。\n"
        f"用户旅行意图：{req.get('intent') or '未填写'}\n"
        f"用户画像：{req.get('city')} {req.get('days')} 天 {req.get('persons')} 人，"
        f"偏好：{preferences}，住宿档次：{req.get('hotel_tier') or '未指定'}，"
        f"预算：{req.get('budget') or '未设'}\n"
        f"用户额外要求：{req.get('requirements') or '无'}\n"
        f"最终行程：{json.dumps(req.get('plans') or [], ensure_ascii=False)}\n"
        f"系统校验日志：{json.dumps(req.get('validation_log') or [], ensure_ascii=False)}"
    )
    raw = client.complete(user, system_prompt=system, temperature=0.5, max_tokens=1200)
    note = raw.strip().replace("\\n", "\n")
    if not note:
        raise ValueError("butler note empty")
    return note


def run_poi_intros(city: str, names: list[str], intent: str | None = None) -> dict:
    """为行程内景点写介绍；允许模型通过 Function Calling 检索只读 POI 事实。

    契约：最终仍只输出 `{intros: {name: text}}`；工具结果只作为中间证据，
    不写库、不改行程。FC 失败时回退为无工具的单轮 complete（保持可用性）。
    intent 非空时要求每段介绍带一句与旅行意图的连接；为空时写口碑/地理理由。
    """
    client = get_llm_client()
    # 意图连接句规则随 intent 有无切换（无 intent 时降级为口碑/地理理由）
    link_rule = (
        f"每段末尾用一句话点出该地点与本趟旅行意图「{intent}」的连接。"
        if (intent or "").strip()
        else "每段末尾用一句话点出该地点的口碑理由或地理优势。"
    )
    system = (
        "你是目的地百科编辑。为每个地点写一段 200~300 字的详细介绍（硬性要求：低于 180 字视为不合格，"
        "必须把特色讲透、把背景写实、把建议给具体来凑足篇幅），分三个自然段："
        "第一段讲特色亮点与定位（这里以什么闻名、最值得看的是什么）；"
        "第二段讲历史/文化/建筑背景（一两句有信息量的事实，不确定的细节宁可不写）；"
        "第三段给实用游玩建议（建议停留时长、最佳时段、门票预约方式、周边顺游 tips）。"
        + link_rule
        + "若对某地点事实不确定，可先调用 search_pois 工具检索该城市候选再写；"
        "工具只读，禁止编造具体价格。只输出 JSON："
        '{"intros":{"名称":"介绍",...}}，必须覆盖给出的所有名称。'
    )
    user = f"城市：{city}\n地点列表：{json.dumps(names, ensure_ascii=False)}"
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]
    # 200-300 字/地点：按地点数给足输出预算，避免批量生成被 max_tokens 截断
    # （截断即 JSON 解析失败 → 全部介绍为空）。
    max_tokens = max(2600, min(8000, len(names) * 500 + 800))
    raw = ""
    try:
        loop = run_tool_call_loop(
            client,
            messages,
            max_rounds=2,
            model=model_for("fast"),
            max_tokens=max_tokens,
        )
        message = loop.get("message") or {}
        raw = str(message.get("content") or "")
    except FunctionCallingError as exc:
        # FC 超轮/协议失败：降级单轮生成，不拖垮介绍接口
        logger.warning("poi_intros function calling fallback: %s", exc)
        raw = ""
    except Exception as exc:
        logger.warning("poi_intros FC unexpected error, fallback: %s", exc)
        raw = ""
    if not raw.strip():
        raw = client.complete(
            user,
            system_prompt=system,
            temperature=0.3,
            max_tokens=max_tokens,
            model=model_for("fast"),
        )
    text = raw.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1].rsplit("```", 1)[0]
    try:
        data = json.loads(text[text.find("{") : text.rfind("}") + 1])
    except (json.JSONDecodeError, ValueError):
        return {}
    intros = data.get("intros")
    if not isinstance(intros, dict) and isinstance(data.get("plans"), dict):
        intros = data.get("plans")
    if not isinstance(intros, dict):
        return {}
    # 长度硬校验：fast model 对 200~300 字契约遵循度不足（实测均 ~90 字），
    # 对过短项再发一轮扩写（引用上轮介绍，要求补足篇幅），失败保留原值。
    MIN_INTRO_LEN = 180
    short = {n: str(t or "") for n, t in intros.items() if len(str(t or "")) < MIN_INTRO_LEN}
    if short:
        try:
            retry_system = (
                system + "\n上一轮生成的介绍过短未达标。本轮只针对下列地点重写介绍，"
                "每段必须达到 200~300 字，直接沿用并扩充上一轮的事实，不得缩水。"
            )
            retry_user = (
                f"城市：{city}\n地点列表：{json.dumps(list(short.keys()), ensure_ascii=False)}\n"
                f"上一轮过短介绍：{json.dumps(short, ensure_ascii=False)}"
            )
            raw2 = client.complete(
                retry_user,
                system_prompt=retry_system,
                temperature=0.3,
                max_tokens=max_tokens,
                model=model_for("fast"),
            )
            text2 = raw2.strip()
            if text2.startswith("```"):
                text2 = text2.split("\n", 1)[-1].rsplit("```", 1)[0]
            data2 = json.loads(text2[text2.find("{") : text2.rfind("}") + 1])
            fixed = data2.get("intros")
            if isinstance(fixed, dict):
                for n, t in fixed.items():
                    if n in intros and isinstance(t, str) and len(t) > len(str(intros[n] or "")):
                        intros[n] = t
        except Exception as exc:
            logger.warning("poi_intros rewrite pass failed: %s", exc)
    return intros
