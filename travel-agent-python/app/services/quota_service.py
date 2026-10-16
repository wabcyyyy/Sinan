"""烧模型额度入口的准入闸门（业务轨：超限抛 ApiError(429)）。

两层闸门**分工明确**，不是同一件事做两遍（V17/T2 之后口径）：

1. **速率**（本文件，Redis 滑动窗 + 进程内兜底）：每分钟点几次、今天点了几"次"。
   Redis 熔断期退回进程内表、恢复后又从空计数重新开始（见 `state_and_sessions._bound_windows`
   自己写明的方向）——那是**放松速率**，可以接受。
2. **钱与额度**（`app/services/cost_ledger.py` 写账、`cost_admission.py` 判准入，MySQL
   `cost_ledger`）：今天到底花了多少
   token / 调用数，行程还能补几次，平台日总额有没有触顶熔断。这一层**不能**被 Redis 故障
   或切回清零，所以账不在 Redis（PLAN §5 T2）。逐笔的持久预留与结算在
   `RunLimits.reserve_llm/settle_llm` 里做（`app/agent/runtime/run_limits.py`），
   本文件调的 `cost_admission.enforce_windows` 只是让入口能干净地回 429，
   而不是等外呼打到一半才发现账本已经顶到头。

为什么只有本文件不够（历史口径，保留以便读懂旧注释）：`app/agent/runtime/run_limits.py`
那份预算是**按次**的（一次生成最多 32 次 LLM 调用 / 8 万 token；整趟行程
`ITINERARY_DEADLINE_SECONDS=300`、单次图运行 `AGENT_DEADLINE_SECONDS=240`——旧注释写的
"120 秒"早已不是事实），一个登录用户循环 POST `/api/itinerary/generate` 就能拿到 N 份
"每次都不超预算"的额度 → 无上限烧钱。全局有界线程池只保证不把进程拖死，
不保证单个用户花掉多少。

未做（明确登记）：按用户的**并发**在跑任务数。它需要一个租约/信号量原语
（获取—释放—超时回收）。V16/T1 的 `planning_task` 租约其实就是那个原语，
"同用户已有活任务 → 429" 也已经按持久面判（`planning_task_store.open_attempt`）；
本文件仍只判窗口，不去重复那套判定。
"""

from __future__ import annotations

from app.common.config import settings
from app.common.envelope import ApiError
from app.services import cost_admission, state_and_sessions

_MINUTE_WINDOW_SECONDS = 60
_DAY_WINDOW_SECONDS = 86400


def enforce_llm_budget(user_id: int) -> None:
    """消耗 LLM 的入口在动手之前调它：先过持久硬顶，再记一次并判分钟窗与日窗。

    日窗判的是"今日生成**次数**"（产品配额），不是金额；金额/用量硬顶由
    `cost_admission.enforce_windows` 按 token 与调用数判（没有可信定价，所以不编造人民币）。
    """
    cost_admission.enforce_windows(user_id)
    minute_key = f"quota:llm:min:{user_id}"
    day_key = f"quota:llm:day:{user_id}"
    per_minute = state_and_sessions.sliding_hit(minute_key, _MINUTE_WINDOW_SECONDS)
    if per_minute > settings.user_llm_runs_per_minute:
        raise ApiError(429, "操作过于频繁，请稍后再试")
    per_day = state_and_sessions.sliding_hit(day_key, _DAY_WINDOW_SECONDS)
    if per_day > settings.user_daily_llm_runs:
        raise ApiError(429, "今日 AI 生成次数已用完，请明天再试")


def enforce_llm_rate_only(user_id: int) -> None:
    """重生成未交付天（P0-3）的配额豁免口径：只过分钟窗，**不记不判日窗**。

    拍板（2026-10-07，审查报告 §3.8.3/P0-3 硬线「不能让用户为补齐从未交付的天
    再付一次配额」）：「重生成第 N 天」端点只接受 PENDING/FAILED/空天——这些天
    用户在首次生成时已经付过配额却没有拿到（部分交付的真实形态），再扣日窗等于
    为同一份交付付两次。选择「免扣」而非「事后回补」：回补需要跨请求记账（哪次
    扣的、退给谁），免扣只需本函数不碰日窗，是最小正确方案。

    滥用面有界（两层闸，不依赖日窗）：
    - 分钟窗照拦脚本式连点（与 enforce_llm_budget 同一只计数器）；
    - 端点语义侧每次成功重生成该天即转 SUCCEEDED，再点返回 400——单行程的免费
      重生成次数上限 = 未交付天数（≤7），把天重新变回未交付态需要真实的上游
      生成失败，不受用户控制。

    免扣的**只是**用户日次数。V17/T2 在上面这条线之外补了三层它管不到的东西：
    行程累计补齐额度、连续失败退避、用户/平台窗口硬顶——判定在
    `app/services/cost_admission.py:admit_repair`，调用点在
    `itinerary_generation.regenerate_day` 的前置校验之后（真动手补才扣一格，
    400/404/409 不消耗额度）。也就是说"补齐不重复收你的次数"这句话仍然为真，
    但它不再是"补齐不花钱、可以无限补"的借口。
    """
    key = f"quota:llm:min:{user_id}"
    if state_and_sessions.sliding_hit(key, _MINUTE_WINDOW_SECONDS) > settings.user_llm_runs_per_minute:
        raise ApiError(429, "操作过于频繁，请稍后再试")


def enforce_places_rate(user_id: int) -> None:
    """点位近邻/详情这类**外部地点 API** 腿的按用户分钟窗（不占 AI 生成次数）。

    `/api/itinerary/poi-nearby` 走 OpenTripMap / Nominatim 的免 key 或免费额度通道，
    一次调用**不烧模型**：旧实现让它去过 `enforce_llm_budget`，等于用户点一下"附近有什么"
    就被扣掉一次今日生成配额——那是把"哪条腿在花钱"说错了（PLAN §5 T2「补齐错误的限额
    注释与产品提示」）。这里只留速率闸，理由与 `enforce_live_quote_budget` 同族：
    免费源有配额与 1 rps 政策，需要挡脚本式连点，但不该计入 AI 生成的产品额度。
    """
    key = f"quota:places:min:{user_id}"
    if state_and_sessions.sliding_hit(key, _MINUTE_WINDOW_SECONDS) > settings.user_live_quotes_per_minute:
        raise ApiError(429, "查询过于频繁，请稍后再试")


def enforce_live_quote_budget(user_id: int) -> None:
    """按需实时价（quotes/live、hotel-quotes/live）的按用户分钟窗。

    这两个端点不走 LLM，但每点一次就烧一次 SerpApi 的**共享月池**
    （航班+酒店同池 250 次/月，见 data/live_quotes.py 的配额口径）。生成侧的
    `enforce_llm_budget` 盖不到它们——没有这道闸，一个登录用户循环点就能把整月
    额度打光，其他用户当天全部查不到价（审查 P1-8）。
    只判分钟窗不判日窗：与 LLM 生成不同，这里没有"今日已用完"的产品语义，
    真实用户也就偶尔查一次；分钟窗足够拦住脚本式连点。
    """
    key = f"quota:live-quotes:min:{user_id}"
    if state_and_sessions.sliding_hit(key, _MINUTE_WINDOW_SECONDS) > settings.user_live_quotes_per_minute:
        raise ApiError(429, "查询过于频繁，请稍后再试")


# 连通性测试的分钟窗上限。不走 settings：探测端点专属的紧闸（真人手动点不会一分钟
# 连发 6 次，上限取 5 = 第 6 次起拒），拉 env 键反而是配置面噪音（2026-10-02 终审修复，保守取整）。
_GATEWAY_TESTS_PER_MINUTE = 5


def enforce_gateway_test_budget(user_id: int) -> None:
    """BYOK 网关连通性测试（POST /api/llm-gateway/{id}/test）的按用户分钟窗。

    /test 是同步阻塞端点且会真实发起上游调用：没有这道闸，一个已认证用户并发
    打几十个指向黑洞地址的 /test（配合慢读超时）就能占满 anyio 线程池，拖死全部
    同步业务端点（2026-10-02 终审）。与 enforce_live_quote_budget 同口径只判分钟窗：
    测试没有"今日已用完"的产品语义；服务端侧另配 15s 短超时 + 不重试收窄单次占用。
    """
    key = f"quota:gateway-test:min:{user_id}"
    if state_and_sessions.sliding_hit(key, _MINUTE_WINDOW_SECONDS) > _GATEWAY_TESTS_PER_MINUTE:
        raise ApiError(429, "测试过于频繁，请稍后再试")
