"""对话改行程的编排层（orchestrator）与唯一对外入口 run_chat_turn。

职责：
- _decide_plan_change：把当前计划+用户要求发给 LLM，要求返回封闭动作集之一的 JSON
  （hotel_proposal / plan_update / rewrite_plan / clarify / no_change）；
- run_chat_turn：按 mode 分流——酒店走候选确认，行程走 plan_edit 落地，
  并对结果跑时间冲突/实质变更/酒店签名等安全校验，最后返回可预览的草稿。

实现要点：
- 封闭动作集（finite action space）：用户千奇百怪的说法都被归约到有限几种能力，
  参数内容（整份 JSON）由 LLM 填充，代码只负责执行与校验；
- 大改（改天数/重排）优先让模型输出完整 plan_document（rewrite_plan），
  小改才用补丁，避免脆弱的“大量 move/delete”表达；
- 模型提案失败时有一次修复重试，仍失败且属精简/延长类再退回确定性兜底；
- 本层只产出草稿，任何落库都由前端“确认应用”触发，绝不直接写数据库。

依赖：intent / validate / document / plan_edit / hotel 全部。
"""

import contextlib
import json
import logging
from datetime import date, timedelta

from app.agent.runtime.memory import dialogue_messages
from app.agent.tools import impl as tools
from app.common.llm_client import get_llm_client
from app.common.model_registry import model_for
from app.schemas.requirement_patches import RequirementPatch, apply_intake_patches
from app.schemas.trip import (
    MAX_TRIP_DAYS,
    ChatTurnRequest,
    ChatTurnResponse,
)
from app.schemas.trip_requirements import IntakeState, TripRequirements, canonical_requirements_payload

from .confirm_graph import run_confirmation
from .document import _decision_plan_document, _trip_plan_document
from .hotel import (
    _hotel_catalog,
    _hotel_intent_from_decision,
    _hotel_proposal_response,
    _hotel_signature,
)
from .hotel_intent import (
    _fallback_hotel_intent,
    _has_explicit_hotel_comparison,
    _hotel_comparison_base_tier,
    _is_hotel_request,
    _understand_hotel_intent,
    _with_stay_scope,
)
from .intent import (
    _increase_target_days,
    _is_reduction_request,
    _is_time_adjustment_request,
    _is_vague_poi_browse_request,
    _reduce_target_days,
    _requested_day_count,
)
from .plan_edit import (
    _apply_plan_update,
    _dedupe_plans,
    _deterministic_extend,
    _deterministic_reduce,
)
from .prompts import DECIDE_SYSTEM_PROMPT
from .validate import (
    DecisionJsonError,
    _all_plan_conflicts,
    _decision_reply,
    _default_plan_update_reply,
    _parse_decision_json,
    _scope_violations,
    _substantive_plan_signature,
    _untouched_conflicted_days,
)

logger = logging.getLogger(__name__)


def _decide_plan_change(req: ChatTurnRequest, hotels: list[dict], feedback: str | None = None) -> dict:
    document = _decision_plan_document(req)
    system = DECIDE_SYSTEM_PROMPT
    messages = [{"role": "system", "content": system}]
    messages.extend(dialogue_messages(req.history))
    user_content = (
        f"当前计划JSON：{json.dumps(document, ensure_ascii=False)}\n"
        f"hotel_catalog：{json.dumps(_hotel_catalog(hotels), ensure_ascii=False)}\n"
        f"用户本轮要求：{req.message}"
    )
    # 残留①预检（2026-10-06）：基线本身带时间重叠时（生成链终检放行的存量行程），
    # 明告知模型「这是遗留问题」——与用户要求相关就顺手修复，无关则保持原样，
    # 不必为绕开它而拒绝整天。放行兜底见 _chat_turn_response 的继承冲突分支。
    baseline_conflicts = _all_plan_conflicts(req.plans)
    if baseline_conflicts:
        day_no, first, second = baseline_conflicts[0]
        user_content += (
            f"\n\n[已知遗留问题] 当前计划第 {day_no} 天「{first}」与「{second}」时间本就重叠。"
            "若用户要求与此相关请一并修复；无关则保持原样即可，系统不会因此拒绝本次编辑。"
        )
    if feedback:
        user_content += f"\n\n[上一轮校验反馈，必须修正] {feedback}"
    messages.append({"role": "user", "content": user_content})
    client = get_llm_client()
    # 输出预算必须容得下 plan_document 整份回显（六维复测 2026-10-06：5 天 29 项的
    # 行程 planDocument ≈ 7000 token，旧上限 1800 必截断 → 畸形 JSON → 修复重试同样
    # 截断 → 5/5 轮 no-op「本次没有需要修改的内容」）。8000 与 day_stream 主调
    # 同一既有上限；7 天满配行程仍可能超限，届时按既有路径降级 no-op，不劣于修复前。
    raw = client.chat(
        messages,
        temperature=0.1,
        max_tokens=8000,
        model=model_for("fast"),
        json_mode=True,
    )
    try:
        return _parse_decision_json(raw)
    except DecisionJsonError:
        logger.warning("plan decision returned malformed JSON; requesting one repair")
        repaired = client.chat(
            [
                {"role": "system", "content": "修复下面的JSON。保持原意，只输出一个语法正确的JSON对象，不要解释。"},
                {"role": "user", "content": raw[:12000]},
            ],
            temperature=0,
            max_tokens=8000,
            model=model_for("fast"),
            json_mode=True,
        )
        return _parse_decision_json(repaired)


def _has_existing_hotel_item(req: ChatTurnRequest) -> bool:
    """当前计划里是否已有住宿条目（时间调整守卫的另一半：没有酒店就谈不上“挪酒店”）。"""
    return any(item.get("item_type") == "hotel" for plan in (req.plans or []) for item in (plan.get("items") or []))


#: 需求提案只允许 TripRequirements 侧目标；基础参数（天数/预算等）不随提案改
_PROPOSAL_TARGETS = frozenset(
    {
        "required_place",
        "excluded_place",
        "excluded_category",
        "pace",
        "transport_preference",
        "max_walk_minutes_per_leg",
        "budget_policy_mode",
        "budget_policy_include_intercity",
        "lodging_rooms",
        "lodging_locked_hotel",
        "lodging_stay_nights_explicit",
    }
)


def _proposed_requirements_from_decision(decision: dict, req: ChatTurnRequest) -> TripRequirements | None:
    """决策里的 requirements_patches → 拟变更需求（确定性应用，非法条目跳过）。

    基线 = 当前正式需求（req.requirements_struct，缺省空结构）；含糊的诉求
    不得顺便解除既有硬要求——只有明确指令的 patch 才生效。
    """
    raw = decision.get("requirements_patches")
    if not isinstance(raw, list) or not raw:
        return None
    baseline = req.requirements_struct or TripRequirements()
    state = IntakeState(requirements=baseline.model_copy(deep=True))
    applied = False
    for item in raw:
        if not isinstance(item, dict):
            continue
        try:
            patch = RequirementPatch.model_validate(item)
        except Exception:
            continue
        if patch.target not in _PROPOSAL_TARGETS:
            continue
        apply_intake_patches(state, [patch])
        applied = True
    if not applied:
        return None
    if (
        canonical_requirements_payload(state.requirements)
        == canonical_requirements_payload(baseline if baseline != TripRequirements() else None)
        and canonical_requirements_payload(state.requirements) == {}
    ):
        return None
    return state.requirements


def _chat_turn_response(req: ChatTurnRequest) -> ChatTurnResponse:
    hotels = tools.search_hotels(req.city, limit=30)
    if _is_vague_poi_browse_request(req.message) and not _is_hotel_request(req, hotels):
        return ChatTurnResponse(
            reply=(
                "### 可以为你推荐其他景点\n\n"
                "请告诉我想查看哪一天、偏好的类型（自然 / 人文 / 亲子等），"
                "或直接说出想替换的景点；当前行程没有修改。"
            ),
            plans=[],
            changed=False,
            hotel_options=[],
            requires_confirmation=False,
            plan_document=_trip_plan_document(req),
            operations=[],
        )
    requested_days = _requested_day_count(req.message, req.days)
    if requested_days is not None and requested_days > MAX_TRIP_DAYS:
        return ChatTurnResponse(
            reply=f"### 行程天数上限\n\n每次生成行程最多支持 {MAX_TRIP_DAYS} 天，本次没有修改行程。",
            plans=[],
            changed=False,
            hotel_options=[],
            requires_confirmation=False,
            plan_document=_trip_plan_document(req),
            operations=[],
        )
    try:
        decision = _decide_plan_change(req, hotels)
    except DecisionJsonError as exc:
        logger.warning("unified plan decision remained invalid after repair: %s", exc)
        return ChatTurnResponse(
            reply="### 暂时没能生成可靠草稿\n\n模型返回的计划格式不完整，本次没有修改行程。请直接重试一次。",
            plans=[],
            changed=False,
            plan_document=_trip_plan_document(req),
        )
    except Exception as exc:
        logger.warning("unified plan decision failed: %s", exc)
        # 仅在模型不可用时启用旧规则兜底；正常语义路由不依赖关键词。
        if _is_hotel_request(req, hotels):
            intent = _understand_hotel_intent(req, hotels)
            return _hotel_proposal_response(req, hotels, intent)
        return ChatTurnResponse(
            reply=("### 行程助手暂时不可用\n\n本次没有修改行程，请稍后重试；如果要求较复杂，也可以拆成一步发送。"),
            plans=[],
            changed=False,
            plan_document=_trip_plan_document(req),
        )

    mode = str(decision.get("mode") or "no_change")
    raw_operations = decision.get("operations")
    operations = raw_operations if isinstance(raw_operations, list) else []
    # 明确酒店请求必须优先进入候选流程。模型有时会把“我想换个酒店”
    # 误判成普通 plan_update（甚至带空 patches），这时不能返回一句无操作的
    # 普通回复，更不能让普通补丁绕过酒店/房型确认。
    # 守卫（2026-09-30 评审误路由项）：仅调整已有条目时间（“把酒店挪到晚上，
    # 博物馆放上午”）不是换住宿——放行给下方 plan_update（酒店条目也只允许
    # 纯时间 update），否则会被“酒店”关键词劫持进候选流、回一句“没有候选”
    # 而行程原封不动。
    if (
        _is_hotel_request(req, hotels)
        and mode != "hotel_proposal"
        and not (_is_time_adjustment_request(req.message) and _has_existing_hotel_item(req))
    ):
        return _hotel_proposal_response(req, hotels, _understand_hotel_intent(req, hotels), operations)
    if mode == "hotel_proposal":
        intent = _hotel_intent_from_decision(req, hotels, decision)
        if _has_explicit_hotel_comparison(req.message):
            intent = _with_stay_scope(
                _fallback_hotel_intent(req.message, _hotel_comparison_base_tier(req, hotels)), req, hotels
            )
        response = _hotel_proposal_response(req, hotels, intent, operations, str(decision.get("reply") or ""))
        # 模型候选参数不完整时再用确定性意图识别重试一次，避免无卡片无提示。
        if not response.hotel_options and _is_hotel_request(req, hotels):
            return _hotel_proposal_response(req, hotels, _understand_hotel_intent(req, hotels), operations)
        return response

    if mode in ("plan_update", "rewrite_plan"):
        reduction_requested = _is_reduction_request(req.message)
        reduce_target = _reduce_target_days(req)
        extension_requested = _increase_target_days(req) is not None
        increase_target = _increase_target_days(req)
        final_decision = decision
        plans = _apply_plan_update(decision, req)
        if plans is None:
            # 模型提案未通过确定性校验：把校验要点反馈给模型再修一轮，
            # 仍失败且属于精简/去重/缩短类请求时，才退回确定性兜底。这样既不放弃
            # 模型的语义理解能力，又比直接报错更可靠。
            feedback = (
                "你上一轮返回的 plan_update 未通过校验，不要修改。请严格只使用现有行程中真实存在的 item_id"
                "（切勿编造 id）；缩短行程时必须把被移除日期里的全部项目用 delete 删除或 move 到保留日期；"
                "hotel 项目不得出现在 patches 中；也不要改动价格、坐标、城市、人数或预算。"
            )
            try:
                repaired = _decide_plan_change(req, hotels, feedback=feedback)
            except Exception as exc:
                logger.warning("plan_update repair attempt failed: %s", exc)
                repaired = None
            if repaired and str(repaired.get("mode") or "plan_update") in ("plan_update", "rewrite_plan"):
                repaired_plans = _apply_plan_update(repaired, req)
                if repaired_plans is not None:
                    plans = repaired_plans
                    final_decision = repaired
        if plans is None and reduction_requested:
            # 模型编辑失败时，对“精简/去重/缩短行程”这类请求做确定性兜底，
            # 避免直接报“无法生成安全草稿”而完全无法操作。
            plans = _deterministic_reduce(req, target_days=reduce_target)
        if plans is None and extension_requested:
            # 模型编辑失败时，对“加/延长 N 天”这类请求用单日生成器确定性补齐新增日期，
            # 避免直接报“无法生成安全草稿”而完全无法操作。
            target = increase_target or (req.days + 1)
            plans = _deterministic_extend(req, target)
        if plans is None:
            # 排障锚点（2026-10-06 R1 取证）：主提案+反馈修复轮双双未过校验且无确定性
            # 兜底（或兜底也失败）时在此静默降级，此前无任何日志——夜审只能看到最终
            # 文案，看不到死在哪步。feedback 在首个 plans-is-None 分支必经，此处可用。
            logger.warning(
                "plan edit rejected after repair: mode=%s requested_days=%s current_days=%s repair_produced=%s",
                str(decision.get("mode")),
                _requested_day_count(req.message, req.days),
                req.days,
                "no" if repaired is None else "invalid",
            )
            return ChatTurnResponse(
                reply="### 无法生成安全草稿\n\n模型返回的计划结构或行程元数据不合法，本次未修改任何内容。",
                plans=[],
                changed=False,
                plan_document=_trip_plan_document(req),
            )
        # 防御性去重：任何环节的遗漏都在此最后兜底，保证草稿无跨天重复景点。
        plans = _dedupe_plans(plans)
        # M4（spec §9.1）：作用域硬校验——affected_days 之外的天/preserved 清单
        # 逐字段比对。违例允许带反馈修一轮；仍违例必须如实拒绝，
        # **不得**用宽泛 fallback（确定性兜底）重写后放行。
        scope_issues = _scope_violations(final_decision, req.plans, plans)
        if scope_issues:
            scope_feedback = (
                "你上一轮的提案越出了授权修改范围："
                + "；".join(scope_issues)
                + "。affected_days 只填你实际触及的天；preserved 清单里的条目必须原样保留（时间/费用/坐标都不变）。"
            )
            try:
                scope_repaired = _decide_plan_change(req, hotels, feedback=scope_feedback)
            except Exception as exc:
                logger.warning("scope repair attempt failed: %s", exc)
                scope_repaired = None
            if scope_repaired and str(scope_repaired.get("mode") or "") in ("plan_update", "rewrite_plan"):
                scope_plans = _apply_plan_update(scope_repaired, req)
                if scope_plans is not None:
                    scope_plans = _dedupe_plans(scope_plans)
                    if not _scope_violations(scope_repaired, req.plans, scope_plans):
                        plans = scope_plans
                        final_decision = scope_repaired
                        scope_issues = []
            if scope_issues:
                logger.warning("scope violations survived repair: %s", scope_issues)
                return ChatTurnResponse(
                    reply=(
                        "### 草稿越出了修改范围\n\n"
                        + "；".join(scope_issues[:2])
                        + "。本次没有生成可应用草稿——请重新说明，只改你想改的那几天。"
                    ),
                    plans=[],
                    changed=False,
                    plan_document=_trip_plan_document(req),
                    operations=operations,
                )
        # M4（spec §9.1）：拟变更需求——用户明确指令可以改变旧要求（如「移到第一天」）。
        # 提案随草稿走：未确认前正式行程与正式需求都不改；apply 确认时同事务更新。
        proposed_requirements = _proposed_requirements_from_decision(final_decision, req)
        if proposed_requirements is not None:
            payload = canonical_requirements_payload(proposed_requirements)
            if payload:
                plans = [{**row, "_proposedRequirements": payload} for row in plans]
        # 缩短/延长行程会顺带移除或新增日期里的住宿，这是用户明确要求的副作用，允许直接应用；
        # 其余情况下（天数未变却出现酒店差异）则必须走酒店确认流程，防止模型偷偷改住宿。
        if _hotel_signature(plans) != _hotel_signature(req.plans) and len(plans) == req.days:
            return ChatTurnResponse(
                reply="### 需要先确认住宿\n\n检测到酒店发生变化。请选择酒店和房型后再应用，本次没有直接修改行程。",
                plans=[],
                changed=False,
                plan_document=_trip_plan_document(req),
            )
        if _substantive_plan_signature(plans) == _substantive_plan_signature(req.plans):
            return ChatTurnResponse(
                reply=(
                    "### 还没有形成有效调整\n\n"
                    "本次建议没有实际改变景点、顺序或时间，因此未生成可应用草稿。"
                    "请说明希望删减、移动或延长停留的具体安排。"
                ),
                plans=[],
                changed=False,
                plan_document=_trip_plan_document(req),
                operations=operations,
            )
        reply = _decision_reply(final_decision.get("reply"), _default_plan_update_reply(req, plans))
        conflicts = _all_plan_conflicts(plans)
        if conflicts:
            inherited = set(_all_plan_conflicts(req.plans))
            untouched_days = _untouched_conflicted_days(plans, req.plans)
            novel = [c for c in conflicts if c not in inherited and c[0] not in untouched_days]
            if novel:
                # 模型新引入的重叠仍硬拒绝——安全边界不让步
                day_no, first, second = novel[0]
                return ChatTurnResponse(
                    reply=(
                        "### 新安排存在时间冲突\n\n"
                        f"第 **{day_no} 天**的「{first}」与「{second}」时间重叠，"
                        "本次没有生成可应用草稿。请指定要移动或替换其中哪一项。"
                    ),
                    plans=[],
                    changed=False,
                    plan_document=_trip_plan_document(req),
                    operations=operations,
                )
            # 残留①（2026-10-06）：全部冲突都是基线遗留（编辑没引入新重叠）时放行。
            # 此前只要回显的计划带重叠就拒绝，基线本身有伤的行程（终检放行的存量）
            # 永远改不动——5 轮编辑链 5/5 changed=false 的直接原因之一。放行时如实
            # 注明遗留问题，不假装计划完美。
            day_no, first, second = conflicts[0]
            reply += (
                f"\n\n注：第 {day_no} 天「{first}」与「{second}」时间重叠是原行程遗留问题，"
                "本次编辑未处理；需要的话可以让我单独修复这一处。"
            )
        updated_document = _trip_plan_document(req)
        updated_document["days"] = plans
        updated_document["trip"]["days"] = len(plans)
        if req.start_date:
            with contextlib.suppress(ValueError):
                updated_document["trip"]["end_date"] = (
                    date.fromisoformat(req.start_date) + timedelta(days=len(plans) - 1)
                ).isoformat()
        return ChatTurnResponse(
            reply=reply + "\n\n以上为待应用草稿，点击「应用到行程」后才会保存修改。",
            plans=plans,
            changed=plans != req.plans,
            requires_confirmation=plans != req.plans,
            plan_document=updated_document,
            operations=operations,
        )

    return ChatTurnResponse(
        reply=_decision_reply(decision.get("reply"), "本次没有需要修改的内容。"),
        plans=[],
        changed=False,
        plan_document=_trip_plan_document(req),
        operations=operations,
    )


def run_chat_turn(req: ChatTurnRequest, *, confirmation_thread: str | None = None) -> ChatTurnResponse:
    """对话回合入口（PR-6 确认流）：需要用户确认的提案进入 interrupt 暂停。

    提案构建在 `_chat_turn_response`（既有路径零改动）；当回复需要确认
    （`requires_confirmation` / `pending_action` / 草稿待应用）且指定了
    `confirmation_thread` 时，经 confirm_graph 的 interrupt 把**提案落 checkpoint**
    并暂停，业务确认端以 `Command(resume=…)` 续跑复核（不再只信前端回显）。
    未指定 thread 的直调（agent 面裸用 / 存量测试）保持旧行为：直接返回提案。
    """
    response = _chat_turn_response(req)
    needs_confirm = bool(
        response.requires_confirmation or response.pending_action or (response.changed and response.plans)
    )
    if confirmation_thread and needs_confirm:
        return run_confirmation(response, thread=confirmation_thread)
    return response
