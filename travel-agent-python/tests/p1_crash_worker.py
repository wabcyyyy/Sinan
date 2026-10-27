"""独立进程崩溃探针，只操作传入的隔离 SQLite；永不请求外网。"""

import argparse
import json
import os
import socket
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.agent import RunLimitExceeded, TaskOwnershipLost, current_limits
from app.agent.generation.orchestration import constraint_planning as planner
from app.common.config import settings
from app.db import session as db
from app.db.models import Base, ItineraryDay, ItineraryItem, ItineraryMain, PlanningTask, SysUser
from app.schemas.business.itinerary import GenerateTripRequest
from app.services import (
    constraint_context,
    day_persistence,
    generation_recovery,
    itinerary_generation,
    planning_queue,
    planning_task_rows,
    planning_task_store,
    planning_tasks,
)
from tests.constraint_planning_cases import frozen_context, planning_request


def run(directory: Path, stage: str, resume: bool):
    def forbidden(*_):
        raise AssertionError("network prohibited in crash fixture")

    socket.socket.connect = forbidden
    settings.redis_url = "redis://127.0.0.1:1/0"
    settings.llm_api_key = ""
    settings.nominatim_enabled = False
    settings.otm_api_key = settings.ors_base_url = settings.osrm_base_url = ""
    engine = create_engine(f"sqlite:///{(directory / 'business.db').as_posix()}")
    db.init_engine(engine, sessionmaker(bind=engine, expire_on_commit=False))

    def event(name):
        with (directory / "events.jsonl").open("a", encoding="utf-8") as output:
            output.write(json.dumps({"event": name, "pid": os.getpid()}) + "\n")

    def research(*args, **kwargs):
        limits = current_limits()
        limits.max_llm_calls = 1
        limits.max_tokens = 20
        key = limits.reserve_llm(10, 10)
        event("simulated_request_reserved")
        if stage == "reserve" and not resume:
            os._exit(91)
        limits.settle_llm(key, {"prompt_tokens": 10, "completion_tokens": 10})
        return frozen_context()

    def routes(points, **kwargs):
        event("simulated_route_matrix")
        return {
            (a["poi_id"], b["poi_id"]): {"duration_min": 5, "source": "osrm"}
            for a in points
            for b in points
            if a is not b
        }

    itinerary_generation.run_plan_context = research
    planner.get_route_matrix = routes
    itinerary_generation.generation_pool.submit = lambda fn, *args: fn(*args)
    itinerary_generation.enricher_pool.submit = lambda fn, *args: (
        planning_tasks.release(args[1]) if len(args) > 1 else None
    )
    itinerary_generation._submit_budget_recalculate = lambda *_: None
    save = constraint_context.save_context
    persist = day_persistence.persist
    complete = day_persistence.complete_trip

    def save_then_exit(*args):
        save(*args)
        context = args[-1]
        if not resume and (
            (stage == "research" and "_constraint_routes" not in context)
            or (stage == "matrix" and "_constraint_routes" in context and "_constraint_plans" not in context)
            or (stage == "solution" and "_constraint_plans" in context)
        ):
            os._exit(91)

    def persist_then_exit(*args, **kwargs):
        persist(*args, **kwargs)
        if not resume and stage == "first_day":
            os._exit(91)

    def complete_then_exit(*args, **kwargs):
        complete(*args, **kwargs)
        if not resume and stage == "terminal":
            os._exit(91)

    constraint_context.save_context = save_then_exit
    day_persistence.persist = persist_then_exit
    day_persistence.complete_trip = complete_then_exit
    if not resume:
        Base.metadata.create_all(engine)
        with db.session_scope() as session:
            session.add(SysUser(id=1, username="crash-fixture", password="example-only", role="user", status=1))
        request = planning_request()
        body = GenerateTripRequest(
            city=request.city,
            days=2,
            persons=2,
            stayNights=0,
            startDate="2026-10-25",
            budget=200,
            requirementsStruct=request.requirements_struct,
        )
        created = itinerary_generation.generate(1, body)
        limits = planning_tasks.claim_queued()
        itinerary_generation.plan_days(1, created["id"], planning_queue.command_for(created["id"]), limits=limits)
        raise AssertionError("crash boundary was not reached")
    with db.session_scope() as session:
        row = session.scalar(select(PlanningTask))
        identifier, old_task = row.itinerary_id, row.task_id
        before_deadline = row.deadline_at.isoformat()
        row.lease_expires_at = datetime.now() - timedelta(seconds=1)
        old_items = [(p.id, p.poi_name, p.start_time.isoformat()) for p in session.scalars(select(ItineraryItem))]
    generation_recovery.recover_one(identifier, False)
    with db.session_scope() as session:
        row = session.get(PlanningTask, identifier)
        main = session.get(ItineraryMain, identifier)
        counters = planning_task_rows.store_totals(row)
        limits = planning_task_rows.build_limits(row, {**json.loads(row.budget_json), **counters})
        items = [(p.id, p.poi_name, p.start_time.isoformat()) for p in session.scalars(select(ItineraryItem))]
        result = {
            "stage": stage,
            "pid": os.getpid(),
            "state": row.state,
            "gen_state": main.gen_state,
            "deadline_preserved": row.deadline_at.isoformat() == before_deadline,
            "consumed_llm_calls": row.consumed_llm_calls,
            "consumed_prompt_tokens": row.consumed_prompt_tokens,
            "consumed_completion_tokens": row.consumed_completion_tokens,
            "already_delivered_items_unchanged": all(p in items for p in old_items),
            "delivered_days": sum(d.generation_status == "SUCCEEDED" for d in session.scalars(select(ItineraryDay))),
            "item_count": len(items),
        }
    try:
        limits.check("llm")
        result["next_call_blocked"] = False
    except RunLimitExceeded:
        result["next_call_blocked"] = True
    try:
        planning_task_store.apply_budget(identifier, old_task, {}, {})
        result["old_owner_rejected"] = False
    except TaskOwnershipLost:
        result["old_owner_rejected"] = True
    (directory / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    planning_tasks.reset()
    engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--stage", required=True)
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args()
    run(args.directory, args.stage, args.resume)
    # 所有恢复断言所需结果已同步写盘；本探针不测试应用后台线程的优雅停机。
    os._exit(0)
