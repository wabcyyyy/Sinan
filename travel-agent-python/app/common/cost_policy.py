"""成本准入的**纯决策**层（V17/T2，上线 v1「建立累计成本预留与结算机制」）。

这里只有规则与算式：没有 SQL、没有连接、没有 Redis。给定「账本现有值 + 这一笔要预留多少」，
回答三个问题——能不能发、级联记到哪些窗口、结算时把账调成多少。持久化与事务在
`app/services/cost_ledger.py`，注入接缝在 `app/agent/runtime/run_limits.py`。

四层限额各自的用途互不重叠（PLAN §5 T2「任务/行程/用户/平台窗口各有清楚用途」）：

- **任务**（一次交付责任）：权威在 `planning_task.consumed_*` + 快照 `max_*`（V16/T1），
  判调用次数 / token / 检索 / 重规划。挡的是"一次生成本身跑飞"：无限反思、无限补查、超长 prompt。
- **行程**（补齐额度）：权威在 `cost_ledger` 的 `trip:` 行，判补齐次数 + 连续失败退避。
  挡的是"同一行程反复失败重试"，以及「补齐免扣日配额」留下的缺口。
- **用户**（日窗）：权威在 `cost_ledger` 的 `user:` 行（按归属分行），判当日 token + 调用次数。
  挡的是"一个账号循环提交，把按次额度乘 N 份变成无上限"。
- **平台**（日总额）：权威在 `cost_ledger` 的 `platform:` 行，只收**运营方 key** 的当日
  token + 次数。这是全服务的硬止损：任意重试、任意账号都绕不过它。

费用硬顶是**绑在 T1 的累计账本之上**的：`planning_task.consumed_*` 是继承型上限
（`open_attempt` 用它播种，跨 attempt 只增），本模块负责的是它管不到的三件——
跨任务的窗口（用户/平台）、行程级补齐额度、以及「一笔请求」的持久生命周期。

**单位只有 token 与调用次数，没有人民币**：本仓没有可信定价表，`attribution` 只用于
归属与追责；没有可信定价时用 token 硬顶，不编造金额（PLAN §5 T2 原话）。

Redis 不参与本模块任何判据。速率（每分钟点几次）仍归 `quota_service` 的滑动窗，
钱与额度归 MySQL 账本——Redis 熔断或切回会让滑动窗重新计数，那是**放松速率**；
它绝不能让硬费用账归零，所以账不在那儿。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from app.common.config import settings
from app.common.envelope import ApiError

#: 账本窗口种类
PLATFORM = "platform"
USER = "user"
TRIP = "trip"
KINDS = (PLATFORM, USER, TRIP)

#: 计费归属：烧的是运营方的 key，还是用户自带网关（BYOK）
ATTR_PLATFORM = "platform"
ATTR_BYOK = "byok"
ATTR_ANY = "any"  # trip 行与归属无关（补齐额度、退避状态）
#: 归属降级原因：用户配了 BYOK 但网关表读不出来（基础设施故障）而回落到运营方 key。
#: 这一笔**确实**花了运营方的钱，所以账记 platform，但必须留下原因——静默转嫁正是
#: PLAN §5 T2 点名的「路由故障不能偷偷把费用转给平台」。
NOTE_BYOK_LOOKUP_FAILED = "byok_lookup_failed"

#: 预留状态机：OPEN 是唯一可结算的状态，因此一笔预留恰好结算一次
OPEN = "OPEN"
SETTLED = "SETTLED"
ABANDONED = "ABANDONED"

#: 稳定错误码（进 `planning_task.result_code`、SSE error 帧与 API message）
CODE_USER_LIMIT = "USER_COST_LIMIT"
CODE_PLATFORM_LIMIT = "PLATFORM_COST_LIMIT"
CODE_REPAIR_LIMIT = "TRIP_REPAIR_LIMIT"
CODE_REPAIR_BACKOFF = "TRIP_REPAIR_BACKOFF"
CODE_RESERVATION_FLOOD = "LLM_RESERVATION_FLOOD"


class CostCeilingError(ApiError):
    """成本硬顶拒绝：不再发放新请求（停止新请求的熔断，PLAN §5 T2）。

    继承 `ApiError`（429 + 中文用户文案）而不是 `RunLimitExceeded`，是刻意的类型区分
    （与 T1 的 `TaskStopped` 同一条纪律）：`RunLimitExceeded` 会被研究/生成段捕获并
    「带着已有证据诚实降级」继续交付，而硬费用顶必须让整条链停手、把稳定码回给用户——
    被降级分支吞掉等于熔断形同虚设。`app/services/itinerary_city.guard_agent_call`
    对 `ApiError` 原样透出，所以入口不会把 429 误裹成 502。
    `stable_code` 由 `envelope.error_code()` 读取，随 `result_code` / SSE error 帧一起出。
    """

    def __init__(self, stable_code: str, status: int, message: str) -> None:
        super().__init__(status, message)
        self.stable_code = stable_code


@dataclass(frozen=True)
class Ceiling:
    """一个窗口的上限（0 = 该维度不限；熔断状态与另一维度的上限不受影响）。"""

    token_cap: int
    call_cap: int


def ceiling_for(kind: str, attribution: str = ATTR_PLATFORM) -> Ceiling:
    """窗口上限。BYOK 腿只受**次数**顶（保护进程与连接池），不受 token 顶——
    那是用户自己的钱；但它照样走持久预留/结算/收口，所以崩溃与双结算的不变式一视同仁。
    """
    if kind == PLATFORM:
        return Ceiling(settings.platform_daily_token_cap, settings.platform_daily_call_cap)
    if kind == USER:
        if attribution == ATTR_BYOK:
            return Ceiling(0, settings.user_daily_call_cap)
        return Ceiling(settings.user_daily_token_cap, settings.user_daily_call_cap)
    return Ceiling(0, 0)  # trip 不重复计 token：行程级累计成本的权威是 planning_task.consumed_*


def day_of(at: datetime) -> int:
    """日窗编号 YYYYMMDD：跨日即换新行 = 窗口自然滚动，熔断不需要"解除"动作。"""
    return at.year * 10000 + at.month * 100 + at.day


def platform_scope(at: datetime) -> str:
    return f"{PLATFORM}:{day_of(at)}"


def user_scope(user_id: int, at: datetime, attribution: str) -> str:
    return f"{USER}:{user_id}:{day_of(at)}:{attribution}"


def trip_scope(itinerary_id: int) -> str:
    return f"{TRIP}:{itinerary_id}"


@dataclass(frozen=True)
class Scope:
    """一个账本窗口的完整身份：键 + 种类 + 日窗 + 归属。

    行内 `window_day`/`attribution` 列与键**同源派生**（不做第二次判断），运营才能按列查，
    而不必解析字符串。
    """

    key: str
    kind: str
    window_day: int
    attribution: str


def platform_window(at: datetime) -> Scope:
    return Scope(platform_scope(at), PLATFORM, day_of(at), ATTR_PLATFORM)


def user_window(user_id: int, at: datetime, attribution: str) -> Scope:
    return Scope(user_scope(user_id, at, attribution), USER, day_of(at), attribution)


def repair_window(itinerary_id: int) -> Scope:
    return Scope(trip_scope(itinerary_id), TRIP, 0, ATTR_ANY)


def parse_scope(key: str) -> Scope:
    """由键反推窗口身份（结算/收口按 `llm_reservation.charged_scopes` 回放到同一批行时用）。"""
    parts = key.split(":")
    kind = parts[0] if parts and parts[0] in KINDS else USER
    if kind == PLATFORM:
        return Scope(key, PLATFORM, int(parts[1]), ATTR_PLATFORM)
    if kind == TRIP:
        return Scope(key, TRIP, 0, ATTR_ANY)
    return Scope(key, USER, int(parts[2]), parts[3])


def scopes_for_reserve(user_id: int | None, attribution: str) -> list[Scope]:
    """这一笔预留级联记哪些窗口。**顺序固定**（platform → user）：
    多个事务按同一顺序取行锁才不会互相咬成死锁。

    BYOK 腿不进 platform 行——用户自带网关花的不是运营方的钱，算进平台日总额会让
    止损线被别人的消费推着走。
    """
    at = datetime.now()
    out: list[Scope] = []
    if attribution == ATTR_PLATFORM:
        out.append(platform_window(at))
    if user_id is not None:
        out.append(user_window(user_id, at, attribution))
    return out


def tokens_of(prompt: int, completion: int) -> int:
    return int(prompt or 0) + int(completion or 0)


def exceeds(row_tokens: int, row_calls: int, prompt: int, completion: int, ceiling: Ceiling) -> bool:
    """加上这一笔预留后是否触顶（0 = 该维度不限）。"""
    if ceiling.token_cap > 0 and row_tokens + prompt + completion > ceiling.token_cap:
        return True
    return bool(ceiling.call_cap > 0 and row_calls + 1 > ceiling.call_cap)


def open_reservations_full(open_count: int) -> bool:
    """未结算预留是否已到上限 = 崩溃循环的**有界性**判据（不依赖清理任务准时跑到）。"""
    return settings.cost_max_open_reservations > 0 and open_count >= settings.cost_max_open_reservations


def reservation_expires_at(created_at: datetime) -> datetime:
    """结算宽限到期时刻：取「供应商读超时 + 余量」量级。

    宽限太短会把慢响应误收口成 ABANDONED（真 usage 再也进不了账，且预留位凭空少一格），
    太长则崩溃后迟迟不腾出 `cost_max_open_reservations` 的格子。
    """
    grace = max(float(settings.llm_timeout) + 30.0, float(settings.cost_reservation_ttl_seconds))
    return created_at + timedelta(seconds=grace)


def settle_charge(reserved_prompt: int, reserved_completion: int, usage: dict | None) -> tuple[int, int, bool]:
    """结算口径 → `(入账 prompt, 入账 completion, usage 是否完整)`。

    供应商没回 / 回不全 → **原样保留预留值**（保守：宁可多算，绝不少算），并如实标
    `usage_complete=0`；对账时这一行不能当真实账单。真值低于预留 → 如实退差；
    高于预留 → 如实补记（超出部分仍由调用方原有的「供应商报告用量超过预留预算」告警兜住）。
    """
    if (
        usage is not None
        and isinstance(usage.get("prompt_tokens"), int | float)
        and isinstance(usage.get("completion_tokens"), int | float)
    ):
        return max(0, int(usage["prompt_tokens"])), max(0, int(usage["completion_tokens"])), True
    return max(0, int(reserved_prompt)), max(0, int(reserved_completion)), False


def backoff_seconds(failures: int) -> float:
    """补齐失败退避：base × 2^(failures-1)，封顶 max；failures<=0 表示不退避。

    挡的是「反复失败重试」把额度与线程池磨穿；封顶是为了让一次真实修复不必等到明天。
    """
    if failures <= 0:
        return 0.0
    base = float(settings.repair_backoff_base_seconds)
    return min(base * (2.0 ** (failures - 1)), float(settings.repair_backoff_max_seconds))


def repair_problem(repairs: int, backoff_until: datetime | None, at: datetime) -> str | None:
    """行程补齐准入的拒绝原因（稳定码），可放行时返回 None。

    这里**不**碰用户的「每日生成次数」——失败天免扣日配额是 P0-3 的产品硬线。
    T2 只在它之外补上「行程累计补齐额度 + 失败退避」，不是把那条线推翻。
    """
    if backoff_until is not None and at < backoff_until:
        return CODE_REPAIR_BACKOFF
    if settings.trip_repair_allowance > 0 and repairs >= settings.trip_repair_allowance:
        return CODE_REPAIR_LIMIT
    return None
