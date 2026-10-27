"""隔离 MySQL 真并发与图/业务事务故障注入。"""

import json
import os
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker

from app.agent import run_confirmation
from app.agent.editing.chat_draft import confirm_graph as confirmation
from app.common.envelope import ApiError
from app.db import session as db
from app.db.models import Base, ItineraryChatMessage, ItineraryMain, ItineraryVersion, PlanningTask, SysUser
from app.schemas.trip import ChatTurnResponse
from app.services import itinerary_chat, planning_task_store, user_service
from app.services import itinerary_plan_apply as apply
from tests.test_replanning_business import _base, _compare

pytest_plugins = ("tests.test_m1b_requirements_pipeline",)


@pytest.fixture
def mysql_db(client):
    if os.environ.get("SINAN_P1_MYSQL") != "1":
        pytest.skip("requires explicitly enabled isolated MySQL integration")
    old_engine, old_factory = db.get_engine(), db.get_session_factory()
    url = make_url(db.database_url())
    admin = create_engine(url.set(database=None), isolation_level="AUTOCOMMIT", connect_args={"connect_timeout": 3})
    name = "sinan_p1_" + uuid.uuid4().hex[:16]
    engine = None
    with admin.connect() as connection:
        connection.execute(text(f"CREATE DATABASE `{name}` CHARACTER SET utf8mb4"))
    try:
        engine = create_engine(url.set(database=name))
        Base.metadata.create_all(engine)
        db.init_engine(engine, sessionmaker(bind=engine, expire_on_commit=False))
        with db.session_scope() as session:
            session.add_all(
                [
                    SysUser(
                        id=i,
                        username="alice" if i == 1 else f"p1-user-{i}",
                        password=user_service.hash_password("example123"),
                        status=1,
                        role="user",
                    )
                    for i in range(1, 4)
                ]
            )
        assert client.post("/api/auth/login", json={"username": "alice", "password": "example123"}).status_code == 200
        yield engine
    finally:
        db.init_engine(old_engine, old_factory)
        if engine is not None:
            engine.dispose()
        # name 完全由本夹具生成，绝不触碰配置中的正式数据库。
        assert name.startswith("sinan_p1_") and len(name) == 25
        with admin.connect() as connection:
            connection.execute(text(f"DROP DATABASE `{name}`"))
        admin.dispose()


def _proposal(client, monkeypatch):
    identifier, before, target = _base(client, monkeypatch)
    _, compare = _compare(client, identifier, before, target)
    action = client.post(
        f"/api/itinerary/{identifier}/replans/select",
        json={
            "planningRevision": before["planningRevision"],
            "comparisonId": compare["comparisonId"],
            "choice": compare["options"][0]["key"],
        },
    ).json()["data"]
    with db.session_scope() as session:
        message = session.get(ItineraryChatMessage, action["messageId"])
        plans = itinerary_chat.read_plans(message)
        thread = itinerary_chat.action_confirmation_thread(message)
    run_confirmation(ChatTurnResponse(reply="请确认", plans=plans, requires_confirmation=True), thread=thread)
    return identifier, before, action, thread


def _versions(identifier):
    with db.session_scope() as session:
        return len(session.scalars(select(ItineraryVersion).where(ItineraryVersion.itinerary_id == identifier)).all())


def test_mysql_parallel_confirmation_commits_once_and_queue_skips_locked(client, monkeypatch, mysql_db):
    identifier, before, action, _ = _proposal(client, monkeypatch)
    baseline = _versions(identifier)
    start = threading.Barrier(2)

    def submit():
        start.wait(timeout=5)
        try:
            apply.apply_plans(1, identifier, action["messageId"], action["baseRevision"])
            return 200
        except ApiError as exc:
            return exc.status

    with ThreadPoolExecutor(max_workers=2) as pool:
        result = list(pool.map(lambda _: submit(), range(2)))
    assert sorted(result) == [200, 409]
    assert _versions(identifier) == baseline + 2  # 一次应用的前/后快照。
    with db.session_scope() as session:
        assert session.get(ItineraryMain, identifier).planning_revision == before["planningRevision"] + 1
        ids = []
        for user in (2, 3):
            main = ItineraryMain(
                user_id=user, title="排队探针", city="Paris", days=1, persons=1, status=1, gen_state="GENERATING"
            )
            session.add(main)
            session.flush()
            ids.append(main.id)
    fixed_at = datetime.now().replace(microsecond=900000)
    monkeypatch.setattr(planning_task_store.rows, "now", lambda: fixed_at)
    for user, key in zip((2, 3), ids, strict=True):
        planning_task_store.open_attempt(user, key, queued=True)
    with db.session_scope() as session:
        task_rows = session.scalars(select(PlanningTask).where(PlanningTask.itinerary_id.in_(ids))).all()
        assert len(task_rows) == 2 and all(
            r.state == "QUEUED"
            and r.deadline_at > datetime.now()
            and r.lease_expires_at > fixed_at  # DATETIME(0) 将 .9 秒向未来取整。
            and r.cancel_requested_at is None
            for r in task_rows
        ), [(r.state, str(r.deadline_at), str(r.lease_expires_at)) for r in task_rows]
    with mysql_db.connect() as locked:
        transaction = locked.begin()
        locked.execute(select(PlanningTask).where(PlanningTask.itinerary_id == ids[0]).with_for_update())
        with ThreadPoolExecutor(max_workers=1) as pool:
            claimed = pool.submit(planning_task_store.claim_queued).result(timeout=5)
        assert claimed is not None, "另一个任务在锁前可领取，却未被 SKIP LOCKED 领取"
        assert claimed[0].itinerary_id == ids[1]
        transaction.rollback()
    _report(
        "mysql",
        {
            "dialect": "mysql",
            "confirmation_results": result,
            "revision_delta": 1,
            "version_delta": 2,
            "skip_locked_bypassed_held_row": True,
            "datetime_rounding_covered": True,
        },
    )


def test_transaction_rollback_keeps_graph_pending_and_retry_is_single_commit(client, monkeypatch):
    identifier, before, action, thread = _proposal(client, monkeypatch)
    baseline = _versions(identifier)
    original = apply._apply_days

    def fail(*args):
        original(*args)
        raise RuntimeError("injected after item writes")

    with monkeypatch.context() as scoped:
        scoped.setattr(apply, "_apply_days", fail)
        with pytest.raises(RuntimeError, match="injected"):
            apply.apply_plans(1, identifier, action["messageId"], action["baseRevision"])
    assert client.get(f"/api/itinerary/{identifier}").json()["data"] == before
    assert _versions(identifier) == baseline
    assert confirmation.confirm_graph.get_state(confirmation.checkpoint.run_config(thread)).next
    apply.apply_plans(1, identifier, action["messageId"], action["baseRevision"])
    assert not confirmation.confirm_graph.get_state(confirmation.checkpoint.run_config(thread)).next
    assert _versions(identifier) == baseline + 2
    with pytest.raises(ApiError):
        apply.apply_plans(1, identifier, action["messageId"], action["baseRevision"])
    _report(
        "rollback",
        {
            "failed_transaction_unchanged": True,
            "graph_remained_pending": True,
            "retry_commits": 1,
            "duplicate_rejected": True,
        },
    )


def test_checkpoint_completion_failure_does_not_repeat_committed_business(client, monkeypatch):
    identifier, before, action, _ = _proposal(client, monkeypatch)
    baseline = _versions(identifier)

    def fail(*_):
        raise RuntimeError("injected checkpoint completion failure")

    monkeypatch.setattr(apply, "resume_confirmation", fail)
    result = apply.apply_plans(1, identifier, action["messageId"], action["baseRevision"])
    assert result["planningRevision"] == before["planningRevision"] + 1
    assert _versions(identifier) == baseline + 2
    with pytest.raises(ApiError) as rejected:
        apply.apply_plans(1, identifier, action["messageId"], action["baseRevision"])
    assert rejected.value.status == 409
    _report("checkpoint_completion", {"committed_response_success": True, "duplicate_rejected": True})


def _report(key, result):
    target = os.environ.get("SINAN_P1_CONFIRM_REPORT")
    if target:
        path = Path(target)
        path.parent.mkdir(parents=True, exist_ok=True)
        data = json.loads(path.read_text("utf-8")) if path.exists() else {}
        data[key] = result
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
