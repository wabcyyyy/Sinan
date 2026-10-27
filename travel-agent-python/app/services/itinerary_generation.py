"""行程生成编排（移植自 Java `ItineraryGenerationService` + `ItineraryAsyncPlanner`）。

结构上照 Java 的分层：本文件只做**流程编排**——幂等原语在 `generation_gate`、事务写入在
`day_persistence`、富化在 `itinerary_enricher`、事件在 `generation_events`。

同进程化后的三处必要差异（都不是"顺手改"，是换了运行模型就必须跟着改）：

1. `@Async("generationExecutor")`（core2/max4/queue20/AbortPolicy）→ `_SlotExecutor`：
   Python 的 `ThreadPoolExecutor` 队列无界，直接用会把"生成队列已满 → 429"这条用户可见
   语义丢掉，所以用信号量把「在跑 + 排队」的总量卡在 24，提交点满即抛 `TaskRejected`。
2. agent 调用不再走 HTTP，但**每一次生成仍单独包 `observe_run`/`use_scene`**：
   `RunLimits`（90s deadline、LLM/token/检索/重规划预算）与 trace 全靠 ContextVar，
   不包就等于静默关掉预算与遥测。ContextVar 只能在同一 Context 里 set/reset，
   所以进入点必须在工作线程内部。
3. `run_plan_context` 今天就是**不带 trace** 被调用的（`/v1/plan-context` 只有 `@scene`），
   因此研究事件的 `data` 里没有 `runId`。这里保持不包，避免直调后凭空多出前端会看见的键。
"""

from __future__ import annotations

import json
import logging
import time
import unicodedata
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any

from pydantic import TypeAdapter, ValidationError
from sqlalchemy import select, update
from sqlalchemy.exc import OperationalError

from app.agent import (
    BUDGET_OVERAGE_MARK,
    RunLimits,
    current_limits,
    destination_problem,
    observe_run,
    phase_scope,
    run_generate_day,
    run_generate_trip_stream,
    run_plan_context,
    use_scene,
    use_task_limits,
    validate_plans,
)
from app.common import cache_store
from app.common.config import settings
from app.common.envelope import ApiError, error_code, user_reason
from app.common.llm_route import use_route
from app.common.task_pool import SlotExecutor, TaskRejected
from app.db.models import ItineraryDay, ItineraryItem, ItineraryMain
from app.db.session import session_scope
from app.schemas.business.itinerary import GenerateTripRequest
from app.schemas.stream_events import StreamEvent
from app.schemas.trip import MAX_TRIP_DAYS, DailyPlan, GenerateDayRequest, GenerateRequest
from app.schemas.trip_requirements import (
    TripRequirements,
    canonical_requirements_payload,
    requirements_issues,
)
from app.services import (
    budget_engine,
    constraint_planning,
    cost_admission,
    day_persistence,
    generation_events,
    generation_gate,
    itinerary_enricher,
    itinerary_query,
    itinerary_version,
    llm_gateway_service,
    planning_task_runs,
    planning_tasks,
    stay_hotels,
)

logger = logging.getLogger(__name__)

MAX_STREAM_CONTRACT_ERRORS = 3
# M5a：day_item_preview / day_item_preview_withdrawn 是前向兼容的附加事件
# （候选预览，不落库），随既有六类一起进白名单。
KNOWN_STREAM_TYPES = (
    "start",
    "day",
    "day_patch",
    "suggestions",
    "done",
    "error",
    "day_item_preview",
    "day_item_preview_withdrawn",
)

# Java AsyncConfig：generationExecutor core2/max4/queue20、enricherExecutor core1/max2/queue10
_GENERATION_SLOTS = 4 + 20
_ENRICHER_SLOTS = 2 + 10


class _StageClock:
    """M5b 阶段计时（spec §11 指标）：submit 起各里程碑的 monotonic 分段耗时。

    经业务事件通道（generation_events.stage_timing）发布：plan_days 大部分区段
    刻意不包 observe_run（研究段与 /v1/plan-context 同口径），agent 面
    record_event 在无 recorder 上下文时是 no-op，业务帧才是这条编排线程里
    真正可达的观测面。只记录、不断言——历史 152 秒不当自动基线，不设任何
    性能阈值；同一名义阶段只发首帧（first_day 的整段/逐日两条落库路径都调 mark）。
    tokens/请求次数沿用既有用量上报（llm_client → metrics），此处不重造。
    """

    __slots__ = ("_fired", "_itinerary_id", "_submit")

    def __init__(self, itinerary_id: int, started_at: float | None = None) -> None:
        self._itinerary_id = itinerary_id
        self._submit = started_at if started_at is not None else time.monotonic()
        self._fired: set[str] = set()

    def mark(self, stage: str) -> None:
        if stage in self._fired:
            return
        self._fired.add(stage)
        generation_events.stage_timing(self._itinerary_id, stage, (time.monotonic() - self._submit) * 1000)


# 与池抛出的异常同一个类；池本身共用 app.common.task_pool（导出任务也用同一个原语）
_SlotExecutor = SlotExecutor


generation_pool = _SlotExecutor("travel-generation", 4, _GENERATION_SLOTS)
enricher_pool = _SlotExecutor("travel-enricher", 2, _ENRICHER_SLOTS)

# ---------- 执行所有权（V16/T1：判据在持久租约，不在本进程） ----------

# "这趟行程有没有人在跑"读 `planning_task`：state∈(QUEUED,RUNNING) 且租约未过期。
# 租约从提交一直维持到富化释放（心跳 + 每次预算写入续租），总时限由绝对 deadline 表达；
# 进程内的句柄表只用于取本地 RunLimits 与定时器，不再被任何判定当作唯一依据。
# 多进程/多实例部署因此不需要再改这套判据——它本来就是数据库租约（当年注释里
# "单进程前提，多实例需升级为 DB 租约"的那条 TODO 到此结束）。


@dataclass
class GenerateCommand:
    """编排层需要的生成参数（Java 侧就是 `GenerateRequest` DTO）。

    `stay_nights` 在建壳时已归一（None → max(days-1, 0)），指纹与逐日 `needs_hotel`
    都读它，因此必须在**归一之后**再构造一次。
    """

    city: str
    days: int
    persons: int
    stay_nights: int
    start_date: date | None = None
    end_date: date | None = None
    budget: Decimal | None = None
    preferences: list[str] = field(default_factory=list)
    hotel_tier: str | None = None
    region_hint: str | None = None
    requirements: str | None = None
    intent: str | None = None
    # L14：可选出发地；有它才查航班（未映射 IATA 时航段如实缺席）
    origin_city: str | None = None
    # M1a：结构化需求单一真源（与自然语言 requirements 原话并存，语义以本字段为准）。
    # None = 请求未携带；建壳时 canonical 序列化进 requirements_json（全默认写 '{}'）
    requirements_struct: TripRequirements | None = None
    # 指纹口径标记（不进指纹哈希输入）：恢复重建旧行程（requirements_json IS NULL）
    # 时由 rebuild_request 置 True，request_fingerprint 按迁移前 v1 口径计算，
    # 与存量指纹逐字节一致——口径升级不得把存量恢复/重生成打成 409。新行程恒 False。
    legacy_fingerprint: bool = False

    def resolved_intent(self) -> str:
        """意图兜底：未填 intent 时用 requirements（M1 契约，与 Java `resolveIntent` 一致）。"""
        if self.intent and self.intent.strip():
            return self.intent.strip()
        return self.requirements or ""

    def to_generate_request(self) -> GenerateRequest:
        """编排 → 研究层的完整请求投影（M1b）。

        此前 run_plan_context 只收 city/preferences 并在内部以 persons=1 重建
        GenerateRequest——研究层永远看不见真实人数/预算/酒店档次/结构化需求。
        现在由 command 单点转换，int 沿命令的真实值透传，不留丢字段适配。
        """
        return GenerateRequest(
            city=self.city,
            days=self.days,
            persons=self.persons,
            budget=None if self.budget is None else float(self.budget),
            start_date=None if self.start_date is None else self.start_date.isoformat(),
            preferences=self.preferences,
            hotel_tier=self.hotel_tier,
            requirements=self.requirements,
            intent=self.resolved_intent(),
            region_hint=self.region_hint,
            origin_city=self.origin_city,
            requirements_struct=self.requirements_struct,
        )


def submit_planning(
    user_id: int,
    itinerary_id: int,
    command: GenerateCommand,
    context: dict[str, Any] | None = None,
    limits: RunLimits | None = None,
) -> None:
    """把一次生成排进有界池；满了直接抛 `TaskRejected`（端点转 429）。

    `context` = 已产出的研究上下文（PR-3 起恢复侧从检查点找回再喂进来，免"僵尸续跑
    = 整段研究重跑"）；None 由 plan_days 自己跑 `run_plan_context`。

    排队准入（V16/T1）：`limits=None` 时在 `planning_task` 上开一个新的执行尝试
    （QUEUED + 绝对截止 + 租约 + 累计账本继承）；传入 limits 只可能是恢复接管的结果，
    用 `attach` 重新绑定同一份所有权——两条路径都不再看进程内活跃表，所以重启后
    "谁在跑这趟行程"仍然问得出答案。最坏静默 19 分钟的研究段不会被误判成僵尸双跑。
    """
    if limits is None:
        limits = planning_tasks.acquire(user_id, itinerary_id)
    else:
        planning_tasks.attach(limits)
    try:
        generation_pool.submit(plan_days, user_id, itinerary_id, command, context, limits)
    except BaseException:
        planning_tasks.release(itinerary_id, limits.task_id)
        raise


# ---------- 编排主体（工作线程内） ----------

#: 研究上下文里的需求指纹键（M1b）：plan_days 新研究后写入，恢复复用前比对
_CONTEXT_FP_KEY = "_requirements_fp"


def plan_days(
    user_id: int,
    itinerary_id: int,
    command: GenerateCommand,
    context: dict[str, Any] | None = None,
    limits: RunLimits | None = None,
) -> None:
    # BYOK 路由（2026-10-02 拍板）：contextvars 不跨线程池传播，必须在显式持 user_id 的
    # 工作线程入口内进入。plan_days 一处覆盖研究段/整段流式/逐日兜底全部 agent 调用；
    # 网关失败响亮报错、不回退默认通道（成本不转嫁 + 模型家族不割裂）。
    # 路由解析先于 with 单独兜 ApiError：密文解不开（加密 key 轮换）若从 route_scope
    # 直接抛出，会绕过下方 try 的失败收尾（fail_trip / SSE 错误 / finally 解注册），
    # 路由失败也要释放规划租约。timing 从共享预算的提交时刻起算，包含排队耗时。
    """执行研究、整趟流式生成与违规日期补齐，统一收敛交付状态。

    研究上下文仅在需求指纹一致时复用；终检重置的日期进入单日图，
    其余已交付日期保留。core_ready 先于后台富化发布，失败走既有
    PARTIAL/FAILED 收敛并保留错误信息。

    Args:
        user_id: 行程所有者，用于选择模型路由与费用归属。
        itinerary_id: 已创建的行程标识。
        command: 本次生成的固定需求快照。
        context: 可复用的研究上下文；过期时重新研究。
        limits: 从提交时刻延续的共享预算；缺省时取得任务预算。
    """
    limits = (
        limits
        if limits is not None
        else planning_task_runs.task_limits(itinerary_id) or planning_tasks.acquire(user_id, itinerary_id)
    )
    timing = _StageClock(itinerary_id, limits.started_at)
    try:
        route = llm_gateway_service.resolve_route(user_id)
    except ApiError as exc:
        logger.error("byok route broken for itinerary %s", itinerary_id, exc_info=True)
        generation_events.error(itinerary_id, "AGENT_ERROR", user_reason(exc), True)
        day_persistence.fail_trip(itinerary_id, str(exc) or None, user_facing=True)
        itinerary_query.evict_detail(user_id, itinerary_id)
        timing.mark("terminal")
        planning_tasks.release(itinerary_id, limits.task_id)
        return
    from app.common.trial_billing import payer_scope

    with use_route(route), use_task_limits(limits), payer_scope(user_id):
        try:
            limits.check("deadline")
            if not _shell_exists(itinerary_id):
                logger.error("itinerary shell %s not visible before planning; abort", itinerary_id)
                raise ApiError(500, "行程壳数据不存在，无法开始生成")
            day_persistence.mark_generating(itinerary_id)
            planning_tasks.mark_running(itinerary_id)

            used_names: list[str] = []
            used_points: list[tuple[str, str, float | None, float | None]] = []
            chosen_hotel: str | None = None
            first_day_suggestions: list[Any] | None = None
            fingerprint = generation_gate.request_fingerprint(command)
            # M1b（spec §6.5）：恢复复用研究上下文前校验需求指纹——参数/需求变了就弃用
            # 旧 context 重新研究，不相干的证据不能续用到新需求上。旧 checkpoint 的
            # context 无此键（部署前生成），按旧宽容口径继续可用。
            if context is not None and context.get(_CONTEXT_FP_KEY) not in (None, fingerprint):
                logger.warning(
                    "research context fingerprint mismatch for itinerary %s; discarding stale context", itinerary_id
                )
                context = None
            constraint_mode = bool(command.requirements_struct and command.requirements_struct.constraint_planning)
            if context is None and constraint_mode:
                from app.services import constraint_context

                context = constraint_context.load_context(user_id, itinerary_id, fingerprint)
            if context is None:
                # 不包 observe_run：与迁移前的 /v1/plan-context 口径一致（研究事件不带 runId）。
                # 但 scene 必须归位（P1-7）：研究段的 LLM 调用此前落 scene="other"，
                # 用量报表把"生成的研究成本"记到匿名桶里。
                # M1b：完整请求进研究层（真实 persons/budget/hotel_tier/结构化需求，
                # 不再由 plan_context 内部 persons=1 重建）。
                with (
                    use_scene("research"),
                    observe_run(request_id=f"itinerary-{itinerary_id}"),
                    phase_scope(settings.research_deadline_seconds),
                ):
                    context = run_plan_context(command.to_generate_request(), itinerary_id=itinerary_id)
                context[_CONTEXT_FP_KEY] = fingerprint
                if constraint_mode:
                    constraint_context.save_context(user_id, itinerary_id, fingerprint, context)
            # M5b：首个研究上下文可用（本轮返回或恢复复用）即记 research 里程碑
            timing.mark("research")
            # L14：报价随研究上下文一次落库；空即写 NULL（续跑取回的 context 同样带它）
            day_persistence.save_flight_quotes(itinerary_id, context.get("flight_quotes"))
            day_persistence.save_origin_city(itinerary_id, command.origin_city)

            if command.requirements_struct and command.requirements_struct.fact_source == "chinatravel":
                from app.services import complete_travel

                complete_travel.plan_trip(user_id, itinerary_id, command, context, fingerprint)
                _finish(user_id, itinerary_id, command)
                return

            suggestions_persisted = False
            unfinished = day_persistence.unfinished_day_nos(itinerary_id)
            fresh_trip = len(unfinished) >= command.days
            constraint_mode = bool(command.requirements_struct and command.requirements_struct.constraint_planning)
            if constraint_mode:
                constraint_planning.plan_trip(user_id, itinerary_id, command, context, fingerprint, timing)
            elif fresh_trip:
                try:
                    suggestions_persisted = _plan_whole_trip(
                        user_id, itinerary_id, command, context, fingerprint, timing=timing
                    )
                except Exception as stream_exc:
                    logger.warning("whole-trip stream failed for itinerary %s: %s", itinerary_id, stream_exc)
                # 整段流式的后置终检（stream 产出的天不再零校验直落库）：对已落库天跑
                # validate_plans，违规天重置 PENDING，交下方既有逐日兜底循环重生成。
                _revalidate_stream_days(itinerary_id, command, fingerprint)

            for day_no in range(1, command.days + 1):
                if constraint_mode:
                    continue  # 当前候选无解/未知不触发另一条 LLM 重试，保留求解报告并收敛为 PARTIAL。
                action_id = f"day-{itinerary_id}-{day_no}"
                if not generation_gate.try_day_lock(itinerary_id, day_no):
                    logger.warning("day lock busy, skip itinerary %s day %s", itinerary_id, day_no)
                    continue
                try:
                    day = day_persistence.find_day(itinerary_id, day_no)
                    if day is None:
                        raise ApiError(500, "行程日不存在")
                    if day.generation_status == "SUCCEEDED":
                        # 已成功的天只做幂等校验与跨天去重登记，不重写（也跳过收尾失效：无变更）
                        generation_gate.verify_action(day, action_id, fingerprint)
                        day_persistence.append_existing_items(day.id, used_names, used_points)
                        if chosen_hotel is None:
                            chosen_hotel = day_persistence.existing_hotel(day.id)
                        continue
                    repair_feedback = day.generation_error
                    day_persistence.mark_running(day.id, action_id, fingerprint)
                    generation_events.day_start(itinerary_id, day_no)
                    try:
                        plan = _generate_day_with_trace(
                            itinerary_id,
                            command,
                            context,
                            day_no,
                            used_names,
                            chosen_hotel,
                            action_id,
                            fingerprint,
                            feedback=repair_feedback,
                        )
                        timing.mark("first_day")
                        if day_no == 1:
                            first_day_suggestions = plan.suggestions
                            day_persistence.set_trip_theme(itinerary_id, plan.trip_theme)
                    except Exception as day_exc:
                        # API-1：str(exc) 对连接层异常（ConnectTimeout）是空串，日级
                        # 失败原因退回错误码保证可归因；degraded 帧带同一错误码。
                        day_persistence.mark_failed(day.id, action_id, fingerprint, str(day_exc) or error_code(day_exc))
                        generation_events.degraded(
                            itinerary_id,
                            f"day_{day_no}",
                            f"{user_reason(day_exc)}（错误码 {error_code(day_exc)}）",
                            "待重试",
                        )
                        raise
                    persisted = day_persistence.find_day(itinerary_id, day_no)
                    if persisted is not None:
                        day_persistence.append_existing_items(persisted.id, used_names, used_points)
                        if chosen_hotel is None:
                            chosen_hotel = day_persistence.existing_hotel(persisted.id)
                finally:
                    generation_gate.release_day_lock(itinerary_id, day_no)
                itinerary_query.evict_detail(user_id, itinerary_id)
                _submit_budget_recalculate(itinerary_id)

            if not suggestions_persisted:
                itinerary_enricher.persist_suggestions(itinerary_id, first_day_suggestions)
            # M5b（spec §11）：core_ready 中间里程碑——全部天落库且终检后无 PENDING 天
            # （终检违规天已被 _revalidate_stream_days 重置、由上方逐日循环重生成）
            # 才发布；仍有未完成天 = 交付不齐，不发，走既有 PARTIAL/FAILED 终态路径。
            # 发布点在提交富化池（_finish 内）之前：此后本任务只余收尾写（摊铺/预算/
            # 终态列/版本快照，均非核心内容改写），核心内容不再变更。
            if not day_persistence.unfinished_day_nos(itinerary_id):
                _publish_core_ready(itinerary_id, timing)
            _finish(user_id, itinerary_id, command)
            timing.mark("terminal")
        except Exception as exc:
            logger.error("async planning failed for itinerary %s", itinerary_id, exc_info=True)
            # R2-F3：原始异常文本（含上游 URL）只进日志；SSE 错误帧与 plan_note 都给用户文案。
            # API-1：统一文案在通道级故障（如 LLM 网关连不上、100% 失败）时无诊断价值，
            # 稳定错误码（异常类别）随 error 帧与 plan_note 透出，可按码与 usage 库对归因。
            planning_tasks.finish_failure(itinerary_id, exc)
            itinerary_query.evict_detail(user_id, itinerary_id)
            timing.mark("terminal")
        finally:
            planning_tasks.release(itinerary_id, limits.task_id, core_only=True)


def _other_completed_points(itinerary_id: int, day_no: int) -> list[tuple[str, str, float | None, float | None]]:
    """修复早期一天时也避开后面保留的成功日；失败草稿不占去重名额（名字+类型+坐标）。

    坐标进跨天近距离判重（P1-B）：单日重试只带名字时，名称变体绕过坐标通道。
    """
    with session_scope() as session:
        rows = session.execute(
            select(ItineraryItem.poi_name, ItineraryItem.item_type, ItineraryItem.latitude, ItineraryItem.longitude)
            .join(ItineraryDay, ItineraryDay.id == ItineraryItem.day_id)
            .where(
                ItineraryItem.itinerary_id == itinerary_id,
                ItineraryDay.day_no != day_no,
                ItineraryDay.generation_status == "SUCCEEDED",
            )
            .order_by(ItineraryDay.day_no, ItineraryItem.sort_no)
        ).all()
    return [
        (name, str(item_type or ""), latitude, longitude)
        for name, item_type, latitude, longitude in rows
        if name and name.strip()
    ]


def _generate_day_with_trace(
    itinerary_id: int,
    command: GenerateCommand,
    context: dict[str, Any],
    day_no: int,
    used_names: list[str],
    chosen_hotel: str | None,
    action_id: str,
    fingerprint: str,
    used_points: list[tuple[str, str, float | None, float | None]] | None = None,
    feedback: str | None = None,
) -> DailyPlan:
    retained_points = _other_completed_points(itinerary_id, day_no)
    # 坐标随请求进 context（P1-B）：单日重试时已成功天的真实坐标参与近距离判重，
    # 名称变体（同建筑不同写法）不能再靠"只有名字、没有坐标"绕过跨天去重。
    # 只改请求侧副本，不写回共享研究上下文与 checkpoint。
    request_context = dict(context or {})
    if used_points or retained_points:
        request_context["used_points"] = [*(used_points or []), *retained_points]
    request = GenerateDayRequest(
        city=command.city,
        persons=command.persons,
        budget=None if command.budget is None else float(command.budget),
        start_date=None if command.start_date is None else str(command.start_date),
        day_no=day_no,
        # BIZ-2：补传真实天数修预算分档（此前不传 days，¥7000/7 天被当成 ¥7000/天
        # 选奢华档）；per_day_chain=True 保住单日链的重试预算（不再从 days 推断）
        days=command.days,
        per_day_chain=True,
        used_names=list(dict.fromkeys([*used_names, *(name for name, _item_type, _lat, _lng in retained_points)])),
        hotel_tier=command.hotel_tier,
        preferences=command.preferences,
        chosen_hotel=chosen_hotel,
        needs_hotel=day_no <= command.stay_nights,
        requirements=command.requirements,
        intent=command.resolved_intent(),
        # M1b：结构化需求随逐日请求进图状态/checkpoint，prompt 层按 day_no 投影渲染
        requirements_struct=command.requirements_struct,
        region_hint=command.region_hint,
        request_id=f"itinerary-{itinerary_id}",
        action_id=action_id,
        context=request_context,
        feedback=feedback or "",
    )
    # days 用于预算分档；per_day_chain=True 保留逐日修复的反思重试预算。
    with use_scene("generate"), observe_run(request_id=f"itinerary-{itinerary_id}", action_id=action_id):
        plan = run_generate_day(request)
    day_persistence.persist(itinerary_id, command, day_no, plan, action_id, fingerprint)
    generation_events.day_done(itinerary_id, day_no, plan.theme, len(plan.items or []), plan.note)
    return plan


# ---------- 一键重生成第 N 天（P0-3 部分交付自救） ----------


def regenerate_day(user_id: int, itinerary_id: int, day_no: int) -> dict[str, Any]:
    """重生成第 N 天：业务面暴露的「真·LLM 按天重生成」（审查 §3.2.1/§3.8.3/P0-3）。

    与既有入口的边界（语义拍板，2026-10-07）：
    - 只允许 PENDING / FAILED 天重新生成（「空天」就是状态列默认值的 PENDING 天，
      generation_status V1 起 NOT NULL DEFAULT 'PENDING'）；已成功天返回 400——
      本端点是对「从未交付」的补救，不是对已交付结果的改写入口（对已有内容做
      增删改走 chat-edit / nl-edit / optimize_day，那是它们各自的产品职责）。
    - 不设「当天不足 2 个点位」限制：那是 optimize_day（确定性重排，不引入点位）
      的前件；本端点恰恰要接住空天——部分交付的真实形态就是天在、内容空
      （.night-audit 行程 189 第二天 PENDING 且为空）。
    - 配额走 quota_service.enforce_llm_rate_only（端点层）：只拦分钟窗不扣日窗，
      用户不为补齐从未交付的天再付一次配额（报告 §3.8.3 硬线）。
    - 同步执行（与 chat-edit 同纪律：请求线程内跑一次单日生成闭环），返回最新
      详情；进度经既有 day_start/day_done/degraded/complete 事件推给 SSE 订阅者。
    - 三重防双跑：日锁（与 plan_days/流式落库互斥）+ 指纹幂等（verify_action，
      参数变化 409）+ 活跃注册表（恢复扫描的存活探测据此跳过，见 generation_recovery）。
    """
    main = itinerary_query.find_owned_main(user_id, itinerary_id)
    if TripRequirements.model_validate(main.requirements_json or {}).constraint_planning:
        raise ApiError(400, "此行程采用整趟约束规划，请调整需求后重新规划；不支持单日模型重生成")
    if main.gen_state == "GENERATING" or main.status == 1 or planning_tasks.is_active(itinerary_id):
        # 在跑的 plan_days / 恢复续跑拥有这趟行程（含各未完成天），此刻抢跑同一天
        # 只会互相覆盖；用户等它终态后再来自救。
        raise ApiError(409, "行程正在生成中，请等本次生成结束后再重试")

    day = day_persistence.find_day(itinerary_id, day_no)
    if day is None:
        raise ApiError(404, "行程日不存在")
    if day.generation_status == "SUCCEEDED":
        raise ApiError(400, "这一天已生成成功，无需重新生成；调整内容请用「AI 编排此日」或对话补写")
    if day.generation_status == "RUNNING":
        raise ApiError(409, "这一天正在生成中，请勿重复操作")

    # 补齐额度（V17/T2）：前置校验都过了、真的要用 LLM 重补这一天，才扣行程的一格补齐额度。
    # 顺序是刻意的——400/404/409 这些"这次根本没动手"的请求不允许消耗额度；
    # 免扣的用户「今日生成次数」保持在端点侧（quota_service.enforce_llm_rate_only）。
    cost_admission.admit_repair(user_id, itinerary_id)
    limits = planning_tasks.acquire(user_id, itinerary_id)
    locked = False
    repaired = False
    try:
        locked = generation_gate.try_day_lock(itinerary_id, day_no)
        if not locked:
            raise ApiError(409, "这一天正在生成中，请稍后再试")
        # 恢复侧同源：从主表反推生成参数（origin_city/intent/requirements 一并读回），
        # 指纹与首次生成一致，幂等门不会把这次补齐拒在门外。函数内导入防环：
        # generation_recovery 模块级 import 本模块，反向只能在运行期解析。
        from app.services.generation_recovery import rebuild_request

        command = rebuild_request(main)
        fingerprint = generation_gate.request_fingerprint(command)
        action_id = f"day-{itinerary_id}-{day_no}"
        generation_gate.verify_action(day, action_id, fingerprint)
        day_persistence.mark_running(day.id, action_id, fingerprint)
        generation_events.day_start(itinerary_id, day_no)
        with use_task_limits(limits):
            _regenerate_day_core(user_id, itinerary_id, day, command, day_no, action_id, fingerprint)
            # 成功收尾与 plan_days 共用 _finish：预算是交付的一部分、终态按天状态重算
            # （补齐最后缺口天时行程升级 COMPLETED）、版本快照与 SSE 终帧照发。
            _finish(user_id, itinerary_id, command, snapshot_summary=f"重生成第 {day_no} 天")
        repaired = True
        return itinerary_query.detail(user_id, itinerary_id)
    finally:
        # 退避状态（V17/T2）：失败一次就把下一次补齐推后（指数、封顶），成功即清零。
        # 挡的是"反复点补齐把额度与线程池磨穿"，不是挡真实用户的第二次尝试。
        cost_admission.record_repair_result(itinerary_id, succeeded=repaired)
        planning_tasks.release(itinerary_id, limits.task_id, core_only=True)
        if locked:
            generation_gate.release_day_lock(itinerary_id, day_no)


def _regenerate_day_core(
    user_id: int,
    itinerary_id: int,
    day: ItineraryDay,
    command: GenerateCommand,
    day_no: int,
    action_id: str,
    fingerprint: str,
) -> None:
    """regenerate_day 的工作段：研究 + 单日生成 + 失败落态（拆出以守住规模门禁）。

    失败语义：天标记 FAILED 并发 degraded 帧，但**不动行程终态**——其余天不受这次
    自救失败牵连（行程进本入口前已是终态）；原始异常只进日志，用户拿到文案 + 错误码
    （R2-F3/API-1，与 plan_days 的日级失败同口径）。
    """
    # chosen_hotel 沿用 plan_days 的口径：首个成功天的酒店——重生成某天不让
    # 跨天同住一店的产品语义被打散。
    chosen_hotel = day_persistence.existing_hotel(sid) if (sid := _first_succeeded_day_id(itinerary_id)) else None
    try:
        with llm_gateway_service.route_scope(user_id):
            # 研究上下文不落库，重生成与「无检查点的恢复」同口径重跑一次研究
            # （恢复腿 submit_planning(context=None) 也是这么做的），保证补齐天
            # 与首次生成同一质量；scene 归位口径同 plan_days。
            with (
                use_scene("research"),
                observe_run(request_id=f"itinerary-{itinerary_id}"),
                phase_scope(settings.research_deadline_seconds),
            ):
                context = run_plan_context(command.to_generate_request(), itinerary_id=itinerary_id)
            context[_CONTEXT_FP_KEY] = fingerprint
            _generate_day_with_trace(itinerary_id, command, context, day_no, [], chosen_hotel, action_id, fingerprint)
    except ApiError as api_exc:
        # BYOK 路由坏（409）等用户可读失败：原样透出，不裹成 502。
        day_persistence.mark_failed(day.id, action_id, fingerprint, str(api_exc) or None)
        generation_events.degraded(
            itinerary_id, f"day_{day_no}", f"{user_reason(api_exc)}（错误码 {error_code(api_exc)}）", "待重试"
        )
        raise
    except Exception as day_exc:
        day_persistence.mark_failed(day.id, action_id, fingerprint, str(day_exc) or error_code(day_exc))
        generation_events.degraded(
            itinerary_id, f"day_{day_no}", f"{user_reason(day_exc)}（错误码 {error_code(day_exc)}）", "待重试"
        )
        logger.error("day regeneration failed for itinerary %s day %s", itinerary_id, day_no, exc_info=day_exc)
        raise ApiError(502, f"{user_reason(day_exc)}（错误码 {error_code(day_exc)}）") from day_exc
    finally:
        itinerary_query.evict_detail(user_id, itinerary_id)


def _first_succeeded_day_id(itinerary_id: int) -> int | None:
    """首个已成功天的行 id（chosen_hotel 口径用；无成功天返回 None）。"""
    with session_scope() as session:
        return session.execute(
            select(ItineraryDay.id)
            .where(ItineraryDay.itinerary_id == itinerary_id, ItineraryDay.generation_status == "SUCCEEDED")
            .order_by(ItineraryDay.day_no)
            .limit(1)
        ).scalar_one_or_none()


# ---------- 整段流式（一次 LLM 生成全程，边流边落库） ----------

_stream_events = TypeAdapter(StreamEvent)


def _match_preview_mappings(
    item_rows: list[tuple[int, str]], preview_entries: list[dict[str, str]]
) -> list[dict[str, Any]]:
    """按 poi_name 把该日候选对到正式条目（条目序遍历、候选先到先配）。

    预览与正式快照之间有反驳剔除/排程重排——序号不保真，存活条目的名字不变，
    名字是持久化时刻唯一可靠的内容身份。重名候选只配第一个；条目没对上候选
    （修复补的点）与候选没对上条目（被淘汰）都不进映射，缺席即如实陈述。
    """
    mappings: list[dict[str, Any]] = []
    consumed: set[int] = set()
    for item_id, poi_name in item_rows:
        name = (poi_name or "").strip()
        if not name:
            continue
        for idx, entry in enumerate(preview_entries):
            if idx in consumed:
                continue
            if entry.get("name", "").strip() == name:
                consumed.add(idx)
                mappings.append(
                    {
                        "previewId": str(entry.get("previewId") or ""),
                        "itemId": int(item_id),
                        "poiName": name,
                    }
                )
                break
    return mappings


def _emit_day_preview_mapping(
    itinerary_id: int,
    day_no: int,
    event_type: str,
    previews_by_day: dict[int, list[dict[str, str]]],
    mapping_emitted: set[int],
) -> None:
    """流式天正式落库后发一次 previewId→itemId 映射帧（M5a 遗留收口）。

    只对 `day` 事件发（day_patch 是酒店摊铺等修补重发，不重映射）；无候选登记
    的天（逐日兜底腿）不发。映射失败绝不影响生成主循环——尽力而为帧。
    """
    if event_type != "day" or day_no in mapping_emitted or not previews_by_day.get(day_no):
        return
    mapping_emitted.add(day_no)
    entries = previews_by_day[day_no]
    try:
        _emit_day_preview_mapping_inner(itinerary_id, day_no, entries)
    except Exception as exc:
        logger.warning("day preview mapping failed for %s day %s: %s", itinerary_id, day_no, exc)


def _emit_day_preview_mapping_inner(itinerary_id: int, day_no: int, entries: list[dict[str, str]]) -> None:
    """读回该日正式条目（final order），对配候选并发帧；空映射不发。"""
    with session_scope() as session:
        day = (
            session.execute(
                select(ItineraryDay).where(ItineraryDay.itinerary_id == itinerary_id, ItineraryDay.day_no == day_no)
            )
            .scalars()
            .first()
        )
        if day is None:
            return
        item_rows = [
            (int(item_id), str(name or ""))
            for item_id, name in session.execute(
                select(ItineraryItem.id, ItineraryItem.poi_name)
                .where(ItineraryItem.day_id == day.id, ItineraryItem.deleted == 0)
                .order_by(ItineraryItem.sort_no)
            ).all()
        ]
    mappings = _match_preview_mappings(item_rows, entries)
    if mappings:
        generation_events.day_preview_mapping(itinerary_id, str(entries[0].get("runId") or ""), day_no, mappings)


def _plan_whole_trip(
    user_id: int,
    itinerary_id: int,
    command: GenerateCommand,
    context: dict[str, Any],
    fingerprint: str,
    timing: _StageClock | None = None,
) -> bool:
    request = GenerateDayRequest(
        city=command.city,
        persons=command.persons,
        budget=None if command.budget is None else float(command.budget),
        start_date=None if command.start_date is None else str(command.start_date),
        day_no=1,
        days=command.days,
        used_names=[],
        hotel_tier=command.hotel_tier,
        preferences=command.preferences,
        chosen_hotel=None,
        needs_hotel=command.stay_nights > 0,
        requirements=command.requirements,
        intent=command.resolved_intent(),
        # M1b：结构化需求随整段流式请求贯通
        requirements_struct=command.requirements_struct,
        region_hint=command.region_hint,
        request_id=f"itinerary-{itinerary_id}",
        context=context,
    )
    logger.info("whole-trip stream generating itinerary %s (%s days)", itinerary_id, command.days)
    suggestions_done = False
    done_seen = False
    contract_errors = 0
    # M5a 遗留收口：转发候选时按日登记（previewId+内容名），该日正式落库后
    # 发 previewId→itemId 映射帧；每正式 persist 只发一次（day_patch 不重发）。
    previews_by_day: dict[int, list[dict[str, str]]] = {}
    mapping_emitted: set[int] = set()
    # 与 /v1/generate-stream 同：只落 trace，不把 runId 回填进事件（消费者看到的键不变）
    with use_scene("generate"), observe_run(request_id=f"itinerary-{itinerary_id}"):
        try:
            # M5a：整段腿开启逐项候选预览（item_preview/item_preview_withdrawn 帧
            # 随流转发给浏览器）；day/day_patch/done 落库语义不变。
            for event in run_generate_trip_stream(request, item_previews=True):
                event_type = str(event.get("type") or "")
                if event_type not in KNOWN_STREAM_TYPES:
                    # 未知类型 = 前向兼容的附加事件：忽略且不计数（增量演进不算协议破坏）
                    logger.debug("stream event with unknown type ignored for %s: %s", itinerary_id, event_type)
                    continue
                try:
                    _stream_events.validate_python(event)
                except ValidationError as exc:
                    contract_errors += 1
                    logger.warning(
                        "stream event rejected by contract for %s (#%s): type=%s %s",
                        itinerary_id,
                        contract_errors,
                        event_type,
                        "; ".join(str(item.get("msg")) for item in exc.errors()[:5]),
                    )
                    if contract_errors > MAX_STREAM_CONTRACT_ERRORS:
                        raise ApiError(502, "行程流事件契约校验失败") from exc
                    continue
                if event_type in ("day", "day_patch"):
                    plan_node = event.get("plan")
                    if not plan_node:
                        continue
                    day_no = int(plan_node.get("dayNo") or 0)
                    if day_no < 1 or day_no > command.days:
                        logger.warning("stream day_no out of range for %s: %s", itinerary_id, day_no)
                        continue
                    try:
                        _persist_stream_day(
                            user_id,
                            itinerary_id,
                            command,
                            day_no,
                            plan_node,
                            fingerprint,
                            overwrite=event_type == "day_patch",
                            timing=timing,
                        )
                        _emit_day_preview_mapping(
                            itinerary_id,
                            day_no,
                            event_type,
                            previews_by_day,
                            mapping_emitted,
                        )
                    except Exception as day_exc:
                        logger.warning("stream day %s persist failed for %s: %s", day_no, itinerary_id, day_exc)
                elif event_type == "suggestions":
                    rows = event.get("items") or []
                    if not rows:
                        continue
                    try:
                        itinerary_enricher.persist_suggestions(itinerary_id, [dict(row) for row in rows])
                        suggestions_done = True
                    except Exception as suggest_exc:
                        logger.warning("stream suggestions persist failed for %s: %s", itinerary_id, suggest_exc)
                elif event_type == "done":
                    done_seen = True
                    day_persistence.set_trip_theme(itinerary_id, event.get("tripTheme"))
                    logger.info(
                        "whole-trip stream done for %s: emitted %s of %s days, complete=%s",
                        itinerary_id,
                        event.get("daysEmitted"),
                        command.days,
                        event.get("complete"),
                    )
                elif event_type == "error":
                    logger.warning("whole-trip stream error for %s: %s", itinerary_id, event.get("message"))
                elif event_type == "day_item_preview":
                    # M5a 候选预览：转发业务 SSE（浏览器先亮「正在完善」）。
                    # 预览不落库——正式内容权威是随后的 day 快照，用它整体替换。
                    run_id = str(event.get("runId") or "")
                    day_no_preview = int(event.get("dayNo") or 0)
                    item_node = dict(event.get("item") or {})
                    generation_events.item_preview(
                        itinerary_id,
                        run_id,
                        day_no_preview,
                        int(event.get("itemOrdinal") or 0),
                        item_node,
                    )
                    previews_by_day.setdefault(day_no_preview, []).append(
                        {
                            "runId": run_id,
                            "previewId": f"{run_id}:{day_no_preview}:{int(event.get('itemOrdinal') or 0)}",
                            "name": str(item_node.get("poi_name") or item_node.get("poiName") or ""),
                        }
                    )
                elif event_type == "day_item_preview_withdrawn":
                    generation_events.item_preview_withdrawn(
                        itinerary_id,
                        str(event.get("runId") or ""),
                        int(event.get("dayNo") or 0),
                        int(event.get("itemOrdinal") or 0),
                        str(event.get("reason") or ""),
                    )
        except Exception as stream_exc:
            if done_seen:
                # done 之后连接收尾的残余异常（如 Premature EOF）不影响结果：缺天交给逐日循环
                logger.info("stream closed after done for itinerary %s (%s), ignoring", itinerary_id, stream_exc)
            else:
                raise
    if contract_errors:
        logger.warning(
            "whole-trip stream for %s finished with %s contract violation(s); "
            "affected days fall back to per-day generation",
            itinerary_id,
            contract_errors,
        )
    return suggestions_done


def _persist_stream_day(
    user_id: int,
    itinerary_id: int,
    command: GenerateCommand,
    day_no: int,
    plan_node: dict[str, Any],
    fingerprint: str,
    overwrite: bool,
    timing: _StageClock | None = None,
) -> None:
    action_id = f"day-{itinerary_id}-{day_no}"
    if not generation_gate.try_day_lock(itinerary_id, day_no):
        logger.warning("stream day lock busy, skip itinerary %s day %s", itinerary_id, day_no)
        return
    try:
        day = day_persistence.find_day(itinerary_id, day_no)
        if day is None:
            raise ApiError(500, "行程日不存在")
        generation_gate.verify_action(day, action_id, fingerprint)
        if day.generation_status != "SUCCEEDED" or overwrite:
            day_persistence.mark_running(day.id, action_id, fingerprint)
            generation_events.day_start(itinerary_id, day_no)
            plan = DailyPlan.model_validate(plan_node)
            day_persistence.persist(
                itinerary_id, command, day_no, plan, action_id, fingerprint, allow_overwrite=overwrite
            )
            if timing is not None:
                timing.mark("first_day")
            generation_events.day_done(itinerary_id, day_no, plan.theme, len(plan.items or []), plan.note)
            itinerary_query.evict_detail(user_id, itinerary_id)
            _submit_budget_recalculate(itinerary_id)
    finally:
        generation_gate.release_day_lock(itinerary_id, day_no)


class DeliveryCheckIncomplete(RuntimeError):
    """终检本身没跑完（校验器抛错、证据读失败）——与"检查跑完并判定违规"是两回事。

    旧口径 `logger.warning` 之后当通过：天仍是 SUCCEEDED，整趟被写成 COMPLETED，用户拿到
    一份没有任何验收背书的产品，而且从状态上看不出来。这里刻意**不**新增状态词表，
    而是让它走既有失败收敛（已交付天保留为 PARTIAL、最终预算重算、error 帧带稳定码），
    所以"没有完成检查"既不会被冒充成通过，也不会被冒充成明确违规。
    刻意不继承 RunLimitExceeded：预算耗尽允许带着已有证据诚实降级，检查没跑完不行。
    """


def _read_stream_day_checks(itinerary_id: int) -> list[tuple[ItineraryDay, dict[str, Any]]]:
    """在短事务里读取逐日检查输入，规则与修复在事务外执行。"""
    with session_scope() as session:
        days = (
            session.execute(
                select(ItineraryDay).where(
                    ItineraryDay.itinerary_id == itinerary_id, ItineraryDay.generation_status == "SUCCEEDED"
                )
            )
            .scalars()
            .all()
        )
        if not days:
            return []
        rows = (
            session.execute(
                select(ItineraryItem)
                .where(ItineraryItem.itinerary_id == itinerary_id, ItineraryItem.deleted == 0)
                .order_by(ItineraryItem.day_id, ItineraryItem.sort_no)
            )
            .scalars()
            .all()
        )
        items_by_day: dict[int, list[dict[str, Any]]] = {}
        for row in rows:
            items_by_day.setdefault(row.day_id, []).append(_item_rule_dict(row))
        checks = [
            (day, {"day_no": day.day_no, "travel_date": day.travel_date, "items": items_by_day.get(day.id, [])})
            for day in days
        ]
    return checks


def _revalidate_stream_days(itinerary_id: int, command: GenerateCommand, fingerprint: str) -> None:
    """整段流式落库天的后置终检（validate_plans 的生产主路径调用点）。

    对 stream 已落库天复检——输入是落库后的行而非流式 wire dict，校验的就是
    用户实际会拿到的数据：时间/饱和度规则按天校验、违规天单独重置；预算硬约束
    （BIZ-1）必须**整趟口径**——单天列表 days=1 的估算永远够不到整趟预算的门槛，
    这里把全部流式天聚合成一次 validate_plans 判超支，超支 issue 无单天归因、
    全部 SUCCEEDED 天一起重置，交下方既有逐日兜底循环重生成（带 used_names 与
    reflect 修复链），不新造机制。
    只在 fresh_trip（开跑前全天未完成）后调用，此刻的 SUCCEEDED 天必然出自
    本次流式；终检自身失败不推翻已交付的结果，放弃本轮复检只留警告。
    """
    try:
        checks = _read_stream_day_checks(itinerary_id)
        if not checks:
            return
        budget = float(command.budget) if settings.budget_hard_constraint and command.budget else None
        requirements = command.requirements_struct
        budget_issues = _trip_budget_issues(checks, budget, command.persons, requirements)
        # M3（spec §8.3）：整趟预算不归因到每一天——hard_cap 超支定向 reset 最贵天，
        # target 超容差只警告（不承诺严格上限）
        budget_reset_day = _hard_cap_reset_day(checks, budget_issues, requirements)
        if budget_issues and budget_reset_day is None:
            logger.warning(
                "stream trip budget over target for itinerary %s (target mode): kept as warnings, no reset",
                itinerary_id,
            )
        for day, raw in checks:
            issues, _log = validate_plans(
                [raw],
                budget=None,
                persons=command.persons,
                budget_overage_ratio=settings.budget_overage_ratio,
                requirements=requirements,
            )
            if day.day_no == budget_reset_day:
                issues = [*issues, *budget_issues]
            if not issues:
                continue
            _reset_stream_day(itinerary_id, day, fingerprint, issues)
    except (planning_tasks.TaskCancelled, planning_tasks.TaskOwnershipLost):
        # 取消/失权不是"检查未完成"：这两种情况下这趟的结论本就不归本 worker，
        # 包装成终检失败会覆盖新持有者的判断，也会把用户取消说成系统故障。
        raise
    except Exception as validate_exc:
        logger.error("stream final check did not complete for itinerary %s: %s", itinerary_id, validate_exc)
        raise DeliveryCheckIncomplete(f"行程 {itinerary_id} 的最终质量检查未完成") from validate_exc


def _reset_stream_day(itinerary_id: int, day: ItineraryDay, fingerprint: str, issues: list[str]) -> None:
    """终检违规天退回 PENDING 交逐日兜底重生成（不含预算 issue 的天不受牵连）。"""
    action_id = f"day-{itinerary_id}-{day.day_no}"
    reason = "; ".join(issues[:2])
    logger.warning(
        "stream day %s of %s failed final validation, reset for per-day regeneration: %s",
        day.day_no,
        itinerary_id,
        reason,
    )
    limits = current_limits()
    if limits is not None:
        limits.record_terminal_reset()
    day_persistence.reset_day_pending(day.id, action_id, fingerprint, reason)


def _hard_cap_reset_day(
    checks: list[tuple[ItineraryDay, dict[str, Any]]],
    budget_issues: list[str],
    requirements: TripRequirements | None,
) -> int | None:
    """M3（spec §8.3）：hard_cap 超支定向 reset 单日估算最贵天；target 无 reset。"""
    if not budget_issues:
        return None
    mode = requirements.budget_policy.mode if requirements is not None and requirements.budget_policy else None
    if mode == "hard_cap" and checks:
        return max(checks, key=lambda pair: sum(float(it.get("cost") or 0) for it in pair[1]["items"]))[0].day_no
    return None


def _trip_budget_issues(
    checks: list[tuple[ItineraryDay, dict[str, Any]]],
    budget: float | None,
    persons: int,
    requirements: TripRequirements | None = None,
) -> list[str]:
    """整趟口径判预算超支（BIZ-1）：单天列表 days=1 的估算够不到整趟预算门槛，
    全量天一次校验，只认领预算 issue（时间规则由逐天校验定位，避免重复计数）。

    M3：rooms 用户口径 + hard_cap/target 分层由 validate_plans 内部按需求处理。
    """
    if budget is None or not checks:
        return []
    trip_issues, _log = validate_plans(
        [raw for _day, raw in checks],
        budget=budget,
        persons=persons,
        budget_overage_ratio=settings.budget_overage_ratio,
        requirements=requirements,
    )
    return [issue for issue in trip_issues if BUDGET_OVERAGE_MARK in issue]


def _item_rule_dict(row: ItineraryItem) -> dict[str, Any]:
    """落库行 → validate_plans 规则键（snake_case）；时间/duration 与 wire 口径一致。"""

    def _money(value: Any) -> float | None:
        return float(value) if value is not None else None

    def _clock(value: Any) -> str | None:
        return value.strftime("%H:%M") if value is not None else None

    return {
        "item_type": row.item_type,
        "poi_name": row.poi_name,
        "fact_evidence": json.loads(row.fact_evidence_json or "{}"),
        "start_time": _clock(row.start_time),
        "end_time": _clock(row.end_time),
        "duration_min": row.duration_min,
        "open_time": row.open_time,
        "remark": row.remark,
        "cost": _money(row.cost),
        "latitude": _money(row.latitude),
        "longitude": _money(row.longitude),
    }


# ---------- 终态 ----------


def _publish_core_ready(itinerary_id: int, timing: _StageClock) -> None:
    """M5b（spec §11）：核心就绪 = DB 权威状态投影 + 业务事件，一次且同源。

    `GENERATING → CORE_READY` 的**条件更新**本身就是发布闸：rowcount=1 才发
    core_ready 事件——状态投影与事件由同一条 UPDATE 保证只发生一次，恢复重放、
    已完成行程上重复触发 plan_days（幂等登记路径）都不会二次发布。revision 与
    daysEmitted 都读自 DB（planning_revision 现值 / SUCCEEDED 天数），不从内存
    或事件流推导。有 PENDING/FAILED 天时调用方不会走到这里，终态语义不变
    （_finish 随后写 COMPLETED/PARTIAL，complete 帧含义不变）。
    """
    with session_scope() as session:
        result = session.execute(
            update(ItineraryMain)
            .where(ItineraryMain.id == itinerary_id, ItineraryMain.gen_state == "GENERATING")
            .values(gen_state="CORE_READY")
        )
        if getattr(result, "rowcount", 0) == 0:
            logger.info("core_ready skipped for itinerary %s: gen_state no longer GENERATING", itinerary_id)
            return
        main = session.get(ItineraryMain, itinerary_id)
        revision = int(main.planning_revision or 0) if main is not None else 0
        days_emitted = len(
            session.execute(
                select(ItineraryDay.id).where(
                    ItineraryDay.itinerary_id == itinerary_id, ItineraryDay.generation_status == "SUCCEEDED"
                )
            )
            .scalars()
            .all()
        )
    generation_events.core_ready(itinerary_id, revision, days_emitted)
    timing.mark("core_ready")


def _recalculate_final_budget(itinerary_id: int) -> None:
    """仅重试独立预算事务的 MySQL 死锁；不重新生成、不重放外层写事务。"""
    for attempt in range(3):
        try:
            budget_engine.recalculate(itinerary_id)
            return
        except OperationalError as exc:
            if exc.orig is None or exc.orig.args[:1] != (1213,) or attempt == 2:
                raise
            logger.warning("final budget deadlock for %s; retry %s/2", itinerary_id, attempt + 1)
            time.sleep(0.05 * (attempt + 1))


def _finish(user_id: int, itinerary_id: int, command: GenerateCommand, snapshot_summary: str = "行程生成完成") -> None:
    with session_scope() as session:
        main = session.get(ItineraryMain, itinerary_id)
    if main is None:
        itinerary_query.evict_detail(user_id, itinerary_id)
        return
    if not planning_task_runs.holds_task(itinerary_id):
        # 失权（被接管/定时器已收/进程身份已换）的 worker 不再拥有这趟行程的结论：
        # 照常写会把接管者判的 PARTIAL 覆盖成 COMPLETED，还会重复发终帧。
        logger.warning("finish skipped for itinerary %s: lease no longer held", itinerary_id)
        itinerary_query.evict_detail(user_id, itinerary_id)
        return
    limits = current_limits()
    if limits is not None and limits.remaining() <= 0:
        # 收尾窗口只做本地事务，仍需发布终态和最终预算。
        _recalculate_final_budget(itinerary_id)
        all_succeeded = day_persistence.all_days_succeeded(itinerary_id)
        day_persistence.complete_trip(itinerary_id, all_succeeded)
        generation_events.complete(
            itinerary_id,
            "COMPLETED" if all_succeeded else "PARTIAL",
            main.days,
            day_persistence.unfinished_day_nos(itinerary_id),
            None,
        )
        planning_tasks.finish_success(itinerary_id)
        itinerary_query.evict_detail(user_id, itinerary_id)
        return
    if not (command.requirements_struct and command.requirements_struct.fact_source == "chinatravel"):
        stay_hotels.spread_stay_hotels(itinerary_id, command.stay_nights)
    # 最终预算是交付的一部分，不能依赖可能已满的后台富化队列。
    _recalculate_final_budget(itinerary_id)
    all_succeeded = day_persistence.all_days_succeeded(itinerary_id)
    day_persistence.complete_trip(itinerary_id, all_succeeded)
    version_id: int | None = None
    try:
        snapshot = itinerary_version.create_snapshot(user_id, itinerary_id, "generate", snapshot_summary)
        if isinstance(snapshot.get("id"), int):
            version_id = snapshot["id"]
    except Exception as snapshot_error:
        logger.warning("version snapshot failed for %s: %s", itinerary_id, snapshot_error)
    generation_events.complete(
        itinerary_id,
        "COMPLETED" if all_succeeded else "PARTIAL",
        main.days,
        day_persistence.unfinished_day_nos(itinerary_id),
        version_id,
    )
    # 执行终态在富化释放句柄时才落（release 里带 outcome），这里只登记结论。
    planning_tasks.finish_success(itinerary_id)
    if command.requirements_struct and command.requirements_struct.fact_source == "chinatravel":
        itinerary_query.evict_detail(user_id, itinerary_id)
        return  # Frozen-source runs must never start unbudgeted online/model enrichment.
    try:
        # M5b（spec §11）：富化任务绑定提交时的规划修订——core_ready 后用户的编辑
        # 会推进 revision，旧富化不得覆盖新行程；比对在富化侧写 suggestions_json
        # 的事务内逐次做（见 itinerary_enricher._store_suggestion_rows）。
        planning_tasks.mark_enriching(itinerary_id, True)
        enricher_pool.submit(
            itinerary_enricher.enrich_itinerary,
            user_id,
            itinerary_id,
            command,
            int(main.planning_revision or 0),
            current_limits(),
        )
    except TaskRejected as rejected:
        planning_tasks.mark_enriching(itinerary_id, False)
        logger.warning("itinerary enrichment rejected for %s: %s", itinerary_id, rejected)
    itinerary_query.evict_detail(user_id, itinerary_id)


def _submit_budget_recalculate(itinerary_id: int) -> None:
    """预算重算移出落库事务，交给富化池；被拒只 warn（后续天/终态还会再算）。"""
    try:
        enricher_pool.submit(budget_engine.recalculate, itinerary_id)
    except TaskRejected as rejected:
        logger.warning("budget recalculate rejected for %s: %s", itinerary_id, rejected)


def _shell_exists(itinerary_id: int) -> bool:
    with session_scope() as session:
        return session.get(ItineraryMain, itinerary_id) is not None


# ---------- 请求入口（Java `ItineraryGenerationService.generate`） ----------

# 全球城市名允许 Unicode 字母/数字（如 São Paulo、กรุงเทพมหานคร），仍拒绝 URL/标记等符号。
MAX_CITY_LENGTH = 32
# requirements 上界（API-2）：Text 列 64KB 字节，10000 汉字（UTF-8 约 30KB）留足余量
MAX_REQUIREMENTS_LENGTH = 10000
QUEUE_FULL_NOTE = "生成失败：系统繁忙，生成队列已满"


IDEMPOTENCY_TTL_SECONDS = 600
_IDEM_NAMESPACE = "idem_generate"
# API-3：占位哨兵。reserve 即写 PENDING（而非 None）——回填窗口（占位成功 →
# 建壳提交 → 回填 id，亚秒级）内并发同键才能区分「有人在建壳」与「键不存在」，
# 不再全放行重复建壳（实测曾 10 连发建 6 个行程）。
_IDEM_PENDING = "PENDING"
_IDEM_BACKFILL_WAIT_SECONDS = 5.0
_IDEM_POLL_INTERVAL = 0.1


def _reserve_idempotent(user_id: int, key: str | None) -> int | None:
    """占幂等键；已占用时返回先前那次生成创建的 itinerary_id。

    语义（backlog「被重复请求咬过」）：同一个 user 的同一个幂等键在 TTL 内
    只会建壳一次——网络层重试 / 双击拿回的是**同一个行程**（可能仍在生成、
    已完成或已失败），而不是第二份行程第二份 LLM 账单。想重新生成就不带键
    或换键（那是用户的显式新意图）。

    API-3：键被占且值为 PENDING（回填窗口）时，轮询等待首个请求回填 id 后
    重放；仅当等待超时（首个请求大概率已崩溃，窗口极小）才释放键降级为新
    一次生成（宁重复、不 500，与旧口径一致）。释放只做一次，防两个等待者
    互踢对方的占位形成活锁。
    """
    normalized = (key or "").strip()
    if not normalized:
        return None
    cache_key = f"{user_id}:{normalized[:128]}"
    deadline = time.monotonic() + _IDEM_BACKFILL_WAIT_SECONDS
    released_once = False
    while True:
        if cache_store.reserve(_IDEM_NAMESPACE, cache_key, _IDEM_PENDING, IDEMPOTENCY_TTL_SECONDS):
            return None  # 首次占位（值在建壳成功后回填）
        existing = cache_store.get_json(_IDEM_NAMESPACE, cache_key)
        if isinstance(existing, int):
            return existing
        if time.monotonic() < deadline:
            time.sleep(_IDEM_POLL_INTERVAL)
            continue
        if released_once:
            # 释放后仍被对手占着且无回填：放弃键判定按新请求走（并发最坏退化为
            # 多建一壳，仍优于旧行为的全放行）
            return None
        released_once = True
        cache_store.delete(_IDEM_NAMESPACE, cache_key)


def _replay_if_known(user_id: int, replay_id: int | None, idempotency_key: str | None) -> dict[str, Any] | None:
    """幂等重放：占位值指向的行程存在且属于本人时，回放它的详情。

    占位值在首次建壳成功后回填；理论上此处读不到值只在「占位后进程崩溃
    未回填」的窗口出现——此时释放键并降级为新一次生成（宁重复、不 500）。
    """
    if replay_id is None:
        return None
    with session_scope() as session:
        exists = session.get(ItineraryMain, replay_id)
    if exists is not None and exists.user_id == user_id:
        return itinerary_query.detail(user_id, replay_id)
    cache_store.delete(_IDEM_NAMESPACE, f"{user_id}:{(idempotency_key or '').strip()[:128]}")
    return None


def generate(user_id: int, body: GenerateTripRequest, idempotency_key: str | None = None) -> dict[str, Any]:
    from app.services import generation_admission

    existing = (
        cache_store.get_json(_IDEM_NAMESPACE, f"{user_id}:{idempotency_key.strip()[:128]}") if idempotency_key else None
    )
    with generation_admission.new_trip(user_id, existing if isinstance(existing, int) else None):
        return _create_generation(user_id, body, idempotency_key)


def _create_generation(user_id: int, body: GenerateTripRequest, idempotency_key: str | None) -> dict[str, Any]:
    """建壳 → 逐日异步生成 → 立即返回可轮询的初始详情（status=1）。

    `idempotency_key`（来自 X-Idempotency-Key 头，可选）：TTL 内同键重试
    返回既有行程，不重复建壳（见 _reserve_idempotent）。
    """
    command = _validate(body)
    problem = destination_problem(command.city)
    if problem:
        raise ApiError(400, problem)
    replay = _replay_if_known(user_id, _reserve_idempotent(user_id, idempotency_key), idempotency_key)
    if replay is not None:
        return replay
    from app.services import planning_queue

    try:
        with planning_queue.admission(), session_scope() as session:
            main = ItineraryMain(
                user_id=user_id,
                title=f"{command.city}{command.days}日游",
                city=command.city,
                start_date=command.start_date,
                end_date=command.end_date,
                days=command.days,
                persons=command.persons,
                budget=command.budget,
                preferences=",".join(command.preferences) if command.preferences else None,
                hotel_tier=command.hotel_tier,
                stay_nights=command.stay_nights,
                # V10：用户原话参数随壳落库——僵尸续跑 rebuild_request 读回（恢复保真 P0-3）
                intent=command.intent,
                requirements=command.requirements,
                # V13（M1a）：结构化需求快照随壳落库；新行程一律写（全默认也写 '{}'），
                # NULL 与旧行程的判定信号——rebuild 据此选择 v1/v2 指纹口径
                requirements_json=canonical_requirements_payload(command.requirements_struct),
                # 出发地建壳即写：submit 与 plan 开跑之间崩掉也留得住用户输入的事实
                origin_city=command.origin_city,
                status=1,
                # 显式状态机：建壳即入 GENERATING 并记开始时间，恢复任务据此识别生成中/僵尸
                gen_state="GENERATING",
                gen_started_at=datetime.now(),
            )
            session.add(main)
            session.flush()
            itinerary_id = main.id
            for day_no in range(1, command.days + 1):
                session.add(
                    ItineraryDay(
                        itinerary_id=itinerary_id,
                        day_no=day_no,
                        city=command.city,
                        travel_date=None
                        if command.start_date is None
                        else command.start_date + timedelta(days=day_no - 1),
                        generation_status="PENDING",
                    )
                )
            planning_queue.enqueue(user_id, itinerary_id, command)
    except (TaskRejected, ApiError) as exc:
        if idempotency_key and idempotency_key.strip():
            cache_store.delete(_IDEM_NAMESPACE, f"{user_id}:{idempotency_key.strip()[:128]}")
        if isinstance(exc, TaskRejected):
            raise ApiError(429, "行程生成任务已满，系统繁忙，请稍后再试") from exc
        raise
    # 规格 §3.1.3（个人 Agent V1）：普通建壳不再把本次偏好自动写入长期记忆——
    # 长期记忆只经 /preferences/memory 显式保存；本次偏好仍随壳存 itinerary_main.preferences。
    # 行程与投递意图已原子提交；此后 API 退出也不会丢失任务。
    itinerary_version.record_snapshot_or_log(user_id, itinerary_id, "create", "创建行程草稿")
    # 建壳成功后回填幂等占位值：此后同键重试拿回这个 itinerary_id
    if idempotency_key and idempotency_key.strip():
        cache_store.set_json(
            _IDEM_NAMESPACE, f"{user_id}:{idempotency_key.strip()[:128]}", itinerary_id, IDEMPOTENCY_TTL_SECONDS
        )

    return itinerary_query.detail(user_id, itinerary_id)


def _validate(body: GenerateTripRequest) -> GenerateCommand:
    """校验阶梯与 Java 一致：先 DTO（Bean Validation），再服务层规则。"""
    if not body.city or not body.city.strip():
        raise ApiError(400, "目的地不能为空")
    if body.days is None:
        raise ApiError(400, "出行天数不能为空")
    if body.days < 1:
        raise ApiError(400, "天数至少为 1 天")
    if body.days > MAX_TRIP_DAYS:
        raise ApiError(400, "天数最多为 7 天")
    if body.persons is not None and body.persons < 1:
        raise ApiError(400, "人数至少为 1 人")
    if body.persons is not None and body.persons > 20:
        raise ApiError(400, "人数最多为 20 人")
    if body.stayNights is not None and body.stayNights < 0:
        raise ApiError(400, "住宿晚数不能为负数")
    if body.stayNights is not None and body.stayNights > MAX_TRIP_DAYS:
        raise ApiError(400, "住宿晚数最多为 7 晚")
    if body.budget is not None and body.budget < 0:
        raise ApiError(400, "预算不能为负数")
    # API-2：上界对齐 cost 先例（itinerary_command 的 0 到 1000000）；不拦的话
    # 10^10+ 的值会一路进 DECIMAL(12,2) 落库时溢出成 500
    if body.budget is not None and body.budget > Decimal("1000000"):
        raise ApiError(400, "预算必须在 0 到 1000000 之间")
    # API-2：requirements 落 Text 列（64KB 字节），不拦的超长文本在 MySQL 侧溢出成 500
    if body.requirements is not None and len(body.requirements) > MAX_REQUIREMENTS_LENGTH:
        raise ApiError(400, f"补充要求最多 {MAX_REQUIREMENTS_LENGTH} 字")
    if body.intent is not None and len(body.intent) > 800:
        raise ApiError(400, "旅行意图最多 800 字")

    city = body.city.strip()
    if (
        len(city) > MAX_CITY_LENGTH
        or not city[0].isalnum()
        or not all(
            char.isalnum() or unicodedata.category(char).startswith("M") or char.isspace() or char in "·'’-()（）."
            for char in city
        )
    ):
        raise ApiError(400, "目的地名称格式不正确，请输入城市或景点所在城市")
    persons = body.persons if body.persons is not None else 1
    if persons < 1 or persons > 20:
        raise ApiError(400, "出行人数必须在 1 到 20 人之间")
    if body.startDate and body.endDate:
        if body.endDate < body.startDate:
            raise ApiError(400, "结束日期不能早于开始日期")
        if (body.endDate - body.startDate).days + 1 != body.days:
            raise ApiError(400, "日期范围与行程天数不一致")
    stay_nights = body.stayNights if body.stayNights is not None else max(body.days - 1, 0)
    if stay_nights < 0 or stay_nights > body.days:
        raise ApiError(400, "住宿晚数必须在 0 到行程天数之间")
    # M1a：结构化需求确定性校验——生成入口必须拿到完整、合法参数（收集阶段的
    # 超限/冲突在 clarify 协商，不走这里）；问题逐条中文提示，与 Java 文案口径并存
    if body.requirementsStruct is not None:
        issues = requirements_issues(body.requirementsStruct, days=body.days)
        if issues:
            raise ApiError(400, "；".join(issues))

    return GenerateCommand(
        city=city,
        days=body.days,
        persons=persons,
        stay_nights=stay_nights,
        start_date=body.startDate,
        end_date=body.endDate,
        budget=body.budget,
        preferences=body.preferences or [],
        hotel_tier=body.hotelTier,
        region_hint=body.regionHint,
        requirements=body.requirements,
        intent=body.intent,
        # L14 契约字段（originCity）此前在这里被丢弃：主路径永远查不到报价，
        # save_origin_city 也永远写不进主表，rebuild 更无从读回（恢复保真 P0-3）。
        origin_city=(body.originCity or "").strip() or None,
        requirements_struct=body.requirementsStruct,
    )
