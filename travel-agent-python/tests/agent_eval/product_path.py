"""离线评测的产品路径驱动：在临时 SQLite 上真跑业务层生成（2026-10-12）。

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
- 整段流式腿的 LLM（`trip_stream.llm_open_trip_stream`）→ 用与阻塞腿同一份 fixture，
  逐条吐 item_preview 后给 final（与 `mock_llm.fixture_open_trip` 同内容）；
- Redis → 指向不可达地址，走进程内降级（本机若跑着 Redis，缓存/计数会跨用例串味）。

其余（研究子图、生成、接地、终检、逐日修复、落库、预算重算）都是生产代码原样执行。
"""

from __future__ import annotations

import tempfile
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.agent.generation.content import trip_stream
from app.agent.runtime import observability
from app.agent.runtime.trace import trace_run
from app.common import cache_store, event_publisher
from app.common.config import settings
from app.db import session as db_session
from app.db.models import Base
from app.schemas.business.itinerary import GenerateTripRequest
from app.schemas.trip import DailyPlan, GenerateResponse, TripItem
from app.services import itinerary_generation, itinerary_query, planning_tasks
from tests.agent_eval import mock_llm

EVAL_USER_ID = 1
_STATUS_BY_GEN_STATE: dict[str, Literal["success", "degraded", "failed"]] = {
    "COMPLETED": "success",
    "PARTIAL": "degraded",
    "FAILED": "failed",
}


def _fixture_trip_stream(trip_req) -> Iterator[tuple]:
    """流式整段腿的离线替身：内容与阻塞腿 fixture 一致，按真实生成器的消息形状吐出。"""
    plans, suggestions = mock_llm.fixture_open_trip(trip_req)
    for plan in plans:
        for ordinal, item in enumerate(plan.get("items") or [], start=1):
            yield ("item_preview", int(plan["day_no"]), ordinal, dict(item))
    yield ("final", plans, suggestions)


@contextmanager
def _isolated_database() -> Iterator[None]:
    """每个用例一份全新的临时库：行程 id、版本快照、预算行互不串用例。"""
    with tempfile.TemporaryDirectory(prefix="sinan-eval-") as tmp:
        engine = create_engine(f"sqlite:///{Path(tmp) / 'eval.db'}")
        Base.metadata.create_all(engine)
        db_session.init_engine(engine, sessionmaker(bind=engine, expire_on_commit=False))
        checkpoint = patch.object(settings, "checkpoint_db_path", str(Path(tmp) / "checkpoints.db"))
        checkpoint.start()
        planning_tasks.reset()
        try:
            yield
        finally:
            planning_tasks.reset()
            checkpoint.stop()
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


def run_product_path(case: dict, run_id: str) -> tuple[GenerateResponse, dict]:
    """真跑一次业务层生成，返回 (读回的交付结果, 合并轨迹)。调用方负责 mock LLM/工具。"""
    inner_traces: list[dict] = []
    contexts: list[dict] = []
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
        enter(patch.object(itinerary_generation, "destination_problem", lambda city: None))
        enter(patch.object(itinerary_generation.generation_pool, "submit", lambda task, *args: task(*args)))
        enter(patch.object(itinerary_generation.enricher_pool, "submit", lambda task, *args: None))
        enter(patch.object(itinerary_generation, "run_plan_context", capture_context))
        enter(patch.object(trip_stream, "llm_open_trip_stream", _fixture_trip_stream))
        enter(patch.object(observability.metrics, "record", capture_run))
        event_publisher.reset_event_publisher()
        cache_store.reset_for_tests()
        # 研究段在生产里不包 observe_run（事件落在外层），外层轨迹接住它；
        # 生成/逐日修复各自的 observe_run 由 capture_run 收集
        outer = enter(trace_run(run_id))
        created = itinerary_generation.generate(EVAL_USER_ID, _trip_request(case))
        itinerary_query.evict_detail(EVAL_USER_ID, int(created["id"]))
        detail = itinerary_query.detail(EVAL_USER_ID, int(created["id"]))
    events = list(outer.events)
    for trace in inner_traces:
        events.extend(trace.get("events") or [])
    trace = {"run_id": run_id, "events": events}
    return _response_from_detail(detail, case, contexts[-1] if contexts else None), trace
