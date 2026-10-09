"""意图确认节点（LLM 半边）：抽取、状态播种/回写与出口编排。

状态语义（spec 2026-10-08 §5.2/M2 §7）：ClarifyRequest.state 是服务端权威累计
状态，模型输出解析成有类型 patch（set/unset/add/remove）后确定性应用；出口
策略（目标计算/协商闸/自然回复采用）是纯规则，在 clarify_policy.respond——
本模块只做 LLM 调用、解析与状态搬运，每轮恰好一次 complete（无第二次润色）。

老前端兼容：ClarifyResponse.slots 继续携带基础槽位投影（含一次性协商标记），
老客户端随轮回传 slots 的行为不破坏；state 传入时以 state 为准。
未支持或冲突的要求不丢弃：被拒 patch 追加进 requirements.unresolved_requests。
"""

import json
import logging
from datetime import date

from app.agent.editing.clarify_policy import (
    BUDGET_WARN_SLOT,
    MULTICITY_WARN_SLOT,
    REQUIRED_SLOTS,
    apply_acceptance,
    respond,
)
from app.common.llm_client import get_llm_client
from app.schemas.requirement_patches import RequirementPatch, apply_intake_patches
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
# 无提示的追问循环。降级轮的话术必须如实告知服务不稳并给出出路（右侧补填 / 稍后再试）。
_DEGRADED_NOTICE = (
    "抱歉，规划服务这会儿有点不稳，你刚才那句没能分析出来～可以在右侧直接补齐出发信息，或稍后再说一次试试。"
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
    '"reply":"给用户的自然回复或null：一句话、简短口语、接住这条消息的具体诉求；'
    '不要重列用户已给过的参数，不要宣称行程已生成或已满足全部要求",'
    '"reply_for":"这句回复对应的目标或null：city/days/persons/start_date/preferences/confirm/'
    'days_limit/budget_low/multicity，须与 reply 同时给出",'
    '"accepted":"用户明确接受的合作协商或null：仅当用户表示就按当前预算/天数继续'
    '（如「就按这个预算试试」「可以就这样」）时给 budget_low，否则null",'
    '"question":"仅当用户要的天数超过上限时给一句自然口语的协商话术，否则null",'
    '"options":["仅协商时给配合question的2~4个短选项，否则null"]}。'
    "patches 只在用户表达了基础槽位之外的需求时输出，目标只允许这些枚举："
    "required_place(必去地点：name=地点名，day_no=指定第几天或null=全程必去，"
    '"改成第N天去X"用 op=set 替换日约束)、excluded_place(不去的具体地点，value=地点名)、'
    "excluded_category(不去的类别，当前仅支持 museum，如「不要博物馆」)、"
    "pace(值 normal/relaxed)、transport_preference(值 walking/driving/mixed/unspecified)、"
    "max_walk_minutes_per_leg(单段步行分钟上限，正整数)、"
    "budget_policy_mode(值 target/hard_cap，「预算不限制了」用 op=unset)、"
    "lodging_locked_hotel(锁定酒店名)。没提到的需求不要编 patch。"
)
_SYSTEM_PROMPT = (
    "你是旅行规划的信息收集助手。从用户最新一句话中抽取槽位，与已有槽位合并。"
    "追问哪个槽位、给哪些点选选项由系统按必填优先（目的地→天数→人数，可选槽位靠后）"
    "自动生成，你只负责抽取和给一句自然回复，不要替系统编追问或选项。"
    f"{_EXTRACT_SPEC}"
    "没提到的字段一律 null，不要猜测。"
    "reply 的语气：简短、自然、不油腻；接住用户这句话里的具体诉求（如「带爸妈」→"
    "回应省力安排），不要每轮重复罗列已有信息，不要使用「太棒了」「正式落库」这类话；"
    "每轮最多回应一个重点。"
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


def _ask(req: ClarifyRequest, slots: dict, state: IntakeState) -> str | None:
    """LLM 抽取；通道挂了返回 None（调用方据此带降级标记），对话不得因追问失败而卡死。"""
    client = get_llm_client()
    requirements = state.requirements.model_dump(mode="json", by_alias=True, exclude_defaults=True, exclude_none=True)
    try:
        return client.complete(
            f"今天是 {date.today().isoformat()}。\n"
            f"已有槽位：{json.dumps(slots, ensure_ascii=False)}\n"
            f"已有需求约束：{json.dumps(requirements, ensure_ascii=False)}\n用户说：{req.message}",
            system_prompt=_SYSTEM_PROMPT,
            temperature=0,
        )
    except Exception as e:
        # AILIVE-2：只记 str(e) 会丢类型名（连接层异常甚至可能是空串），排障困难
        logger.warning("clarify llm call failed: %s: %s", type(e).__name__, e)
        return None


def _extract(
    raw: str, slots: dict
) -> tuple[str | None, list[str] | None, bool, list[RequirementPatch], str | None, str | None, str | None]:
    """解析 LLM 输出：槽位并入 slots、patch/reply/reply_for/accepted 结构化。

    返回 (question, options, 解析是否成功, patches, reply, reply_for, accepted)；
    解析失败等同没抽到；单条 patch 形状非法只跳过该条（记日志），不否定整轮。
    语义性拒绝（词表外类别、超界值）由 apply_intake_patches 处理并入 unresolved；
    reply 的目标校验在 clarify_policy.respond（服务端重算 next 后比对）。
    """
    patches: list[RequirementPatch] = []
    try:
        text = raw.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[-1].rsplit("```", 1)[0]
        data = json.loads(text[text.find("{") : text.rfind("}") + 1])
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
        reply = data.get("reply")
        reply = str(reply).strip() if isinstance(reply, str) and reply.strip() else None
        reply_for = data.get("reply_for")
        reply_for = str(reply_for).strip() if isinstance(reply_for, str) and reply_for.strip() else None
        accepted = data.get("accepted")
        accepted = str(accepted).strip() if isinstance(accepted, str) and accepted.strip() else None
        return data.get("question") or None, options, True, patches, reply, reply_for, accepted
    except Exception as e:
        logger.warning("clarify parse failed: %s | raw=%s", e, raw[:200])
        return None, None, False, [], None, None, None


def run_clarify(req: ClarifyRequest) -> ClarifyResponse:
    state = _seed_state(req)
    slots = _slots_projection(state)
    raw = _ask(req, slots, state)
    if raw is None:
        question, options, parsed, patches = None, None, False, []
        llm_reply, reply_for, accepted = None, None, None
    else:
        question, options, parsed, patches, llm_reply, reply_for, accepted = _extract(raw, slots)
    for key in _INT_SLOTS:
        _normalize_int(slots, key)
    # 顺序即语义（E02 回归钉住）：先回写基础槽位、再应用 patch——「预算不限制了」
    # 的 unset 必须赢过旧值经 slots 投影的回写，否则解除的预算被投影复活（M7 题集实测）。
    _sync_base_from_slots(state, slots)
    if patches:
        # 有类型 patch 确定性应用；被拒条目进 unresolved_requests（不丢弃）
        apply_intake_patches(state, patches)
    apply_acceptance(state, accepted)
    missing = [k for k in REQUIRED_SLOTS if k not in slots or slots[k] in (None, "")]
    response = respond(state, missing, question, options, llm_reply, reply_for)
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
