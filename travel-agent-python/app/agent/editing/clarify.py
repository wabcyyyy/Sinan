"""意图确认节点（LLM 半边）：抽取、状态播种/回写与出口编排。

状态语义（spec 2026-10-08 §5.2/M2 §7）：ClarifyRequest.state 是服务端权威累计
状态，模型输出解析成有类型 patch（set/unset/add/remove）后确定性应用；出口
策略（目标计算/协商闸/自然回复采用）是纯规则，在 clarify_policy.respond——
本模块只做 LLM 调用、解析与状态搬运。正常轮恰好一次 `complete`；输出不可用时
**有界修复一次**（复用生成链 `json_output.parse_llm_json_with_repair` 的同一实现），
再失败按降级轮如实透出，已确认槽位不动。

老前端兼容：ClarifyResponse.slots 继续携带基础槽位投影（含一次性协商标记），
老客户端随轮回传 slots 的行为不破坏；state 传入时以 state 为准。
未支持或冲突的要求不丢弃：被拒 patch 追加进 requirements.unresolved_requests。
"""

import json
import logging
from datetime import date
from typing import get_args

from app.agent.core.json_utils import parse_llm_json_or_none
from app.agent.editing.clarify_policy import (
    BUDGET_WARN_SLOT,
    DINING_CHOICES,
    MULTICITY_WARN_SLOT,
    REQUIRED_SLOTS,
    apply_acceptance,
    respond,
)
from app.agent.generation.content.json_output import parse_llm_json_with_repair
from app.common.llm_client import get_llm_client
from app.schemas.requirement_patches import PatchTarget, RequirementPatch, apply_intake_patches
from app.schemas.trip import MAX_TRIP_DAYS, ClarifyRequest, ClarifyResponse
from app.schemas.trip_requirements import IntakeState

logger = logging.getLogger(__name__)

_KNOWN = [
    *REQUIRED_SLOTS,
    "start_date",
    "stay_nights",
    "budget",
    "hotel_tier",
    "preferences",
    "origin_city",
]
_INT_SLOTS = ("days", "persons")
# AILIVE-2：LLM 失败曾双层静默吞掉（_ask 返回空串 + _extract 等同没抽到），用户面对
# 无提示的追问循环。降级轮的话术必须如实告知服务不稳并给出重试出路。
_DEGRADED_NOTICE = (
    "抱歉，规划服务这会儿有点不稳，你刚才那句没能分析出来～你的已有信息还在，可以换个简短说法，或稍后再说一次试试。"
)
# F10：追问文本与点选选项由系统按必填优先顺序自动生成（clarify_policy.respond），
# LLM 的 question/options 只保留一个用途——天数超上限时的协商话术。
_EXTRACT_SPEC = (
    '只输出 JSON：{"city":"城市名或null","origin_city":"出发城市或null",'
    '"start_date":"YYYY-MM-DD或null",'
    '"days":数字或null,"stay_nights":数字或null,"persons":数字或null,'
    '"budget":数字或null,"hotel_tier":"经济型/舒适型/高档型/豪华型/奢华型或null",'
    '"preferences":["偏好"]或null,'
    '"patches":[{"op":"set/unset/add/remove","target":"目标枚举","value":值或null,'
    '"name":名称或null,"day_no":数字或null}]或null,'
    '"ack":"对用户这句话的一句热情接话或null：像懂行的旅行朋友，具体回应他提到的地点/同伴/玩法'
    "（如「成都吃火锅逛老街，这个组合太对味了！」「带爸妈出门，节奏我会排得松一点～」），"
    '不超过30字，不提问，不复述已给参数",'
    '"reply":"给用户的完整回复或null：先接住这句话（可沿用 ack），再自然地问出下一个该问的问题，'
    '不超过60字，一次只问一个问题；不要宣称行程已生成或已满足全部要求",'
    '"reply_for":"reply 里问的目标或null：city/days/persons/budget/start_date/preferences/confirm/'
    'days_limit/budget_low/multicity，须与 reply 同时给出",'
    '"accepted":"用户明确接受的合作协商或null：仅当用户表示就按当前预算/天数继续'
    '（如「就按这个预算试试」「可以就这样」）时给 budget_low，否则null",'
    '"question":"仅当用户要的天数超过上限时给一句自然口语的协商话术，否则null",'
    '"options":["仅协商时给配合question的2~4个短选项，否则null"]}。'
    "patches 只在用户表达了基础槽位之外的需求时输出，目标只允许这些枚举："
    "required_place(必去地点：name=地点名，day_no=指定第几天或null=全程必去，"
    '"改成第N天去X"用 op=set 替换日约束)、excluded_place(不去的具体地点，value=地点名)、'
    "excluded_category(不去的类别：museum=博物馆，temple=寺庙/神社，如「不要寺庙」)、"
    "pace(值 normal/relaxed)、transport_preference(值 walking/driving/mixed/unspecified)、"
    "max_attractions_per_day(每天景点数硬上限，1~20整数；每天最多两个景点=2)、"
    "dietary_requirement(vegetarian=素食，可含蛋奶；vegan=纯素，不含蛋奶；不再要求素食用 unset)、"
    "dining_evidence_policy(verified_only=只安排有饮食依据的餐厅；review_candidates=用户明确接受"
    "先给餐饮候选并自行核实。仅在用户明确选择后输出，不能因为说素食或直接开始就默认接受候选)、"
    "max_walk_minutes_per_leg(单段步行分钟上限，正整数)、"
    "budget_policy_mode(值 target/hard_cap，「预算不限制了」用 op=unset)、"
    "lodging_locked_hotel(锁定酒店名)。没提到的需求不要编 patch。"
)
_SYSTEM_PROMPT = (
    "你是司南，一个热情又靠谱的旅行规划助手，正在和用户聊出这趟旅行的基本信息。"
    "从用户最新一句话中抽取槽位，与已有槽位合并。"
    "点选选项由系统自动生成，你不要编选项；你负责抽取、一句接话（ack）和一句回复（reply）。"
    f"{_EXTRACT_SPEC}"
    "没提到的字段一律 null，不要猜测。"
    "ack/reply 的语气：热情、友好、有画面感，像一个懂行又靠谱的旅行朋友，但要简短；"
    "接住用户这句话里的具体诉求（如「带爸妈」→回应省力安排，「吃火锅」→回应美食），"
    "热情要具体，不要空洞的「太棒了」「好的收到」，不要用「正式落库」这类系统话术，"
    "不要每轮重复罗列已有信息；每轮最多回应一个重点。"
    "追问顺序（合并本轮抽取结果后判断）：目的地→天数→人数→预算（问全程总预算，不问人均）→出发日期→玩法偏好→确认；"
    "预算/出发日期/玩法偏好若已有值或在「已问过」里就跳过；全部齐了 reply_for 填 confirm，"
    "reply 只需说信息齐了、可以在右侧确认开工。"
    f"单次行程天数上限 {MAX_TRIP_DAYS} 天：用户要超过时 days 照实抽取，"
    f"但此时 question 必须说明最多 {MAX_TRIP_DAYS} 天，并协商改天数或拆成两段。"
)


def _normalize_int(slots: dict, key: str) -> None:
    """LLM 偶尔把数字槽位吐成「两周」这类词；洗不成正整数就当没抽到，让追问接手。"""
    if key not in slots:
        return
    value = slots[key]
    if isinstance(value, bool):
        slots.pop(key)
        return
    try:
        number = int(value)
    except (TypeError, ValueError):
        slots.pop(key)
        return
    if number < 1:
        slots.pop(key)
    else:
        slots[key] = number


_BASE_SLOT_KEYS = ("city", "days", "persons", "budget", "start_date", "stay_nights", "hotel_tier", "origin_city")


def _slots_projection(state: IntakeState) -> dict:
    """IntakeState → 兼容槽位投影：老前端随轮回传 slots 的行为不破坏。"""
    slots: dict = {}
    for key in _BASE_SLOT_KEYS:
        value = getattr(state, key)
        if value is not None:
            slots[key] = value
    if state.preferences:
        slots["preferences"] = list(state.preferences)
    slots.update({marker: True for marker, hit in state.negotiations.items() if hit})
    return slots


def _seed_state(req: ClarifyRequest) -> IntakeState:
    """state 传入则以它为权威；未传（老前端）从兼容 slots 播种。"""
    if req.state is not None:
        return req.state
    state = IntakeState()
    slots = req.slots or {}
    for key in _BASE_SLOT_KEYS:
        value = slots.get(key)
        if value in (None, ""):
            continue
        try:
            setattr(state, key, value)
        except Exception:
            # 兼容投影里的脏值不致命：丢弃该槽位，由追问重新收集
            setattr(state, key, None)
    prefs = slots.get("preferences")
    if isinstance(prefs, list):
        state.preferences = [str(p) for p in prefs if str(p).strip()][:10]
    for marker in (BUDGET_WARN_SLOT, MULTICITY_WARN_SLOT):
        if slots.get(marker):
            state.negotiations[marker] = True
    return state


def _sync_base_from_slots(state: IntakeState, slots: dict) -> None:
    """本轮基础槽位抽取结果回写累计状态（patch 之外的基础参数单一回写点）。"""
    for key in _BASE_SLOT_KEYS:
        value = slots.get(key)
        if value in (None, ""):
            continue
        if key in _INT_SLOTS and not isinstance(value, int):
            continue
        setattr(state, key, value)
    prefs = slots.get("preferences")
    if isinstance(prefs, list) and prefs:
        state.preferences = [str(p) for p in prefs if str(p).strip()][:10]


#: 抽取出口的 token 预算：思考（全局 low）计入 completion，2026-10-12 同一原话 5 次
#: 实测 completion 416-1856（reasoning 185-1617），旧默认 2048 最高用到 91%，与此前
#: 空串/JSON 解析失败一致。4096 ≈ 实测上沿 2.2 倍；正文本身只有 ~650 字。
_EXTRACT_MAX_TOKENS = 4096


def _closed(properties: dict) -> dict:
    """闭合对象：strict json_schema 要求每个键都在 required 里且不许额外键。"""
    return {"type": "object", "properties": properties, "required": sorted(properties), "additionalProperties": False}


#: 抽取出口的形状（与 `_EXTRACT_SPEC` 同一份契约：那边是给模型看的自然语言说明，
#: 这边是给守约网关执行的机器约束；改一处必须同时改另一处，如同 day_prompts 里
#: `open_day_output_schema` 与生成提示词的分工）。patch 的 target 枚举直接从
#: `app.schemas.requirement_patches` 取，不在这里抄第二份词表。
_OUTPUT_SCHEMA = _closed(
    {
        "city": {"type": ["string", "null"]},
        "origin_city": {"type": ["string", "null"]},
        "start_date": {"type": ["string", "null"], "description": "YYYY-MM-DD"},
        "days": {"type": ["integer", "null"]},
        "stay_nights": {"type": ["integer", "null"]},
        "persons": {"type": ["integer", "null"]},
        "budget": {"type": ["number", "null"]},
        "hotel_tier": {"type": ["string", "null"]},
        "preferences": {"type": ["array", "null"], "items": {"type": "string"}},
        "patches": {
            "type": ["array", "null"],
            "items": _closed(
                {
                    "op": {"type": "string", "enum": ["set", "unset", "add", "remove"]},
                    "target": {"type": "string", "enum": sorted(get_args(PatchTarget))},
                    "value": {
                        "anyOf": [
                            {"type": ["string", "number", "boolean", "null"]},
                            {"type": "array", "items": {"type": "string"}},
                        ]
                    },
                    "name": {"type": ["string", "null"]},
                    "day_no": {"type": ["integer", "null"]},
                }
            ),
        },
        "ack": {"type": ["string", "null"]},
        "reply": {"type": ["string", "null"]},
        "reply_for": {"type": ["string", "null"]},
        "accepted": {"type": ["string", "null"]},
        "question": {"type": ["string", "null"]},
        "options": {"type": ["array", "null"], "items": {"type": "string"}},
    }
)


def _user_line(req: ClarifyRequest, slots: dict, state: IntakeState) -> str:
    """抽取请求的用户侧文本（唯一构造点：正调与有界修复共用，两处不能分叉）。"""
    requirements = state.requirements.model_dump(mode="json", by_alias=True, exclude_defaults=True, exclude_none=True)
    return (
        f"今天是 {date.today().isoformat()}。\n"
        f"已有槽位：{json.dumps(slots, ensure_ascii=False)}\n"
        f"已有需求约束：{json.dumps(requirements, ensure_ascii=False)}\n"
        f"已问过的可选问题：{json.dumps(state.optional_asked, ensure_ascii=False)}\n用户说：{req.message}"
    )


def _response_format(client) -> dict:
    """能力位由 client 自己判（`json_response_format`）：守约网关给 json_schema 强约束，
    不支持的降级 json_object——与生成链同一口径，不在这里猜模型能力。"""
    return client.json_response_format("clarify_extract", _OUTPUT_SCHEMA)


def _ask(req: ClarifyRequest, slots: dict, state: IntakeState) -> str | None:
    """LLM 抽取；通道挂了返回 None（调用方据此带降级标记），对话不得因追问失败而卡死。"""
    client = get_llm_client()
    try:
        return client.complete(
            _user_line(req, slots, state),
            system_prompt=_SYSTEM_PROMPT,
            temperature=0,
            max_tokens=_EXTRACT_MAX_TOKENS,
            response_format=_response_format(client),
        )
    except Exception as e:
        # AILIVE-2：只记 str(e) 会丢类型名（连接层异常甚至可能是空串），排障困难
        logger.warning("clarify llm call failed: %s: %s", type(e).__name__, e)
        return None


def _repair(req: ClarifyRequest, slots: dict, state: IntakeState, raw: str) -> dict | None:
    """输出不可用时的**有界**修复：携原请求 + 上一轮原输出重问恰好一次。

    复用生成链的同一实现（`json_output.parse_llm_json_with_repair`：一次修复、
    遥测 `llm_json_repair`、再失败上抛），这里不重写第二套修复循环。修复通道自己
    挂了或二次仍不可用时返回 None → 走降级轮，用户原话与已确认槽位都不动。
    """
    client = get_llm_client()
    try:
        return parse_llm_json_with_repair(
            raw,
            client,
            repair_max_tokens=_EXTRACT_MAX_TOKENS,
            response_format=_response_format(client),
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": _user_line(req, slots, state)},
            ],
        )
    except Exception as e:
        logger.warning("clarify repair failed: %s: %s", type(e).__name__, e)
        return None


def _trimmed_str(value: object) -> str | None:
    """文本出口字段：非字符串或空白都按没给（不当成空话术）。"""
    return value.strip() if isinstance(value, str) and value.strip() else None


def _extract(
    data: dict, slots: dict
) -> tuple[str | None, list[str] | None, list[RequirementPatch], str | None, str | None, str | None, str | None]:
    """把已解析的抽取结果结构化：槽位并入 slots、patch/reply/reply_for/accepted/ack 取出。

    返回 (question, options, patches, reply, reply_for, accepted, ack)；JSON 解析在
    调用方完成（`parse_llm_json_or_none` → `_repair`，唯一实现），这里只处理形状：
    单条 patch 形状非法只跳过该条（记日志），不否定整轮。
    语义性拒绝（词表外类别、超界值）由 apply_intake_patches 处理并入 unresolved；
    reply 的目标校验在 clarify_policy.respond（服务端重算 next 后比对）。
    """
    patches: list[RequirementPatch] = []
    for k in _KNOWN:
        v = data.get(k)
        if v not in (None, "", "null"):
            slots[k] = v
    raw_options = data.get("options")
    options = None
    if isinstance(raw_options, list):
        options = [str(o).strip() for o in raw_options if str(o).strip()][:4] or None
    raw_patches = data.get("patches")
    if isinstance(raw_patches, list):
        for item in raw_patches:
            try:
                patches.append(RequirementPatch.model_validate(item))
            except Exception as e:
                logger.warning("clarify patch invalid: %s | item=%s", e, str(item)[:120])
    reply = _trimmed_str(data.get("reply"))
    reply_for = _trimmed_str(data.get("reply_for"))
    accepted = _trimmed_str(data.get("accepted"))
    ack = _trimmed_str(data.get("ack"))
    return data.get("question") or None, options, patches, reply, reply_for, accepted, ack


def run_clarify(req: ClarifyRequest) -> ClarifyResponse:
    state = _seed_state(req)
    slots = _slots_projection(state)
    raw = _ask(req, slots, state)
    # 解析走唯一实现（core.json_utils），失败交 `_repair` 有界修复一次；两条都不可用
    # 才是"本轮没抽到"——降级如实透出，已确认槽位与用户原话都不动（AILIVE-2）。
    data = parse_llm_json_or_none(raw) if raw is not None else None
    if raw is not None and data is None:
        data = _repair(req, slots, state, raw)
    parsed = data is not None
    if data is None:
        question, options, patches, llm_reply, reply_for, accepted, ack = None, None, [], None, None, None, None
    else:
        question, options, patches, llm_reply, reply_for, accepted, ack = _extract(data, slots)
    for key in _INT_SLOTS:
        _normalize_int(slots, key)
    # 顺序即语义（E02 回归钉住）：先回写基础槽位、再应用 patch——「预算不限制了」
    # 的 unset 必须赢过旧值经 slots 投影的回写，否则解除的预算被投影复活（M7 题集实测）。
    _sync_base_from_slots(state, slots)
    # 饮食证据取舍由用户点选明确文案决定，模型不可替用户接受待核实候选。
    if any(
        patch.target == "dining_evidence_policy" and patch.value != state.requirements.dining_evidence_policy
        for patch in patches
    ):
        state.requirements.dining_evidence_policy = None  # 改口径须重新明确选择，不能静默保留旧取舍
    patches = [patch for patch in patches if patch.target != "dining_evidence_policy"]
    if patches:
        # 有类型 patch 确定性应用；被拒条目进 unresolved_requests（不丢弃）
        apply_intake_patches(state, patches)
    if state.requirements.dietary_requirement and req.message.strip() in DINING_CHOICES:
        apply_intake_patches(
            state,
            [RequirementPatch(op="set", target="dining_evidence_policy", value=DINING_CHOICES[req.message.strip()])],
        )
    apply_acceptance(state, accepted)
    missing = [k for k in REQUIRED_SLOTS if k not in slots or slots[k] in (None, "")]
    response = respond(state, missing, question, options, llm_reply, reply_for, ack)
    # 投影最后做：出口内的协商闸标记（budget/multicity warned）必须进当轮 slots
    response.slots = _slots_projection(state)
    if raw is None or not parsed:
        # AILIVE-2：本轮 LLM 调用失败或输出不可解析 = 槽位零进展且服务降级，必须
        # 如实透出（degraded 标记 + 兜底话术），不再伪装成正常的缺槽追问让用户
        # 无限循环。超天协商（blocked）话术本身已是明确引导，不覆盖。
        response.degraded = True
        if not response.ready and not response.blocked:
            response.question = _DEGRADED_NOTICE
            response.reply = None
    return response
