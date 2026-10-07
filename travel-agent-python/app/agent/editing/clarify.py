"""意图确认节点：多轮对话收集行程条件与偏好。"""

import json
import logging
from datetime import date

from app.agent.data.city_center import destination_problem
from app.agent.data.city_reference import known_city_hits
from app.common.llm_client import get_llm_client
from app.schemas.trip import MAX_TRIP_DAYS, ClarifyRequest, ClarifyResponse

logger = logging.getLogger(__name__)

_REQUIRED = ["city", "days", "persons"]
_KNOWN = [
    *_REQUIRED,
    "start_date",
    "stay_nights",
    "budget",
    "hotel_tier",
    "preferences",
    "origin_city",
]
_LABELS = {"city": "目的地城市", "days": "出行天数", "persons": "出行人数"}
_INT_SLOTS = ("days", "persons")
_DEFAULT_OPTIONS = {
    "city": ["帮我推荐目的地"],
    "days": ["3 天", "5 天", "7 天"],
    "persons": ["2 人", "4 人", "一家人"],
}
_BLOCKED_OPTIONS = [f"改成 {MAX_TRIP_DAYS} 天以内", "拆成两段行程"]
_NEGOTIATION = f"单次行程最多排 {MAX_TRIP_DAYS} 天哦～要不要改成 {MAX_TRIP_DAYS} 天以内，或者拆成两段分开规划？"
# BIZ-4（2026-10-06）：矛盾预算前置提示。人均每天低于该地板线时，就绪前先协商
# 一次——「100 元游瑞士 10 天」这类输入此前被静默放行，事后只剩泛化 degraded
# 文案。budget_tier 的节俭档地板是 ¥150/天，低于 100 连国内基础食宿都难覆盖。
_BUDGET_WARN_PPD = 100.0
_BUDGET_WARN_SLOT = "_budget_warned"  # 随 slots 往返的「已提示」标记：无状态服务只拦一次
# 残留③（2026-10-06）：行程级荒谬预检——「一天四城」类输入此前被静默放行，下游
# 研究与终检只能兜成 degraded 文案。确定性判据：城市槽位命中的字典城市数 > 2 且
# days=1（双城一日如苏杭是常见可行玩法，不拦）。同样只协商一次。
_MULTICITY_MAX_PER_DAY = 2
_MULTICITY_WARN_SLOT = "_multicity_warned"
# AILIVE-2：LLM 失败曾双层静默吞掉（_ask 返回空串 + _extract 等同没抽到），用户面对
# 无提示的追问循环。降级轮的话术必须如实告知服务不稳并给出出路（右侧补填 / 稍后再试）。
_DEGRADED_NOTICE = (
    "抱歉，规划服务这会儿有点不稳，你刚才那句没能分析出来～可以在右侧直接补齐出发信息，或稍后再说一次试试。"
)
# F10：追问文本与点选选项由系统按必填优先顺序自动生成（缺槽分支强制同槽对齐），
# LLM 的 question/options 只保留一个用途——天数超上限时的协商话术。
_EXTRACT_SPEC = (
    '只输出 JSON：{"city":"城市名或null","origin_city":"出发城市或null",'
    '"start_date":"YYYY-MM-DD或null",'
    '"days":数字或null,"stay_nights":数字或null,"persons":数字或null,'
    '"budget":数字或null,"hotel_tier":"经济型/舒适型/高档型/豪华型/奢华型或null",'
    '"preferences":["偏好"]或null,'
    '"question":"仅当用户要的天数超过上限时给一句自然口语的协商话术，否则null",'
    '"options":["仅协商时给配合question的2~4个短选项，否则null"]}。'
)
_SYSTEM_PROMPT = (
    "你是旅行规划的信息收集助手。从用户最新一句话中抽取槽位，与已有槽位合并。"
    "追问哪个槽位、给哪些点选选项由系统按必填优先（目的地→天数→人数，可选槽位靠后）"
    "自动生成，你只负责抽取，不要替系统编追问或选项。"
    f"{_EXTRACT_SPEC}"
    "没提到的字段一律 null，不要猜测。"
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


def _ask(req: ClarifyRequest) -> str | None:
    """LLM 抽取；通道挂了返回 None（调用方据此带降级标记），对话不得因追问失败而卡死。"""
    client = get_llm_client()
    try:
        return client.complete(
            f"今天是 {date.today().isoformat()}。\n"
            f"已有槽位：{json.dumps(req.slots, ensure_ascii=False)}\n用户说：{req.message}",
            system_prompt=_SYSTEM_PROMPT,
            temperature=0,
        )
    except Exception as e:
        # AILIVE-2：只记 str(e) 会丢类型名（连接层异常甚至可能是空串），排障困难
        logger.warning("clarify llm call failed: %s: %s", type(e).__name__, e)
        return None


def _extract(raw: str, slots: dict) -> tuple[str | None, list[str] | None, bool]:
    """解析 LLM 输出：槽位并入 slots，返回 (question, options, 解析是否成功)；解析失败等同没抽到。"""
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
        return data.get("question") or None, options, True
    except Exception as e:
        logger.warning("clarify parse failed: %s | raw=%s", e, raw[:200])
        return None, None, False


def _respond(slots: dict, missing: list[str], question: str | None, options: list[str] | None) -> ClarifyResponse:
    """三级出口：超天协商 > 缺槽追问 > 就绪（就绪时不带追问，选择交给确认条）。

    F10：缺槽追问的问题与选项一律按 ``missing[0]`` 槽位现生成——问哪个槽位就给哪个
    槽位的候选，且必填（city→days→persons）未齐前可选槽位不可能成为追问对象；
    LLM 自由发挥的 question/options 只在超天协商分支透传，杜绝「问出发城市却给
    天数选项」「出发日期先于人数被问」。
    """
    if isinstance(slots.get("days"), int) and slots["days"] > MAX_TRIP_DAYS:
        # 上限是产品红线：不静默截断，天数原样保留、协商话术交回对话
        return ClarifyResponse(
            slots=slots,
            missing=missing,
            question=question or _NEGOTIATION,
            ready=False,
            options=options or _BLOCKED_OPTIONS,
            blocked=True,
        )
    if missing:
        # 文本与 chips 强制同槽：不再让 LLM 的自由追问/选项越过 missing[0]
        slot = missing[0]
        return ClarifyResponse(
            slots=slots,
            missing=missing,
            question=f"还想确认一下{_LABELS[slot]}～",
            ready=False,
            options=_DEFAULT_OPTIONS.get(slot, []),
        )
    # 必填齐备、即将就绪——两个确定性把关（矛盾预算/一天多城）依次过闸，
    # 各自只拦一次（slots 标记防循环，见各 gate 注）
    days_raw = slots.get("days")
    problem = destination_problem(str(slots.get("city") or ""))
    if problem:
        return ClarifyResponse(slots=slots, missing=["city"], ready=False, blocked=True, question=problem, options=[])
    for gate in (_budget_gate, _multicity_gate):
        blocked = gate(slots, days_raw)
        if blocked is not None:
            return blocked
    return ClarifyResponse(slots=slots, missing=[], question=None, ready=True, options=[])


def _budget_gate(slots: dict, days_raw: object) -> ClarifyResponse | None:
    """BIZ-4：矛盾预算前置提示。人均每天低于地板线时就绪前先协商一次——
    「100 元游瑞士 10 天」这类输入此前被静默放行，事后只剩泛化 degraded 文案。"""
    budget_raw = slots.get("budget")
    persons_raw = slots.get("persons")
    persons: int = persons_raw if isinstance(persons_raw, int) and persons_raw > 0 else 1
    if (
        isinstance(budget_raw, (int, float))
        and isinstance(days_raw, int)
        and days_raw > 0
        and float(budget_raw) / persons / days_raw < _BUDGET_WARN_PPD
        and not slots.get(_BUDGET_WARN_SLOT)
    ):
        slots[_BUDGET_WARN_SLOT] = True
        ppd = float(budget_raw) / persons / days_raw
        return ClarifyResponse(
            slots=slots,
            missing=[],
            ready=False,
            blocked=True,
            question=f"这趟预算人均每天约 ¥{ppd:.0f}，可能连基础的住宿和餐饮都覆盖不了哦～要调整预算或天数吗？",
            options=["提高预算", "减少天数", "就按这个预算试试"],
        )
    return None


def _multicity_gate(slots: dict, days_raw: object) -> ClarifyResponse | None:
    """残留③：行程级荒谬预检——城市槽位命中的字典城市数超线且 days=1（一天四城类
    输入此前被静默放行，下游研究与终检只能兜成 degraded 文案）。双城一日如苏杭是
    常见可行玩法，不拦；同样只协商一次。"""
    city_text = slots.get("city")
    if isinstance(city_text, str) and days_raw == 1 and not slots.get(_MULTICITY_WARN_SLOT):
        hits = known_city_hits(city_text)
        if len(hits) > _MULTICITY_MAX_PER_DAY:
            slots[_MULTICITY_WARN_SLOT] = True
            preview = "、".join(hits[:4])
            return ClarifyResponse(
                slots=slots,
                missing=[],
                ready=False,
                blocked=True,
                question=f"一天串完「{preview}」基本全程都在赶路哦～要调整天数或只挑一两座城市深玩吗？",
                options=["延长到多天分城玩", "只挑 1-2 座城市", "就按一天多城试试"],
            )
    return None


def run_clarify(req: ClarifyRequest) -> ClarifyResponse:
    slots = dict(req.slots or {})
    raw = _ask(req)
    if raw is None:
        question, options, parsed = None, None, False
    else:
        question, options, parsed = _extract(raw, slots)
    for key in _INT_SLOTS:
        _normalize_int(slots, key)
    missing = [k for k in _REQUIRED if k not in slots or slots[k] in (None, "")]
    response = _respond(slots, missing, question, options)
    if raw is None or not parsed:
        # AILIVE-2：本轮 LLM 调用失败或输出不可解析 = 槽位零进展且服务降级，必须
        # 如实透出（degraded 标记 + 兜底话术），不再伪装成正常的缺槽追问让用户
        # 无限循环。超天协商（blocked）话术本身已是明确引导，不覆盖。
        response.degraded = True
        if not response.ready and not response.blocked:
            response.question = _DEGRADED_NOTICE
    return response
