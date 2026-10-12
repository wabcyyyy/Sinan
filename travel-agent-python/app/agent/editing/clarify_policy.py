"""clarify 出口策略：纯规则的目标计算、协商闸与自然回复采用（M2，spec §7.1）。

职责（从 clarify.py 拆出的纯策略半边，无 LLM 调用）：
- respond：出口策略唯一定义点——非法/冲突阻断 → 缺必填 city/days/persons →
  最重要的可选问题 → 确认；每轮最多一个主要追问（next），已给的信息不重问；
- adopted_reply：模型 reply 采用规则——服务端重算目标与模型声明 reply_for
  一致且不超长才采用，否则规则回退（模型输出不是追问/就绪权威）；
- 协商闸：矛盾预算（BIZ-4）提示一次、用户明确接受（绑定参数摘要）后放行，
  未接受前就绪回复带估算提示；一天多城（残留③）提示一次后放行；
- apply_acceptance：接受记录与摘要失配失效。

依赖：app.agent.data（城市字典）、app.schemas；不含 LLM/IO。
跨模块 API（clarify.py 消费）：respond / adopted_reply / negotiation_summary /
apply_acceptance / OPTIONAL_ASK / CONFIRM_REPLY / BUDGET_CAUTION_REPLY /
BUDGET_WARN_SLOT / MULTICITY_WARN_SLOT / REQUIRED_SLOTS / LABELS / DEFAULT_OPTIONS。
"""

from app.agent.data.city_center import destination_problem
from app.agent.data.city_reference import known_city_hits
from app.agent.data.fx import destination_currency
from app.schemas.trip import MAX_TRIP_DAYS, ClarifyResponse
from app.schemas.trip_requirements import IntakeState

REQUIRED_SLOTS = ["city", "days", "persons"]
LABELS = {"city": "目的地城市", "days": "出行天数", "persons": "出行人数"}
DEFAULT_OPTIONS = {
    "city": ["帮我推荐目的地"],
    "days": ["3 天", "5 天", "7 天"],
    "persons": ["2 人", "4 人", "一家人"],
}
BLOCKED_OPTIONS = [f"改成 {MAX_TRIP_DAYS} 天以内", "拆成两段行程"]
NEGOTIATION = f"单次行程最多排 {MAX_TRIP_DAYS} 天哦～要不要改成 {MAX_TRIP_DAYS} 天以内，或者拆成两段分开规划？"
# BIZ-4（2026-10-06）：矛盾预算前置提示。人均每天低于该地板线时，就绪前先协商
# 一次——「100 元游瑞士 10 天」这类输入此前被静默放行。budget_tier 的节俭档地板
# 是 ¥150/天，低于 100 连国内基础食宿都难覆盖。
BUDGET_WARN_PPD = 100.0
BUDGET_WARN_SLOT = "_budget_warned"  # 一次性协商闸标记：随 state.negotiations 往返，只拦一次
# 残留③（2026-10-06）：行程级荒谬预检——「一天四城」类输入此前被静默放行。
# 确定性判据：城市槽位命中的字典城市数 > 2 且 days=1（双城一日如苏杭不拦）。
_MULTICITY_MAX_PER_DAY = 2
MULTICITY_WARN_SLOT = "_multicity_warned"

#: 模型 reply 的长度上限：超长视为越界，走规则回退（spec §7.1）。
#: 2026-10-12 收紧 500 → 160：回复要简短，长篇大论不如规则话术
_REPLY_MAX = 160
#: 模型 ack（接话）上限：一句话，超长或带问号（接话不该提问，否则与追问叠成两问）即弃用
_ACK_MAX = 40

#: 缺必填时的追问（2026-10-12：替换「还想确认一下{X}～」这种表单腔）
SLOT_QUESTIONS = {
    "city": "想去哪座城市呀？还没想好的话，我也可以帮你推荐～",
    "days": "打算玩几天呢？",
    "persons": "这次几个人一起出发？",
}

#: 服务端主动追问的可选槽位（重要度序）；其余可选信息走右侧表单，不一律阻断。
#: 2026-10-12 预算提到最前：没有预算时酒店/餐饮档次全靠猜，且无预算行程曾整段缺失币种约束
_OPTIONAL_ORDER = ("budget", "start_date", "preferences")
#: budget 的点选项按天数×人数现算（见 _budget_options），这里留空
OPTIONAL_ASK: dict[str, tuple[str, list[str]]] = {
    "budget": ("全程预算大概多少？给个大概范围就行，我好把住宿和吃饭配得刚刚好。", []),
    "start_date": ("大概哪天出发？还没定的话说「待定」也行。", ["下周五出发", "近期周末出发", "日期待定"]),
    "preferences": (
        "这趟有什么特别的偏好吗？比如美食、自然风光，或者带爸妈要少走路。",
        ["特色美食 · 慢节奏", "自然风光 · 拍照", "经典打卡", "直接开始规划"],
    ),
}
# 规则回退话术（模型 reply 缺失/目标不符/超长时使用）
CONFIRM_REPLY = "信息都齐了～右侧确认一下就能开始规划。"
BUDGET_CAUTION_REPLY = "按这个预算我会尽量排得实惠些，具体花费出发前记得再核对～"
DINING_CHOICES = {"只安排有饮食依据的餐厅": "verified_only", "先给餐饮候选，我会自行核实": "review_candidates"}


def negotiation_summary(state: IntakeState) -> str:
    """被协商参数摘要：预算/天数/人数任一变化都会让旧接受失配自动失效（spec §7.2）。"""
    return f"budget={state.budget}/persons={state.persons}/days={state.days}"


def adopted_reply(llm_reply: str | None, reply_for: str | None, target: str) -> str | None:
    """模型回复采用规则：服务端重算的目标与模型声明一致且不超长才采用。

    模型输出不是追问/就绪权威——目标不符、缺失、超 500 字一律规则回退。
    """
    if not llm_reply or reply_for != target:
        return None
    text = llm_reply.strip()
    if not text or len(text) > _REPLY_MAX:
        return None
    return text


def _with_ack(ack: str | None, question: str) -> str | None:
    """模型接话 + 规则追问拼成一条回复：模型 reply 目标不符被弃用时，热情不随之丢失。"""
    text = (ack or "").strip()
    if not text or len(text) > _ACK_MAX or "?" in text or "？" in text:
        return None
    joiner = "" if text.endswith(("！", "。", "～", "~", "!", "，", ",")) else "！"
    return f"{text}{joiner}{question}"


def _round_budget(value: float) -> int:
    step = 500 if value >= 3000 else 100
    return int(round(value / step) * step)


def _budget_options(state: IntakeState) -> list[str]:
    """预算点选项：人均每天三档 × 人数 × 天数估出全程总额。

    国内 300 / 600 / 1200；海外（目的地有本币）600 / 1000 / 1800——东京两人两天给「全程 1200」不像话。"""
    persons = state.persons if isinstance(state.persons, int) and state.persons > 0 else 1
    days = state.days if isinstance(state.days, int) and state.days > 0 else 1
    tiers = (600, 1000, 1800) if destination_currency(state.city) else (300, 600, 1200)
    return [f"全程约 {_round_budget(ppd * persons * days)} 元" for ppd in tiers] + ["预算不限"]


def _slot_filled(state: IntakeState, slot: str) -> bool:
    if slot == "preferences":
        return bool(state.preferences)
    return getattr(state, slot, None) not in (None, "")


def _ppd(state: IntakeState) -> float | None:
    """人均每天预算（口径同 BIZ-4）；参数不足以计算时 None。"""
    if not isinstance(state.budget, (int, float)) or not isinstance(state.days, int) or state.days <= 0:
        return None
    persons = state.persons if isinstance(state.persons, int) and state.persons > 0 else 1
    return float(state.budget) / persons / state.days


def apply_acceptance(state: IntakeState, accepted: str | None) -> None:
    """记录用户对协商的明确接受；仅当该协商确实提示过才有效（模型声明不是权威）。

    接受绑定被协商参数摘要——预算/天数/人数一变，旧接受随摘要失配自动失效。
    """
    if accepted != "budget_low":
        return
    if not state.negotiations.get(BUDGET_WARN_SLOT):
        return
    state.acceptances["budget_low"] = negotiation_summary(state)


def _multicity_pending(state: IntakeState) -> tuple[str, list[str]] | None:
    """残留③：行程级荒谬预检——城市命中的字典城市数超线且 days=1；只协商一次。"""
    if not (isinstance(state.city, str) and state.days == 1) or state.negotiations.get(MULTICITY_WARN_SLOT):
        return None
    hits = known_city_hits(state.city)
    if len(hits) <= _MULTICITY_MAX_PER_DAY:
        return None
    state.negotiations[MULTICITY_WARN_SLOT] = True
    preview = "、".join(hits[:4])
    return (
        f"一天串完「{preview}」基本全程都在赶路哦～要调整天数或只挑一两座城市深玩吗？",
        ["延长到多天分城玩", "只挑 1-2 座城市", "就按一天多城试试"],
    )


def _hard_block(
    state: IntakeState,
    missing: list[str],
    question: str | None,
    options: list[str] | None,
    llm_reply: str | None,
    reply_for: str | None,
    build,
    llm_ack: str | None = None,
) -> ClarifyResponse | None:
    """硬阻断三连：超天协商 → 非法目的地 → 缺必填。命中返回响应，否则 None。"""
    # ① 天数超上限（不静默截断，原值保留走协商）
    if isinstance(state.days, int) and state.days > MAX_TRIP_DAYS:
        return build(
            missing=missing,
            question=question or NEGOTIATION,
            ready=False,
            options=options or BLOCKED_OPTIONS,
            blocked=True,
            reply=adopted_reply(llm_reply, reply_for, "days_limit"),
            reply_for=reply_for,
            next="days_limit",
        )
    # ② 非法/未知目的地（「直接开始」不可绕过）
    problem = destination_problem(str(state.city or ""))
    if problem:
        return build(missing=["city"], ready=False, blocked=True, question=problem, options=[], next="invalid_city")
    # ③ 缺必填：问哪个槽位就给哪个槽位的候选
    if missing:
        slot = missing[0]
        slot_question = SLOT_QUESTIONS[slot]
        return build(
            missing=missing,
            question=slot_question,
            ready=False,
            options=DEFAULT_OPTIONS.get(slot, []),
            reply=adopted_reply(llm_reply, reply_for, slot) or _with_ack(llm_ack, slot_question),
            reply_for=reply_for,
            next=slot,
        )
    return None


def _negotiation_block(
    state: IntakeState, llm_reply: str | None, reply_for: str | None, build
) -> tuple[ClarifyResponse | None, bool]:
    """协商闸：矛盾预算提示一次、一天多城提示一次。

    返回 (阻断响应, 就绪轮是否带预算提示)：无阻断时未接受低预算 → caution=True，
    就绪回复须带估算提示（估算草案，spec §7.2）。
    """
    ppd = _ppd(state)
    budget_low = ppd is not None and ppd < BUDGET_WARN_PPD
    budget_warned = bool(state.negotiations.get(BUDGET_WARN_SLOT))
    if budget_low and not budget_warned:
        state.negotiations[BUDGET_WARN_SLOT] = True
        return (
            build(
                missing=[],
                ready=False,
                blocked=True,
                question=f"这趟预算人均每天约 ¥{ppd:.0f}，可能连基础的住宿和餐饮都覆盖不了哦～要调整预算或天数吗？",
                options=["提高预算", "减少天数", "就按这个预算试试"],
                reply=adopted_reply(llm_reply, reply_for, "budget_low"),
                reply_for=reply_for,
                next="budget_low",
            ),
            False,
        )
    budget_caution = budget_low and state.acceptances.get("budget_low") != negotiation_summary(state)
    # 一天多城（残留③）：提示一次后放行（双城一日不拦）
    multicity = _multicity_pending(state)
    if multicity is not None:
        mc_question, mc_options = multicity
        return (
            build(
                missing=[],
                ready=False,
                blocked=True,
                question=mc_question,
                options=mc_options,
                reply=adopted_reply(llm_reply, reply_for, "multicity"),
                reply_for=reply_for,
                next="multicity",
            ),
            False,
        )
    return None, budget_caution


def respond(
    state: IntakeState,
    missing: list[str],
    question: str | None,
    options: list[str] | None,
    llm_reply: str | None,
    reply_for: str | None,
    llm_ack: str | None = None,
) -> ClarifyResponse:
    """出口策略（spec §7.1）：非法/冲突阻断 → 缺必填 → 最重要的可选问题 → 确认。

    每轮最多一个主要追问（next），已给的信息不重问；F10 沿用——追问文本与
    chips 按当前目标现生成，LLM 的 question/options 只在协商分支透传；
    reply 采用规则见 adopted_reply；模型 reply 被弃用时，用模型的一句接话（llm_ack）
    拼上规则追问（_with_ack），协商/阻断分支不拼。slots 兼容投影由调用方在出口后重算
    （协商闸标记在出口内新写入，必须进当轮投影）。
    """

    def build(**kw) -> ClarifyResponse:
        return ClarifyResponse(state=state, slots={}, **kw)

    blocked = _hard_block(state, missing, question, options, llm_reply, reply_for, build, llm_ack)
    if blocked is not None:
        return blocked
    negotiation, budget_caution = _negotiation_block(state, llm_reply, reply_for, build)
    if negotiation is not None:
        return negotiation
    if state.requirements.dietary_requirement and state.requirements.dining_evidence_policy is None:
        return build(
            missing=[],
            ready=False,
            blocked=True,
            next="dining_evidence_policy",
            question="餐厅的饮食资料可能不齐全。你希望只安排有素食依据的餐厅，还是先看候选、再自行核实？",
            options=list(DINING_CHOICES),
        )
    # 就绪：最重要的未问可选问题（不阻断生成，选择交给确认卡）
    optional = next((s for s in _OPTIONAL_ORDER if s not in state.optional_asked and not _slot_filled(state, s)), None)
    if optional is not None:
        state.optional_asked.append(optional)
        ask_question, ask_options = OPTIONAL_ASK[optional]
        if optional == "budget":
            ask_options = _budget_options(state)
        # 未接受的低预算：提示优先于模型回复（估算草案必须带提示，spec §7.2）
        caution_reply = BUDGET_CAUTION_REPLY if budget_caution else None
        return build(
            missing=[],
            ready=True,
            question=ask_question,
            options=ask_options,
            reply=caution_reply or adopted_reply(llm_reply, reply_for, optional) or _with_ack(llm_ack, ask_question),
            reply_for=reply_for,
            next=optional,
        )
    # 确认：结构化需求是权威，回复不宣称行程已生成/已满足全部要求
    if budget_caution:
        reply: str | None = BUDGET_CAUTION_REPLY
    else:
        reply = adopted_reply(llm_reply, reply_for, "confirm") or _with_ack(llm_ack, CONFIRM_REPLY) or CONFIRM_REPLY
    return build(missing=[], ready=True, question=None, options=[], reply=reply, reply_for="confirm", next="confirm")
