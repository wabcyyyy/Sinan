"""出发前照看的后台建议：模型流程（规格 §4.4 步骤 5–6）与发布短事务（步骤 8–9）。

当日 AI 尝试占用后，`run_suggestion` 在实际工作线程内依次绑定 use_route、
payer_scope、带 owner/itinerary 的 RunLimits、chat scene 与 observe_run；
不能依赖请求线程的 contextvars。guard 每次外呼前复核令牌与资格。
后台限额为 90 秒、最多 4 次模型调用，token/检索上限沿用现有默认。

发布短事务无 LLM/HTTP 外呼：与前台共用主行程行锁，复核令牌、资格、版本及待确认
草稿后，写入 `[行程照看]` 消息、确认线程、去重键与结果并清令牌；失败不留键。

不调用 finalize_chat_turn（它会伪造 user 消息并顶掉前台草稿），不建 planning_task
行、不改 gen_state。依赖：itinerary_care_check（收尾落库，本模块失败路径复用其
token 条件写；反向的 execute_check 用函数内懒导入取本模块，破环）。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select

from app.agent import (
    CareAdjustInput,
    CareAdjustResult,
    RunLimits,
    TaskStopped,
    care_confirm_thread,
    observe_run,
    run_confirmation,
    run_weather_care_adjustment,
    use_scene,
    use_task_limits,
)
from app.common.clock import shanghai_today
from app.common.envelope import ApiError
from app.common.llm_route import use_route
from app.common.trial_billing import payer_scope
from app.common.vo_json import iso_datetime
from app.db.models import ItineraryCare, ItineraryDay, ItineraryMain, SysUser
from app.db.session import session_scope
from app.schemas.trip_requirements import TripRequirements
from app.services import itinerary_chat, llm_gateway_service, quota_service
from app.services.itinerary_care import (
    STATUS_FAILED,
    STATUS_QUOTA,
    STATUS_SUGGESTED,
    STATUS_SUPERSEDED,
    STATUS_WAITING_USER,
    SuggestionOrder,
    eligibility_problem,
)
from app.services.itinerary_care_check import finalize_check, finalize_check_in_session
from app.services.itinerary_care_risk import CheckOutcome, result_payload

logger = logging.getLogger(__name__)

# ---- 后台草稿的一次 run 限额（规格 §4.4 步骤 6：不高于现有默认，不加 env）----
CARE_DEADLINE_SECONDS = 90.0
CARE_MAX_LLM_CALLS = 4

#: 发布消息的内容前缀（规格 §4.4 步骤 8：一条 [行程照看] AI 消息）
CARE_MESSAGE_PREFIX = "[行程照看] "

#: 失败收尾的用户可理解文案（不写成功、不给空「已调整」消息）
GENERATION_FAILED_REASON = "未能生成调整建议，明天会自动再试"
ROUTE_BROKEN_REASON = "模型通道暂不可用，本次未生成调整建议"
STALE_REASON = "行程已更新，本次建议作废"
WATCHED_MOVED_REASON = "关注范围已更新，本次建议作废"


@dataclass(frozen=True)
class _GenerationInputs:
    """模型阶段的正式行程事实（步骤 2 的读取在生成前一刻复核令牌/资格）。

    watched_item_ids 是生成实际用的监控范围快照：发布锁内与本表当前值比对，
    生成期间用户重选范围的建议不再按旧范围发布（§4.4 步骤 8 的复核项）。
    """

    owner_id: int
    base_revision: str
    planning_revision: int
    watched_item_ids: tuple[int, ...]
    care_input: CareAdjustInput


def run_suggestion(itinerary_id: int, token: str, order: SuggestionOrder) -> None:
    """后台草稿生成与发布（规格 §4.4 步骤 5–9）；当日 AI 尝试已被判定管线占用。"""
    inputs = _generation_inputs(itinerary_id, token, order)
    if inputs is None:
        # 门槛通过后锁/资格已失效（暂停/禁用/出发）：无声退出——暂停路径已写自己的
        # 状态，这里任何 token 条件写都落空，不重复打扰
        return
    quota_outcome = _admit_quota(inputs.owner_id)
    if quota_outcome is not None:
        finalize_check(itinerary_id, token, quota_outcome)
        return
    try:
        # 路由解析先于 with 单独兜 ApiError（同 plan_days）：BYOK 配置损坏也要走
        # failed 收尾；解析结果为 None = 平台 key 正常路径，不是失败
        route = llm_gateway_service.resolve_route(inputs.owner_id)
    except ApiError as exc:
        logger.error("care model route broken for itinerary %s: %s", itinerary_id, exc)
        finalize_check(itinerary_id, token, _failed_outcome(ROUTE_BROKEN_REASON))
        return
    result = _generate_in_context(itinerary_id, inputs, route, token)
    if isinstance(result, CheckOutcome):
        finalize_check(itinerary_id, token, result)  # 生成异常的简短失败态（步骤 7）
        return
    if result is None:
        return  # guard 探针失权：令牌已不在，结果不得发布，也不覆盖暂停写入的状态
    if not result.ok or not result.plans or result.response is None:
        # LLM/工具/校验失败：简短状态，不写成功、不自动降低要求（步骤 7）
        finalize_check(itinerary_id, token, _failed_outcome(result.reason))
        return
    publish_draft(itinerary_id, token, inputs, result, order)


def _admit_quota(owner_id: int) -> CheckOutcome | None:
    """模型阶段的产品额度准入（§4.4 步骤 4「是否有额度」）：滑动窗 + 持久硬顶。"""
    try:
        quota_service.enforce_llm_budget(owner_id)
    except ApiError:
        return CheckOutcome(status=STATUS_QUOTA, result=result_payload(STATUS_QUOTA, "未生成调整建议：额度不足"))
    return None


def _generate_in_context(
    itinerary_id: int, inputs: _GenerationInputs, route, token: str
) -> CareAdjustResult | CheckOutcome | None:
    """进入后台费用/身份上下文并生成（§4.4 步骤 5）。

    - TaskStopped（guard 探针失权：暂停/禁用/删除/令牌易主）→ None，后续外呼停止、
      结果不发布；已发生的外呼费用照常结算（供应商侧不可撤回）；
    - 其余异常 → 简短 failed 结论；这里不做任何持久化。
    """
    limits = _care_limits(itinerary_id, inputs.owner_id, token)
    try:
        # 顺序即语义：先路由/付费身份，再 use_task_limits 压入带 owner 的 RunLimits，
        # 最后 use_scene/observe_run——begin_limits 继承已有 limits，颠倒就成了无主账。
        with (
            use_route(route),
            use_task_limits(limits),
            payer_scope(inputs.owner_id),
            use_scene("chat"),
            observe_run(request_id=f"care-{itinerary_id}"),
        ):
            return run_weather_care_adjustment(inputs.care_input)
    except TaskStopped:
        logger.warning("care run lost authority for itinerary %s; result discarded", itinerary_id)
        return None
    except Exception as exc:
        logger.warning("care generation failed for itinerary %s: %s", itinerary_id, exc, exc_info=True)
        return _failed_outcome(GENERATION_FAILED_REASON)


def _failed_outcome(reason: str | None) -> CheckOutcome:
    return CheckOutcome(
        status=STATUS_FAILED,
        result=result_payload(STATUS_FAILED, reason or GENERATION_FAILED_REASON),
    )


def _care_limits(itinerary_id: int, owner_id: int, token: str) -> RunLimits:
    """本批后台任务的 run 限额：90 秒截止、最多 4 次模型调用，token/检索沿用默认。"""
    limits = RunLimits(
        user_id=owner_id,
        itinerary_id=itinerary_id,
        deadline_seconds=CARE_DEADLINE_SECONDS,
        max_llm_calls=CARE_MAX_LLM_CALLS,
    )
    # guard 在每次外呼/计数前触发（RunLimits.check）：令牌易主、暂停、禁用、出发、
    # 归档任一发生即失权——「后续外呼停止」不是 prompt 约定，是探针硬断
    limits.guard = lambda: _assert_still_authorized(itinerary_id, token)
    return limits


def _assert_still_authorized(itinerary_id: int, token: str) -> None:
    with session_scope() as session:
        row = session.get(ItineraryCare, itinerary_id)
        if row is None or not row.enabled or row.check_token != token:
            raise TaskStopped(f"care 任务 {itinerary_id} 已失去执行权")
        main = session.get(ItineraryMain, itinerary_id)
        today = shanghai_today()
        if main is None or main.archived or (main.start_date is not None and main.start_date <= today):
            raise TaskStopped(f"care 任务 {itinerary_id} 的行程已不可照看")
        owner = session.get(SysUser, main.user_id)
        if owner is None or not owner.status:
            raise TaskStopped(f"care 任务 {itinerary_id} 的账号已停用")


def _generation_inputs(itinerary_id: int, token: str, order: SuggestionOrder) -> _GenerationInputs | None:
    """生成前一刻读取正式行程（步骤 2），并复核令牌与资格；失效返回 None。"""
    with session_scope() as session:
        row = session.get(ItineraryCare, itinerary_id)
        if row is None or not row.enabled or row.check_token != token:
            return None
        main = session.get(ItineraryMain, itinerary_id)
        today = shanghai_today()
        if main is None or main.archived or (main.start_date is not None and main.start_date <= today):
            return None
        owner = session.get(SysUser, main.user_id)
        if owner is None or not owner.status:
            return None
        if eligibility_problem(main, session):
            return None
        requirements = TripRequirements.model_validate(main.requirements_json) if main.requirements_json else None
        owner_id = int(main.user_id)
        planning_revision = int(main.planning_revision)
        watched = tuple(int(item) for item in (row.watched_item_ids_json or []))
        city = str(main.city)
        persons = 1 if main.persons is None else int(main.persons)
        budget = None if main.budget is None else float(main.budget)
        start_date = None if main.start_date is None else str(main.start_date)
        hotel_tier = main.hotel_tier
        # current_plans 的历史指纹形状不变；照看是整份替换，另带齐每日展示元数据。
        raw_plans = itinerary_chat.current_plans(itinerary_id)
        base_revision = itinerary_chat.plan_revision(raw_plans)
        metadata_by_day = {
            day.day_no: json.loads(day.metadata_json)
            for day in session.scalars(select(ItineraryDay).where(ItineraryDay.itinerary_id == itinerary_id))
            if day.metadata_json
        }
    # 指纹先算 Decimal 原始形状；发给模型时再转 float，不改变现有确认版本口径。
    care_plans = [{**metadata_by_day.get(plan["day_no"], {}), **plan} for plan in raw_plans]
    return _GenerationInputs(
        owner_id=owner_id,
        base_revision=base_revision,
        planning_revision=planning_revision,
        watched_item_ids=watched,
        care_input=CareAdjustInput(
            city=city,
            plans=itinerary_chat._json_numbers(care_plans),
            requirements=requirements,
            affected_days=order.affected,
            persons=persons,
            budget=budget,
            start_date=start_date,
            hotel_tier=hotel_tier,
        ),
    )


# ------------------------------------------------------- 发布短事务（步骤 8–9）----


def publish_draft(
    itinerary_id: int,
    token: str,
    inputs: _GenerationInputs,
    result: CareAdjustResult,
    order: SuggestionOrder,
) -> None:
    """发布短事务：行锁内复核 → 绑 care 确认线程 → 落一条 [行程照看] AI 消息。

    事务内不做任何 LLM/HTTP 外呼（步骤 8）；确认图的 checkpoint 写入是进程级
    检查点库，不是业务事务的一部分——事务若回滚，checkpoint 成为不可达孤儿，
    下次发布同线程覆盖，apply 只认服务端消息里的线程，无副作用。
    """
    now = datetime.now()
    thread = care_confirm_thread(itinerary_id)
    with session_scope() as session:
        # 与前台 finalize_chat_turn 同一把主行程行锁（§4.5）：前台优先
        itinerary_chat.lock_main_for_draft(session, itinerary_id)
        row = (
            session.execute(select(ItineraryCare).where(ItineraryCare.itinerary_id == itinerary_id).with_for_update())
        ).scalar_one_or_none()
        owner_id = _publish_gate(session, row, itinerary_id, token)
        if owner_id is None:
            return  # 令牌已易主/任务已暂停/资格失效：结果不得发布，也不覆盖暂停状态
        outcome = _outcome_under_lock(session, row, owner_id, itinerary_id, inputs, result, order, now, thread)
        finalize_check_in_session(session, itinerary_id, token, outcome)


def _publish_gate(session, row: ItineraryCare | None, itinerary_id: int, token: str) -> int | None:
    """锁内复核（步骤 8）：令牌、enabled、删除/归档/出发、账号、资格；通过返回 owner_id。"""
    if row is None or not row.enabled or row.check_token != token:
        return None
    main = session.get(ItineraryMain, itinerary_id)
    today = shanghai_today()
    if main is None or main.archived or (main.start_date is not None and main.start_date <= today):
        return None
    owner = session.get(SysUser, main.user_id)
    if owner is None or not owner.status:
        return None
    if eligibility_problem(main, session):
        return None
    return int(main.user_id)


def _outcome_under_lock(
    session,
    row: ItineraryCare | None,
    owner_id: int,
    itinerary_id: int,
    inputs: _GenerationInputs,
    result: CareAdjustResult,
    order: SuggestionOrder,
    now: datetime,
    thread: str,
) -> CheckOutcome:
    """锁内的四岔结论：前台草稿出现 → 丢弃；版本/关注范围推进 → 作废；否则发布。"""
    if itinerary_chat.latest_action_message(session, owner_id, itinerary_id) is not None:
        # 前台优先：生成期间出现了用户待确认草稿 → 丢弃本次结果，不顶掉草稿
        return CheckOutcome(
            status=STATUS_WAITING_USER,
            result={
                "checkedAt": iso_datetime(now),
                "status": STATUS_WAITING_USER,
                "reason": "先处理当前草稿，本次自动建议未保存",
                "days": order.observations,
                "affectedItemIds": _affected_ids(order),
                "pendingMessageId": None,
            },
        )
    if _version_moved(session, itinerary_id, inputs):
        # 版本复核（步骤 8）：生成期间正式行程/修订被推进 → 结果作废，不发布
        return CheckOutcome(
            status=STATUS_FAILED,
            result={
                "checkedAt": iso_datetime(now),
                "status": STATUS_FAILED,
                "reason": STALE_REASON,
                "days": order.observations,
                "affectedItemIds": _affected_ids(order),
                "pendingMessageId": None,
            },
        )
    if row is not None and _watched_moved(row, inputs):
        # 关注范围复核（步骤 8 的同族项）：生成期间用户重选监控范围 → 旧范围的建议
        # 作废，不发布；去重键未落库，下一窗口按新范围重判（§3.3 重算不受每日限影响）
        return CheckOutcome(
            status=STATUS_SUPERSEDED,
            result={
                "checkedAt": iso_datetime(now),
                "status": STATUS_SUPERSEDED,
                "reason": WATCHED_MOVED_REASON,
                "days": order.observations,
                "affectedItemIds": _affected_ids(order),
                "pendingMessageId": None,
            },
        )
    return _published_outcome(session, owner_id, itinerary_id, inputs, result, order, now, thread)


def _watched_moved(row: ItineraryCare, inputs: _GenerationInputs) -> bool:
    """监控范围快照 vs 当前值的集合比较（PUT 保存顺序可能不同，范围语义只看集合）。"""
    current = {int(item) for item in (row.watched_item_ids_json or [])}
    return current != set(inputs.watched_item_ids)


def _version_moved(session, itinerary_id: int, inputs: _GenerationInputs) -> bool:
    main = session.get(ItineraryMain, itinerary_id)
    moved = itinerary_chat.plan_revision(itinerary_chat.current_plans(itinerary_id)) != inputs.base_revision
    return moved or main is None or int(main.planning_revision) != inputs.planning_revision


def _published_outcome(
    session,
    owner_id: int,
    itinerary_id: int,
    inputs: _GenerationInputs,
    result: CareAdjustResult,
    order: SuggestionOrder,
    now: datetime,
    thread: str,
) -> CheckOutcome:
    """发布分支：确认图绑提案 → 存一条带基准版本与线程元数据的 AI 消息。"""
    proposal = result.response
    assert proposal is not None, "run_suggestion 已确认成功结果携带原始提案"
    run_confirmation(proposal, thread=thread)  # 提案落 care 专属线程 checkpoint
    attached = itinerary_chat.attach_base_revision(
        result.plans,
        inputs.base_revision,
        camel_case=False,
        planning_revision=inputs.planning_revision,
        confirmation_thread=thread,
    )
    message = itinerary_chat.save_chat_message(
        session, owner_id, itinerary_id, "ai", f"{CARE_MESSAGE_PREFIX}{result.reply}", attached, [], True
    )
    return CheckOutcome(
        status=STATUS_SUGGESTED,
        result={
            "checkedAt": iso_datetime(now),
            "status": STATUS_SUGGESTED,
            "reason": "已生成天气调整建议，请查看草稿",
            "days": order.observations,
            "affectedItemIds": _affected_ids(order),
            "pendingMessageId": int(message.id),
        },
        risk_keys=order.fresh_keys,  # 去重键只随发布成功落库（步骤 8）
    )


def _affected_ids(order: SuggestionOrder) -> list[int]:
    return sorted(item.id for day in order.affected for item in day.items)
