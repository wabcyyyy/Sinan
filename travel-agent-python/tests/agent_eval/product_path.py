"""评测共用的产品路径驱动：在临时 SQLite 上真跑业务层生成。

此前离线评测跑的是统一图的 trip 分支（parse → research → generate → reflect → format），
而用户走的是业务层 `itinerary_generation.plan_days`：研究 → 整段流式生成 → 对落库天跑
validate_plans 终检 → 违规/缺失天逐日补齐。两条链的交付口径不同（终检、逐日修复、落库
往返都只在后者），评测量到的不是用户拿到的东西。

本模块让评测走后者，且**不在评测里重写编排**：真调 `itinerary_generation.generate`
（建壳）与 `plan_days`（同步执行），生成结束后从库里读回详情再算指标。被钉死的只有
产品路径之外、或会出网的环节：

- 目的地预检 `destination_problem`（会调地理编码）→ 恒通过；
- 后台富化池（备选池联网补齐，交付之后才跑）→ 不执行；交付预算由 `_finish` 同步重算；
- 生成池 → 同步执行（评测要等结果，不改变 plan_days 本身的逻辑）；
- Redis → 指向不可达地址，走进程内降级（本机若跑着 Redis，缓存/计数会跨用例串味）。

本模块不替换模型/检索；离线调用方注入 fixture，真实评测使用配置的模型与工具。
其余（研究子图、生成、接地、终检、逐日修复、落库、预算重算）都是生产代码原样执行。
"""

from __future__ import annotations

import sqlite3
import tempfile
import time
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal
from unittest.mock import patch

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.sqlite import SqliteSaver
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.agent.generation.orchestration.trip_graph import unified_agent_graph
from app.agent.runtime import observability
from app.agent.runtime.trace import trace_run
from app.common import cache_store, event_publisher, redis_client
from app.common.config import settings
from app.common.trial_billing import open_wallet
from app.db import session as db_session
from app.db.models import Base, SysUser
from app.schemas.business.itinerary import GenerateTripRequest
from app.schemas.trip import DailyPlan, GenerateResponse, TripItem
from app.services import itinerary_generation, itinerary_query, planning_tasks

EVAL_USER_ID = 1
PRODUCT_PATH_IDENTITY = "business-service plan_days (isolated SQLite; core delivery)"
_STATUS_BY_GEN_STATE: dict[str, Literal["success", "degraded", "failed"]] = {
    "COMPLETED": "success",
    "PARTIAL": "degraded",
    "FAILED": "failed",
}


@contextmanager
def _isolated_database() -> Iterator[None]:
    """每个用例一份全新的临时库：行程 id、版本快照、预算行互不串用例。"""
    with tempfile.TemporaryDirectory(prefix="sinan-eval-") as tmp:
        engine = create_engine(f"sqlite:///{Path(tmp) / 'eval.db'}")
        Base.metadata.create_all(engine)
        db_session.init_engine(engine, sessionmaker(bind=engine, expire_on_commit=False))
        connection = sqlite3.connect(str(Path(tmp) / "checkpoints.db"), check_same_thread=False)
        previous_saver = unified_agent_graph.checkpointer
        assert isinstance(previous_saver, BaseCheckpointSaver)
        saver = SqliteSaver(connection, serde=previous_saver.serde)
        checkpoint = patch.object(unified_agent_graph, "checkpointer", saver)
        checkpoint.start()
        planning_tasks.reset()
        try:
            with db_session.session_scope() as session:
                session.add(SysUser(id=EVAL_USER_ID, username="eval-user", password="example-evaluation-no-login"))
                session.flush()
                open_wallet(session, EVAL_USER_ID)
            yield
        finally:
            planning_tasks.reset()
            checkpoint.stop()
            connection.close()
            db_session.init_engine(None, None)
            engine.dispose()


def _trip_request(case: dict) -> GenerateTripRequest:
    """case → 业务面建行程请求（前端 POST /api/itinerary/generate 的同一形状）。"""
    budget = case.get("budget")
    return GenerateTripRequest(
        city=case["city"],
        days=case["days"],
        persons=case.get("persons", 1),
        startDate=case.get("start_date"),
        budget=None if budget is None else Decimal(str(budget)),
        preferences=case.get("preferences") or [],
        hotelTier=case.get("hotel_tier"),
        regionHint=case.get("region_hint"),
        originCity=case.get("origin_city"),
        requirements=case.get("requirements"),
        intent=case.get("intent"),
        requirementsStruct=case.get("requirements_struct"),
    )


def _response_from_detail(detail: dict[str, Any], case: dict, context: dict | None) -> GenerateResponse:
    """库里读回的详情 → 指标层吃的 GenerateResponse（只取指标用到的键，不发明数据）。"""
    plans = [
        DailyPlan(
            day_no=int(day["dayNo"]),
            theme=day.get("theme"),
            note=day.get("note"),
            practical_notes=day.get("practicalNotes") or [],
            items=[TripItem.model_validate(item) for item in day.get("items") or []],
        )
        for day in detail.get("dayList") or []
    ]
    gen_state = str(detail.get("genState") or "")
    return GenerateResponse(
        city=case["city"],
        days=int(case["days"]),
        title=str(detail.get("title") or ""),
        trip_theme=detail.get("tripTheme"),
        daily_plans=plans,
        budget_estimate={str(row["category"]): float(row.get("amount") or 0) for row in detail.get("budgetList") or []},
        # 研究报告来自研究上下文（与图路径的 schedule_report.research 同形）；接地否证
        # 计数只存在于图路径的 schedule_report，产品路径不落库，如实为空
        schedule_report={"research": (context or {}).get("research_report") or {}},
        status=_STATUS_BY_GEN_STATE.get(gen_state, "failed"),
        status_reason=detail.get("planNote") if gen_state != "COMPLETED" else None,
    )


@contextmanager
def run_product_path(case: dict, run_id: str) -> Iterator[tuple[GenerateResponse, dict, dict]]:
    """在隔离环境内交出 (最终交付, 全阶段轨迹, 研究候选目录)，供调用方计算指标。"""
    inner_traces: list[dict] = []
    contexts: list[dict] = []
    detail: dict | None = None
    failure: Exception | None = None
    record_run = observability.metrics.record
    plan_context = itinerary_generation.run_plan_context

    def capture_run(trace: dict, *, success: bool) -> None:
        inner_traces.append(trace)
        record_run(trace, success=success)

    def capture_context(*args, **kwargs) -> dict:
        context = plan_context(*args, **kwargs)
        contexts.append(context)
        return context

    with ExitStack() as stack:
        enter = stack.enter_context
        enter(_isolated_database())
        enter(patch.object(settings, "redis_url", "redis://127.0.0.1:1/0"))
        redis_client.reset_for_tests()
        stack.callback(redis_client.reset_for_tests)
        enter(patch.object(itinerary_generation, "destination_problem", lambda city: None))
        enter(patch.object(itinerary_generation.generation_pool, "submit", lambda task, *args: task(*args)))
        enter(patch.object(itinerary_generation.enricher_pool, "submit", lambda task, *args: None))
        enter(patch.object(itinerary_generation, "run_plan_context", capture_context))
        enter(patch.object(observability.metrics, "record", capture_run))
        event_publisher.reset_event_publisher()
        cache_store.reset_for_tests()
        # 研究/生成/逐日修复各自的 observe_run 由 capture_run 收集；
        # 外层轨迹接住业务编排事件，不重复合并内层事件。
        outer = enter(trace_run(run_id))
        started = time.perf_counter()
        try:
            created = itinerary_generation.generate(EVAL_USER_ID, _trip_request(case))
            itinerary_query.evict_detail(EVAL_USER_ID, int(created["id"]))
            detail = itinerary_query.detail(EVAL_USER_ID, int(created["id"]))
        except Exception as exc:
            failure = exc
        duration_ms = (time.perf_counter() - started) * 1000
        events = list(outer.events)
        for trace in inner_traces:
            events.extend(trace.get("events") or [])
        context = contexts[-1] if contexts else {}
        trace = {"run_id": run_id, "events": events, "duration_ms": round(duration_ms, 2)}
        catalog = {
            "attractions": context.get("candidates") or [],
            "foods": context.get("foods") or [],
            "hotels": context.get("hotels") or [],
            "consumption": context.get("consumption") or {},
        }
        if failure is not None:
            # 执行/读回失败也保留已经发生的调用轨迹，不能把本次费用清零。
            response = GenerateResponse(
                city=case["city"],
                days=int(case["days"]),
                title="评测执行失败",
                daily_plans=[],
                status="failed",
                status_reason=str(failure)[:200],
            )
        else:
            assert detail is not None
            response = _response_from_detail(detail, case, context)
        # 证据票查询仍需同一缓存后端；指标算完后才退出临时库与 Redis 隔离。
        yield response, trace, catalog
