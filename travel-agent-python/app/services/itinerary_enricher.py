"""生成后的可选富化（移植自 Java `ItineraryEnricher`）。

三段彼此独立的增强，**任何一段失败都只降级、不影响已生成好的行程**：
A. AI 管家讲解 → `itinerary_main.plan_note`
B. 景点详细介绍 → `itinerary_item.intro`（按 8 个一批，避免 max_tokens 截断致整批解析失败）
C. 备选池批量后验证 + 介绍补写 → 重写 `itinerary_main.suggestions_json`

三条实现纪律（都是并发写回踩过的坑）：
- 一律**单列 UPDATE**，不用陈旧实体整行回写：富化动辄几十秒，期间用户可能正在编辑同一行程；
- B 段按 `poi_name` 批量覆盖同名列（不按 day_id 精确定位），与 Java 一致；
- 收尾无条件按 `(userId, itineraryId)` 精确失效详情缓存（不是全量 clear，避免一次生成
  把所有用户的详情缓存击穿）。

M5b（spec §11）第四条：**写 suggestions_json 绑定规划修订**。富化任务提交时捕获
`main.planning_revision`，每次回写都在同一事务内以 `WHERE planning_revision = 提交时值`
做条件更新（CAS）——core_ready 后用户的编辑会推进 revision，修订号已移动 = 行程已是
新规划，旧富化放弃写并记日志（绝不用陈旧备选覆盖新行程）。
"""

from __future__ import annotations

import json
import logging
from typing import Any

from pydantic import BaseModel
from sqlalchemy import select, update

from app.agent import run_butler_note, run_poi_intros, verify_suggestion_rows
from app.common.envelope import ApiError
from app.common.llm_route import use_route
from app.db.models import ItineraryDay, ItineraryItem, ItineraryMain
from app.db.session import session_scope
from app.schemas.trip import Suggestion
from app.services import itinerary_query, llm_gateway_service

logger = logging.getLogger(__name__)

INTRO_BATCH_SIZE = 8
MIN_SUGGESTION_INTRO = 50
BUTLER_PREVIEW = 60


def persist_suggestions(itinerary_id: int, suggestions: list[Suggestion] | list[dict] | None) -> None:
    """落库行程级备选池（「发现更多」）；失败只记日志。"""
    if not suggestions:
        return
    rows = [
        suggestion.model_dump(mode="json", by_alias=True) if isinstance(suggestion, BaseModel) else dict(suggestion)
        for suggestion in suggestions
    ]
    try:
        payload = json.dumps(rows, ensure_ascii=False)
    except (TypeError, ValueError) as exc:
        logger.warning("persist suggestions failed for %s: %s", itinerary_id, exc)
        return
    with session_scope() as session:
        session.execute(update(ItineraryMain).where(ItineraryMain.id == itinerary_id).values(suggestions_json=payload))


def enrich_itinerary(user_id: int, itinerary_id: int, request: Any, expected_revision: int | None = None) -> None:
    """生成完成后的富化主入口（编排层提交到富化线程池执行）。

    `expected_revision`（M5b，spec §11）：提交任务时的 `main.planning_revision`
    （_finish 捕获传入）；None = 旧口径不设防（测试/独立调用）。C 段对
    suggestions_json 的每次回写都按它做事务内 CAS。
    """
    # BYOK 路由：enricher_pool 工作线程不继承 plan_days 的 contextvars（实测），
    # 入口自带 user_id——这里进入，A/B/C 三段的 LLM 调用全部走用户网关。
    # 密文解不开（ApiError 409）只跳过富化：行程本体已成功，富化是锦上添花，
    # 不能让 409 裸穿线程池（任务体自己负责异常处理的池契约，终审补丁）。
    try:
        route = llm_gateway_service.resolve_route(user_id)
    except ApiError as exc:
        logger.warning("byok route broken, skip enrichment for itinerary %s: %s", itinerary_id, exc)
        return
    with use_route(route):
        with session_scope() as session:
            main = session.get(ItineraryMain, itinerary_id)
        if main is None:
            return
        _write_butler_note(user_id, main, request, itinerary_id)
        _fill_item_intros(user_id, main, request, itinerary_id)
        try:
            _verify_suggestions(user_id, itinerary_id, expected_revision)
        except Exception as exc:
            logger.warning("suggestion verification failed for %s: %s", itinerary_id, exc)
        try:
            _enrich_suggestion_intros(main, request, itinerary_id, expected_revision)
        except Exception as exc:
            logger.warning("suggestion intros failed for %s: %s", itinerary_id, exc)
        itinerary_query.evict_detail(user_id, itinerary_id)


def _plans_for_butler(itinerary_id: int) -> list[dict[str, Any]]:
    """管家讲解的行程摘要：每天 {day_no, items:[点位名]}（键是 snake_case，Python 契约）。"""
    with session_scope() as session:
        days = (
            session.execute(
                select(ItineraryDay).where(ItineraryDay.itinerary_id == itinerary_id).order_by(ItineraryDay.day_no)
            )
            .scalars()
            .all()
        )
        plans: list[dict[str, Any]] = []
        for day in days:
            names = [
                name
                for name in session.execute(
                    select(ItineraryItem.poi_name).where(ItineraryItem.day_id == day.id).order_by(ItineraryItem.sort_no)
                )
                .scalars()
                .all()
                if name and name.strip()
            ]
            if names:
                plans.append({"day_no": day.day_no, "items": names})
    return plans


def _write_butler_note(user_id: int, main: ItineraryMain, request: Any, itinerary_id: int) -> None:
    plans = _plans_for_butler(itinerary_id)
    if not plans:
        return
    try:
        payload = {
            "city": main.city,
            "days": main.days,
            "persons": main.persons if main.persons is not None else 1,
            # 偏好列本身是逗号拼接串；Java 也是原样传字符串，
            # `run_butler_note` 内部 `"、".join(str)` 会逐字符连接——这是迁移前就有的形状，
            # 不在迁移里改提示词口径（会动到评测基线）。
            "preferences": main.preferences or "",
            "hotel_tier": main.hotel_tier or "",
            "region_hint": getattr(request, "region_hint", None) or "",
            "budget": "" if main.budget is None else main.budget,
            "requirements": getattr(request, "requirements", None) or "",
            "intent": resolve_intent(request),
            "plans": plans,
            "validation_log": None,
        }
        note = run_butler_note(payload) or ""
        if not note.strip():
            return
        with session_scope() as session:
            session.execute(update(ItineraryMain).where(ItineraryMain.id == itinerary_id).values(plan_note=note))
        from app.services import generation_events

        generation_events.butler_note(itinerary_id, len(note), note[:BUTLER_PREVIEW])
    except Exception as exc:
        logger.warning("butler note failed for %s: %s", itinerary_id, exc)
        _publish_degraded(itinerary_id, "butler", str(exc), "跳过讲解")


def _fill_item_intros(user_id: int, main: ItineraryMain, request: Any, itinerary_id: int) -> None:
    with session_scope() as session:
        names = list(
            dict.fromkeys(
                name
                for name in session.execute(
                    select(ItineraryItem.poi_name).where(ItineraryItem.itinerary_id == itinerary_id)
                )
                .scalars()
                .all()
                if name and name.strip()
            )
        )
    intros: dict[str, str] = {}
    try:
        for start in range(0, len(names), INTRO_BATCH_SIZE):
            chunk = names[start : start + INTRO_BATCH_SIZE]
            try:
                intros.update(run_poi_intros(main.city, chunk, resolve_intent(request)) or {})
            except Exception as exc:
                logger.warning("poi intros batch failed for %s: %s", itinerary_id, exc)
        with session_scope() as session:
            for name, intro in intros.items():
                if intro and intro.strip():
                    session.execute(
                        update(ItineraryItem)
                        .where(ItineraryItem.itinerary_id == itinerary_id, ItineraryItem.poi_name == name)
                        .values(intro=intro)
                    )
    except Exception as exc:
        logger.warning("poi intros failed for %s: %s", itinerary_id, exc)
        _publish_degraded(itinerary_id, "poi_intros", str(exc), "跳过景点介绍")


def _load_suggestion_rows(itinerary_id: int) -> list[dict[str, Any]] | None:
    """读备选池 JSON（用最新行，避免拿 finish 时的陈旧快照）；无内容返回 None。"""
    with session_scope() as session:
        fresh = session.get(ItineraryMain, itinerary_id)
        raw = fresh.suggestions_json if fresh is not None else None
    if not raw or not str(raw).strip():
        return None
    try:
        rows = json.loads(raw)
    except (ValueError, TypeError) as exc:
        logger.warning("suggestions json parse failed for %s: %s", itinerary_id, exc)
        return None
    if not isinstance(rows, list) or not rows:
        return None
    return rows


def _store_suggestion_rows(
    user_id: int, itinerary_id: int, rows: list[dict[str, Any]], expected_revision: int | None = None
) -> None:
    """单列 UPDATE 回写：富化动辄几十秒，期间用户可能正在编辑同一行程。

    M5b：`expected_revision` 给定时回写是**事务内 CAS**（`WHERE planning_revision =
    提交时值`）——修订号已移动说明 core_ready 后行程被编辑过，旧富化在此放弃：
    不覆盖新行程、不带回已被排除的内容，rowcount=0 记日志即可（单语句原子判定，
    不做"先读后写"的竞态窗口）。
    """
    try:
        payload = json.dumps(rows, ensure_ascii=False)
    except (TypeError, ValueError) as exc:
        logger.warning("suggestions json encode failed for %s: %s", itinerary_id, exc)
        return
    with session_scope() as session:
        stmt = update(ItineraryMain).where(ItineraryMain.id == itinerary_id).values(suggestions_json=payload)
        if expected_revision is not None:
            stmt = stmt.where(ItineraryMain.planning_revision == int(expected_revision))
        result = session.execute(stmt)
        moved = getattr(result, "rowcount", 0) == 0
    if expected_revision is not None and moved:
        logger.warning(
            "stale enrichment abandoned for itinerary %s: planning_revision moved off %s; "
            "suggestions_json not overwritten",
            itinerary_id,
            expected_revision,
        )
        return
    itinerary_query.evict_detail(user_id, itinerary_id)


def _verify_suggestions(user_id: int, itinerary_id: int, expected_revision: int | None = None) -> dict[str, int]:
    """备选池批量后验证（PLAN-A1 G6-B）：回填坐标 + 丢弃与本次行程矛盾的点。

    主行程的点位在生成时被问过一遍；备选池那 24-40 个名字此前从未被检验，
    "AI 备选"面板因此一直在展示未经核实的名字。这里补上判定，并把结果写回
    `suggestions_json`（前端据此决定打点还是走关键词深链）。
    """
    rows = _load_suggestion_rows(itinerary_id)
    if not rows:
        return {}
    with session_scope() as session:
        fresh = session.get(ItineraryMain, itinerary_id)
        city = str(fresh.city or "") if fresh is not None else ""
    kept, stats = verify_suggestion_rows(rows, city)
    if stats.get("filled") or stats.get("dropped"):
        _store_suggestion_rows(user_id, itinerary_id, kept, expected_revision)
    return stats


def _enrich_suggestion_intros(
    main: ItineraryMain, request: Any, itinerary_id: int, expected_revision: int | None = None
) -> None:
    rows = _load_suggestion_rows(itinerary_id)
    if not rows:
        return

    def needs_intro(row: dict) -> bool:
        intro = row.get("intro")
        return intro is None or not str(intro).strip() or len(str(intro)) < MIN_SUGGESTION_INTRO

    # 只补「没有有效介绍」的行：模型单次生成的 60-100 字亮点遵循度不足，短于 50 字视为无介绍
    names = list(
        dict.fromkeys(
            str(row.get("name"))
            for row in rows
            if isinstance(row, dict)
            and needs_intro(row)
            and row.get("name") is not None
            and str(row.get("name")).strip()
            and str(row.get("name")) != "null"
        )
    )

    intros: dict[str, str] = {}
    for start in range(0, len(names), INTRO_BATCH_SIZE):
        try:
            intros.update(
                run_poi_intros(main.city, names[start : start + INTRO_BATCH_SIZE], resolve_intent(request)) or {}
            )
        except Exception as exc:
            logger.warning("suggestion intros batch failed for %s: %s", itinerary_id, exc)
    if not intros:
        return

    changed = False
    for row in rows:
        if not isinstance(row, dict):
            continue
        name = row.get("name")
        intro = intros.get(str(name)) if name is not None else None
        if intro and intro.strip():
            row["intro"] = intro
            changed = True
    if not changed:
        return
    _store_suggestion_rows(main.user_id, itinerary_id, rows, expected_revision)


def resolve_intent(request: Any) -> str:
    """意图兜底：未填 intent 时用 requirements 作为意图（M1 契约）。"""
    if request is None:
        return ""
    intent = getattr(request, "intent", None)
    if intent and intent.strip():
        return intent.strip()
    return getattr(request, "requirements", None) or ""


def _publish_degraded(itinerary_id: int, scope: str, reason: str, fallback: str) -> None:
    # 延迟导入：富化 → 事件 → trace，避免模块级环依赖
    from app.services import generation_events

    generation_events.degraded(itinerary_id, scope, reason, fallback)
