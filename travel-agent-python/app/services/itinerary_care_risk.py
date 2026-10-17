"""出发前照看的一次检查判定管线（个人 Agent V1，规格 §4.4 步骤 1–4 的判定半边）。

`evaluate_check(itinerary_id, token)` 是纯判定：资格闸 → 产品窗口 → 监控范围 →
天气 → 确定性风险 → 建议流程门槛。它不做收尾持久化——步骤 9 由 `itinerary_care_check`
按 token 条件落库，调度与争抢也在那边。这条线读的是正式行程（day/item 行），用户
待确认草稿只在「有没有」这一层被感知（规格 §4.4 步骤 2/§4.5）。

风险判定是确定性单点规则（`_risk_category`，规格 §3.3），不是模型结论；天气经
app.agent 门面读取，失败=None=unavailable，不是晴天。门槛全部通过时返回
`SuggestionOrder`（受影响日期/监控项/去重键——步骤 4 的产出），模型流程与发布
（步骤 5–9）在 `itinerary_care_check._run_suggestion`：当日 AI 尝试已在此边界原子
占用（外呼前），去重键则只有发布成功才随发布短事务落库。

依赖：app.services.itinerary_care（资格/范围判定与状态常量）；itinerary_chat
（草稿现状）；cost_admission（只读硬顶）；app.agent 门面（天气）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from sqlalchemy import or_, select, update

from app.agent import get_weather_forecast
from app.common.clock import shanghai_today
from app.common.envelope import ApiError
from app.common.vo_json import iso_datetime
from app.db.models import ItineraryCare, ItineraryDay, ItineraryItem, ItineraryMain, SysUser
from app.db.session import session_scope
from app.services import cost_admission, itinerary_chat
from app.services.itinerary_care import (
    STATUS_ACCOUNT_DISABLED,
    STATUS_DEFERRED,
    STATUS_FINISHED,
    STATUS_NEEDS_RESELECT,
    STATUS_NO_RISK,
    STATUS_QUOTA,
    STATUS_UNAVAILABLE,
    STATUS_WAITING,
    STATUS_WAITING_USER,
    SuggestionOrder,
    build_suggestion_order,
    current_attraction_ids,
    eligibility_problem,
)

# 检查窗：明天起 3 个自然日与行程日期的交集（Asia/Shanghai，规格 §3.2）
WINDOW_DAYS = 3

# ---- 风险规则单点（规格 §3.3）：逐日 precip_prob>=70，或 WMO 雨/雪/雷雨码 ----
#: 降水概率下限（「不因概率 70→71 重复通知」的分界在同一条规则里）
PRECIP_PROB_THRESHOLD = 70.0
#: WMO 码段 → 风险类别；与 app/agent/data/weather.py 的 _WMO_TEXT 同源同段，
#: 这里只摘「构成风险」的段——雨（含毛毛雨/阵雨）、雪（含阵雪）、雷雨
_RISK_SPANS: tuple[tuple[range, str], ...] = (
    (range(95, 100), "thunder"),
    (range(71, 78), "snow"),
    (range(85, 87), "snow"),
    (range(51, 58), "rain"),
    (range(61, 68), "rain"),
    (range(80, 83), "rain"),
)


@dataclass
class CheckOutcome:
    """一次检查的结论（判定管线 → 收尾落库的交接物）。"""

    #: 用户可见状态；None = 不改状态与结果（瞬态让路）
    status: str | None = None
    result: dict | None = None
    #: 终态（已出发/归档/删除/账号禁用）：next_check_at 置空，离开扫描
    stop: bool = False
    #: 瞬态资格不足（未完整交付等）：只释放争抢令牌，状态与节奏都不动
    release_only: bool = False
    #: 发布成功的短事务要持久化的去重键（失败/崩溃不得留键，规格 §4.4 步骤 8）
    risk_keys: list[str] = field(default_factory=list)


@dataclass
class _CheckFacts:
    """资格闸放行后的正式行程事实（规格 §4.4 步骤 2）：用户待确认草稿不在其中。"""

    owner_id: int
    city: str
    window_start: date
    window_end: date
    #: 监控项（存储 ID ∩ 当前 attraction）→ 所在日的 travel_date
    item_day: dict[int, date]
    #: 监控项 ID → 当前名称（草稿输入里点名的活动）
    item_name: dict[int, str]
    #: travel_date → day_no（把「日期」翻译成草稿里的「第 N 天」）
    day_no_by_date: dict[date, int]
    #: 上次有观测的 last_result（unavailable 时保留最后有效观测，不覆盖）
    previous_result: dict | None
    existing_keys: set[str]


def evaluate_check(itinerary_id: int, token: str) -> CheckOutcome | SuggestionOrder | None:
    """检查主体：资格 → 窗口 → 监控范围 → 天气 → 确定性风险 → 建议流程门槛。

    返回 None = 无声退出（授权已撤或锁已易主），什么都不写、不外呼；
    返回 `SuggestionOrder` = 门槛全过、当日尝试已占用，调用方接着跑模型流程。
    """
    today = shanghai_today()
    gate = _admit(itinerary_id, token, today)
    if not isinstance(gate, _CheckFacts):
        return gate  # None=无声退出；CheckOutcome=终态/瞬态/等待
    # 会话已关再做外呼：天气可能慢（带 6h 缓存），不占着写事务
    rows = get_weather_forecast(gate.city, gate.window_start.isoformat(), gate.window_end.isoformat())
    if rows is None:
        return _unavailable_outcome(gate)
    observations, risk_by_date = _observe(gate, rows)
    return _decide(itinerary_id, token, today, gate, observations, risk_by_date)


def _admit(itinerary_id: int, token: str, today: date) -> _CheckFacts | CheckOutcome | None:
    """资格闸与正式行程读取（规格 §4.4 步骤 1–2）：读 day/item 行，不读草稿。"""
    with session_scope() as session:
        row = session.get(ItineraryCare, itinerary_id)
        if row is None or not row.enabled or row.check_token != token:
            return None
        main = session.get(ItineraryMain, itinerary_id)
        if main is None or main.archived or (main.start_date is not None and main.start_date <= today):
            # 删除/归档/已出发：照看期结束，离开扫描（规格 §3.2「停止新的外呼」）
            return CheckOutcome(
                status=STATUS_FINISHED,
                result=result_payload(STATUS_FINISHED, "行程已出发或已归档，照看结束"),
                stop=True,
            )
        owner = session.get(SysUser, main.user_id)
        if owner is None or not owner.status:
            # 账号禁用：停止外呼；恢复后由用户重新开启（PUT 会重置 next_check_at）
            return CheckOutcome(
                status=STATUS_ACCOUNT_DISABLED,
                result=result_payload(STATUS_ACCOUNT_DISABLED, "账号已停用，照看暂停"),
                stop=True,
            )
        if eligibility_problem(main, session) or main.start_date is None or main.end_date is None:
            # 走到这里只剩瞬态原因（日期缺失/未完整交付）：保持任务等待，下次扫描再试
            return CheckOutcome(release_only=True)
        window_start = max(today + timedelta(days=1), main.start_date)
        window_end = min(today + timedelta(days=WINDOW_DAYS), main.end_date)
        if window_start > window_end:
            # 窗口外：保留任务等待进入窗口（规格 §3.2「在 3 日窗口外保留任务」），零外呼
            return CheckOutcome(status=STATUS_WAITING, result=result_payload(STATUS_WAITING, "将在出发前三天开始检查"))
        return _window_facts(session, row, main, window_start, window_end)


def _window_facts(
    session, row: ItineraryCare, main: ItineraryMain, window_start: date, window_end: date
) -> _CheckFacts | CheckOutcome:
    """窗口内的正式行程事实：监控项∩当前 attraction、item→日期/天次、既有去重键。"""
    itinerary_id = int(main.id)
    watched = {int(item) for item in (row.watched_item_ids_json or [])} & current_attraction_ids(session, itinerary_id)
    if not watched:
        # 规格 §3.2：已删除的 ID 不再监控，不按同名重绑；全部失效时禁止空范围生成
        return CheckOutcome(
            status=STATUS_NEEDS_RESELECT,
            result=result_payload(STATUS_NEEDS_RESELECT, "关注的活动已不在行程中，请重新选择"),
        )
    item_day: dict[int, date] = {}
    item_name: dict[int, str] = {}
    day_no_by_date: dict[date, int] = {}
    day_rows = session.execute(
        select(ItineraryDay.id, ItineraryDay.travel_date, ItineraryDay.day_no).where(
            ItineraryDay.itinerary_id == itinerary_id,
            ItineraryDay.travel_date >= window_start,
            ItineraryDay.travel_date <= window_end,
        )
    ).all()
    day_date_by_id = {int(day_id): (travel_date, int(day_no)) for day_id, travel_date, day_no in day_rows}
    if day_date_by_id:
        day_no_by_date = {travel_date: day_no for travel_date, day_no in day_date_by_id.values()}
        item_rows = session.execute(
            select(ItineraryItem.id, ItineraryItem.day_id, ItineraryItem.poi_name).where(
                ItineraryItem.itinerary_id == itinerary_id,
                ItineraryItem.id.in_(watched),
                ItineraryItem.day_id.in_(day_date_by_id),
            )
        ).all()
        for item_id, day_id, poi_name in item_rows:
            travel_date, _day_no = day_date_by_id[int(day_id)]
            item_day[int(item_id)] = travel_date
            item_name[int(item_id)] = str(poi_name or "")
    return _CheckFacts(
        owner_id=int(main.user_id),
        city=str(main.city),
        window_start=window_start,
        window_end=window_end,
        item_day=item_day,
        day_no_by_date=day_no_by_date,
        item_name=item_name,
        previous_result=row.last_result_json if isinstance(row.last_result_json, dict) else None,
        existing_keys={
            str(key) for key in (row.notified_risks_json if isinstance(row.notified_risks_json, list) else [])
        },
    )


def _unavailable_outcome(facts: _CheckFacts) -> CheckOutcome:
    """天气 None（规格 §3.3）：不是晴天、不产生安全结论，也不覆盖最后有效观测。"""
    previous = facts.previous_result if isinstance(facts.previous_result, dict) else {}
    return CheckOutcome(
        status=STATUS_UNAVAILABLE,
        result={
            "checkedAt": iso_datetime(datetime.now()),
            "status": STATUS_UNAVAILABLE,
            "reason": "暂未取得天气信息",
            "days": previous.get("days") or [],
            "affectedItemIds": previous.get("affectedItemIds") or [],
            "pendingMessageId": None,
        },
    )


def _observe(facts: _CheckFacts, rows: list[dict[str, object]]) -> tuple[list[dict[str, object]], dict[date, str]]:
    """逐日观测 + 确定性风险归类：只认产品窗口内的行（适配器可能带出窗外行）。"""
    observations: list[dict[str, object]] = []
    risk_by_date: dict[date, str] = {}
    for entry in rows:
        day = parse_iso_date(entry.get("date"))
        if day is None or not (facts.window_start <= day <= facts.window_end):
            continue
        category = _risk_category(entry.get("code"), entry.get("precip_prob"))
        if category is not None:
            risk_by_date[day] = category
        prob = entry.get("precip_prob")
        numeric_prob = float(prob) if isinstance(prob, (int, float)) and not isinstance(prob, bool) else None
        observations.append(
            {
                "date": day.isoformat(),
                "precipProb": numeric_prob,
                "weatherCode": entry.get("code"),
                "condition": entry.get("text"),
                "risk": category is not None,
            }
        )
    return observations, risk_by_date


def _decide(
    itinerary_id: int,
    token: str,
    today: date,
    facts: _CheckFacts,
    observations: list[dict[str, object]],
    risk_by_date: dict[date, str],
) -> CheckOutcome | SuggestionOrder:
    """去重判定 + 建议流程门槛（规格 §4.4 步骤 4）：前台草稿 → 额度 → 当日已尝试。

    门槛全过后原子占用当日 AI 尝试，返回 `SuggestionOrder`——模型流程（步骤 5–8）
    由 `itinerary_care_check._run_suggestion` 接手；本函数不再落任何持久状态。
    """
    affected = sorted(item_id for item_id, day in facts.item_day.items() if day in risk_by_date)
    affected_dates = {day for day in facts.item_day.values() if day in risk_by_date}
    fresh_keys = [
        key
        for key in (f"{day.isoformat()}:{risk_by_date[day]}" for day in sorted(affected_dates))
        if key not in facts.existing_keys
    ]
    if not fresh_keys:
        return CheckOutcome(
            status=STATUS_NO_RISK,
            result={
                "checkedAt": iso_datetime(datetime.now()),
                "status": STATUS_NO_RISK,
                "reason": _no_risk_reason(risk_by_date, affected_dates),
                "days": observations,
                "affectedItemIds": affected,
                "pendingMessageId": None,
            },
        )
    if _has_pending_user_draft(itinerary_id, facts.owner_id):
        # 规格 §3.3：有前台待确认草稿不发起后台模型调用、不顶掉草稿，下个周期重新判断
        return CheckOutcome(
            status=STATUS_WAITING_USER,
            result=_gate_result(observations, affected, STATUS_WAITING_USER, "先处理当前草稿，之后会自动重试"),
        )
    try:
        # 只读持久硬顶（token/调用数/熔断）：不预留不占格，真预留随模型流程的 RunLimits 走
        cost_admission.enforce_windows(facts.owner_id)
    except ApiError:
        return CheckOutcome(
            status=STATUS_QUOTA,
            result=_gate_result(observations, affected, STATUS_QUOTA, "未生成调整建议：额度不足"),
        )
    if not _claim_ai_day(itinerary_id, token, today):
        return CheckOutcome(
            status=STATUS_DEFERRED,
            result=_gate_result(observations, affected, STATUS_DEFERRED, "今日已尝试过自动建议，明天继续"),
        )
    return build_suggestion_order(
        item_day=facts.item_day,
        item_name=facts.item_name,
        day_no_by_date=facts.day_no_by_date,
        observations=observations,
        risk_by_date=risk_by_date,
        affected=affected,
        fresh_keys=fresh_keys,
    )


def _gate_result(
    observations: list[dict[str, object]], affected: list[int], status: str, reason: str
) -> dict[str, object]:
    """门槛未过的结果形状：观测照实记录，但没有建议消息。"""
    return {
        "checkedAt": iso_datetime(datetime.now()),
        "status": status,
        "reason": reason,
        "days": observations,
        "affectedItemIds": affected,
        "pendingMessageId": None,
    }


def _has_pending_user_draft(itinerary_id: int, owner_id: int) -> bool:
    """前台是否还有待确认草稿：与 apply 链同一判定（itinerary_chat.latest_action_message）。"""
    with session_scope() as session:
        return itinerary_chat.latest_action_message(session, owner_id, itinerary_id) is not None


def _claim_ai_day(itinerary_id: int, token: str, today: date) -> bool:
    """原子占用当日自动 AI 尝试（规格 §3.3「外呼前持久化占位」）。

    条件含 token：锁已易主（暂停/顶掉）时不占格——不为一次不会发布的流程烧掉当日名额。
    """
    with session_scope() as session:
        result = session.execute(
            update(ItineraryCare)
            .where(
                ItineraryCare.itinerary_id == itinerary_id,
                ItineraryCare.check_token == token,
                or_(ItineraryCare.last_ai_attempt_on.is_(None), ItineraryCare.last_ai_attempt_on < today),
            )
            .values(last_ai_attempt_on=today)
        )
    return getattr(result, "rowcount", 0) == 1


def _no_risk_reason(risk_by_date: dict[date, str], affected_dates: set[date]) -> str:
    if not risk_by_date:
        return "近期没有影响出行的降水风险"
    if not affected_dates:
        return "有降水风险的日子没有你关注的活动"
    return "这些风险已经提醒过，出现新变化会再次提醒"


def result_payload(status: str, reason: str) -> dict[str, object]:
    """无天气观测的结果形状（窗口外/需重选/终态/失败）：不编造逐日数据。"""
    return {
        "checkedAt": None,
        "status": status,
        "reason": reason,
        "days": [],
        "affectedItemIds": [],
        "pendingMessageId": None,
    }


def parse_iso_date(value: object) -> date | None:
    try:
        return date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError):
        return None


def _risk_category(code: object, precip_prob: object) -> str | None:
    """风险规则单点（规格 §3.3）：WMO 雨/雪/雷雨码，或逐日降水概率 ≥ 70。

    返回 None 只表示「无风险结论」：数据缺失（code/prob 均缺）由调用方按
    unavailable 处理，绝不冒充晴天。
    """
    try:
        wmo = int(str(code))
    except (TypeError, ValueError):
        wmo = None
    if wmo is not None:
        for span, category in _RISK_SPANS:
            if wmo in span:
                return category
    if isinstance(precip_prob, (int, float)) and not isinstance(precip_prob, bool):
        return "rain" if float(precip_prob) >= PRECIP_PROB_THRESHOLD else None
    return None
