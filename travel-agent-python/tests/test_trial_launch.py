"""Trial access and money boundaries use a real isolated DB and mocked upstream HTTP."""

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.api.business import trial
from app.api.public_web import PublicSurface, install_routes
from app.api.security_headers import SecurityHeadersMiddleware
from app.common import deep_health, llm_client, trial_billing
from app.common.config import settings
from app.common.envelope import ApiError, install_exception_handlers
from app.db import session as db_session
from app.db.models import Base, ItineraryDay, ItineraryMain, SysUser, TrialInvite, TrialLedger, TrialWallet
from app.schemas.trip import DailyPlan, TripItem
from app.schemas.trip_requirements import TripRequirements
from app.services import (
    day_persistence,
    generation_admission,
    itinerary_generation,
    itinerary_query,
    state_and_sessions,
    trial_invites,
    user_service,
)


@pytest.fixture
def trial_db(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'trial.db'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    db_session.init_engine(engine, sessionmaker(bind=engine, expire_on_commit=False))
    monkeypatch.setattr(settings, "registration_invite_required", True)
    monkeypatch.setattr(settings, "trial_initial_credit_cny", 6.0)
    monkeypatch.setattr(settings, "trial_billing_enabled", True)
    monkeypatch.setattr(
        settings,
        "llm_price_cny_json",
        json.dumps({"https://billing.example/v1|sample": {"input": 1, "cached_input": 0.1, "output": 2}}),
    )
    generation_admission.reset_for_tests()
    state_and_sessions.reset_for_tests()
    yield engine
    generation_admission.reset_for_tests()
    state_and_sessions.reset_for_tests()
    db_session.init_engine(None, None)
    engine.dispose()


def register_trial(name="trialuser", code=None):
    code = code or trial_invites.issue(1, "operator")[0]
    return user_service.register(name, "example123", None, name, code)


def test_invitation_atomic_competition_and_one_time_grant(trial_db):
    code = trial_invites.issue(1, "operator")[0]
    barrier = threading.Barrier(2)

    def attempt(name):
        barrier.wait()
        try:
            return register_trial(name, code)["id"]
        except ApiError:
            return None

    with ThreadPoolExecutor(2) as pool:
        ids = list(pool.map(attempt, ["firstuser", "seconduser"]))
    assert sum(value is not None for value in ids) == 1
    with db_session.session_scope() as session:
        assert len(session.scalars(select(SysUser)).all()) == 1
        wallet = session.scalars(select(TrialWallet)).one()
        assert wallet.balance_micros == 6_000_000
        assert len(session.scalars(select(TrialLedger)).all()) == 1
        assert session.scalars(select(TrialInvite)).one().code_hash != code


def test_bad_invitation_rolls_back_user_and_credit(trial_db):
    with pytest.raises(ApiError, match="邀请码"):
        register_trial(code="invalid")
    with db_session.session_scope() as session:
        assert session.scalars(select(SysUser)).all() == []
        assert session.scalars(select(TrialWallet)).all() == []


def test_fee_reserve_settle_cached_and_idempotency(trial_db):
    user_id = register_trial()["id"]
    with (
        trial_billing.payer_scope(user_id),
        trial_billing.billable_call(
            "https://billing.example/v1",
            {
                "model": "sample",
                "messages": [{"role": "user", "content": "hello"}],
                "max_tokens": 100,
            },
        ) as charge,
    ):
        assert charge is not None
        assert trial_billing.wallet_view(user_id)["balanceCny"] < 6
        charge.settle({"prompt_tokens": 100, "completion_tokens": 50, "prompt_cache_hit_tokens": 50})
        charge.settle({"prompt_tokens": 100, "completion_tokens": 50, "prompt_cache_hit_tokens": 50})
    wallet = trial_billing.wallet_view(user_id)
    assert wallet["spentCny"] == 0.000155
    assert wallet["balanceCny"] == 5.999845


def test_unknown_usage_survives_restart_and_requires_reconciliation(trial_db):
    user_id = register_trial()["id"]
    with (
        trial_billing.payer_scope(user_id),
        trial_billing.billable_call(
            "https://billing.example/v1",
            {
                "model": "sample",
                "messages": [],
                "max_tokens": 100,
            },
        ) as charge,
    ):
        assert charge is not None
        charge.settle(None)
        charge_id = charge.id
    before = trial_billing.wallet_view(user_id)["balanceCny"]
    trial_billing.recover_reservations()
    assert trial_billing.wallet_view(user_id)["balanceCny"] == before
    trial_billing.reconcile(charge_id, 0.001, "verified provider bill", "admin")
    wallet = trial_billing.wallet_view(user_id)
    assert wallet["balanceCny"] == 5.999
    assert wallet["spentCny"] == 0.001
    assert next(entry for entry in wallet["entries"] if entry["id"] == charge_id)["detail"]["reconciledBy"] == "admin"


def test_parallel_reservations_cannot_spend_same_credit(trial_db, monkeypatch):
    user_id = register_trial()["id"]
    monkeypatch.setattr(
        settings,
        "llm_price_cny_json",
        json.dumps({"https://billing.example/v1|sample": {"input": 1000, "cached_input": 1000, "output": 2000}}),
    )

    def attempt(_):
        try:
            with (
                trial_billing.payer_scope(user_id),
                trial_billing.billable_call(
                    "https://billing.example/v1",
                    {
                        "model": "sample",
                        "messages": [],
                        "max_tokens": 1000,
                    },
                ),
            ):
                return True
        except ApiError as exc:
            assert exc.status == 402
            return False

    with ThreadPoolExecutor(4) as pool:
        results = list(pool.map(attempt, range(4)))
    assert sum(results) == 1
    assert trial_billing.wallet_view(user_id)["balanceCny"] >= 0


def test_grant_is_audited_and_replayed_once(trial_db):
    user_id = register_trial()["id"]
    request_id = str(uuid4())
    for _ in range(2):
        trial_billing.grant(user_id, 1.25, "trial extension", "admin", request_id)
    assert trial_billing.wallet_view(user_id)["balanceCny"] == 7.25
    with pytest.raises(ApiError):
        trial_billing.grant(user_id, 2, "changed", "admin", request_id)


def test_real_client_billing_and_insufficient_balance_never_calls_upstream(trial_db, monkeypatch):
    user_id = register_trial()["id"]
    calls = []

    def upstream(request):
        calls.append(request)
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "ok"}}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 50},
            },
        )

    monkeypatch.setattr(llm_client, "_http_client", httpx.Client(transport=httpx.MockTransport(upstream)))
    client = llm_client.LLMClient(base_url="https://billing.example/v1", model="sample", max_attempts=1)
    with trial_billing.payer_scope(user_id):
        assert client.chat([{"role": "user", "content": "hello"}]) == "ok"
        with db_session.session_scope() as session:
            wallet_row = session.get(TrialWallet, user_id)
            assert wallet_row is not None
            wallet_row.balance_micros = 0
        with pytest.raises(ApiError) as exc:
            client.chat([{"role": "user", "content": "hello"}])
        assert exc.value.status == 402
    assert len(calls) == 1
    assert trial_billing.wallet_view(user_id)["spentCny"] == 0.0002


@pytest.mark.parametrize("stream", [False, True])
def test_usage_missing_is_frozen_for_both_client_paths(trial_db, monkeypatch, stream):
    user_id = register_trial()["id"]

    def upstream(_request):
        if stream:
            return httpx.Response(200, text='data: {"choices":[{"delta":{"content":"ok"}}]}\n\ndata: [DONE]\n\n')
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    monkeypatch.setattr(llm_client, "_http_client", httpx.Client(transport=httpx.MockTransport(upstream)))
    client = llm_client.LLMClient(base_url="https://billing.example/v1", model="sample", max_attempts=1)
    with trial_billing.payer_scope(user_id):
        method = client.stream_chat if stream else client.chat
        assert method([{"role": "user", "content": "hello"}]) == "ok"
    wallet = trial_billing.wallet_view(user_id)
    charge = next(entry for entry in wallet["entries"] if entry["kind"] == "call")
    assert charge["status"] == "unknown" and wallet["balanceCny"] < 6


def test_byok_does_not_exempt_platform_role_client(trial_db, monkeypatch):
    from app.common.llm_route import LLMRoute, use_route

    user_id = register_trial()["id"]

    def upstream(_request):
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "ok"}}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 50},
            },
        )

    monkeypatch.setattr(llm_client, "_http_client", httpx.Client(transport=httpx.MockTransport(upstream)))
    monkeypatch.setattr(llm_client, "_guarded_http_client", httpx.Client(transport=httpx.MockTransport(upstream)))
    with trial_billing.payer_scope(user_id), use_route(LLMRoute("https://byok.example/v1", "example-key", "sample")):
        llm_client.get_llm_client().chat([{"role": "user", "content": "hello"}])
        assert trial_billing.wallet_view(user_id)["spentCny"] == 0
        llm_client.LLMClient(base_url="https://billing.example/v1", model="sample").chat(
            [{"role": "user", "content": "hello"}]
        )
    assert trial_billing.wallet_view(user_id)["spentCny"] == 0.0002


def test_chunked_body_is_bounded_without_nginx():
    from fastapi import Request

    from main import API_BODY_MAX_BYTES, RequestBodySizeLimitMiddleware

    app = FastAPI()
    app.add_middleware(RequestBodySizeLimitMiddleware)

    @app.post("/api/body")
    async def consume(request: Request):
        return {"bytes": len(await request.body())}

    client = TestClient(app)
    response = client.post("/api/body", content=iter([b"x" * (API_BODY_MAX_BYTES + 1)]))
    assert response.status_code == 413


def test_admin_auth_policy_and_wallet_privacy(trial_db):
    app = FastAPI()
    app.include_router(trial.router)
    install_exception_handlers(app)
    client = TestClient(app)
    assert client.get("/api/auth/trial-policy").status_code == 200
    assert client.get("/api/admin/trial/users").status_code == 401
    user_id = register_trial()["id"]
    token = user_service.login("trialuser", "example123", "ip").token
    client.cookies.set("TA_AUTH", token)
    assert client.get("/api/admin/trial/users").status_code == 403
    assert client.get("/api/user/trial-wallet").json()["data"]["userId"] == user_id
    with db_session.session_scope() as session:
        admin_user = session.get(SysUser, user_id)
        assert admin_user is not None
        admin_user.role = "admin"
    assert client.get("/api/admin/trial/users").status_code == 200
    assert client.post("/api/admin/trial/invites", json={"count": 1}).status_code == 200


def test_unknown_persists_warning_without_pending(trial_db):
    user_id = register_trial()["id"]
    with db_session.session_scope() as session:
        main = ItineraryMain(
            user_id=user_id,
            city="杭州",
            title="test",
            days=1,
            persons=1,
            status=2,
            gen_state="COMPLETED",
            budget=Decimal(1000),
            stay_nights=0,
        )
        session.add(main)
        session.flush()
        trip_id = main.id
        session.add(ItineraryDay(itinerary_id=trip_id, day_no=1, generation_status="PENDING"))
    plan = DailyPlan(
        day_no=1,
        items=[
            TripItem(item_type="attraction", poi_name="A", start_time="09:00", end_time="11:00"),
            TripItem(item_type="food", poi_name="B", start_time="11:00", end_time="12:00"),
            TripItem(item_type="attraction", poi_name="C", start_time="13:00", end_time="16:00"),
        ],
    )
    command = itinerary_generation.GenerateCommand(
        city="杭州",
        days=1,
        persons=1,
        stay_nights=0,
        requirements_struct=TripRequirements(excluded_categories=["aquarium"]),
    )
    day_persistence.persist(trip_id, command, 1, plan, "action", "fingerprint")
    persisted = day_persistence.find_day(trip_id, 1)
    assert persisted is not None and persisted.generation_status == "SUCCEEDED"
    detail = itinerary_query.detail(user_id, trip_id)
    assert any(issue["code"] == "CONSTRAINT_UNKNOWN" for issue in detail["qualityReport"]["warnings"])


def test_user_admission_keeps_other_users_and_replay_available(trial_db, monkeypatch):
    monkeypatch.setattr(settings, "user_generation_limit_enabled", True)
    generation_admission.started(1, 99)
    with pytest.raises(ApiError):
        with generation_admission.new_trip(1, None):
            pytest.fail("must not admit a second trip")
    with generation_admission.new_trip(1, 99), generation_admission.new_trip(2, None):
        pass
    generation_admission.finished(99)
    with generation_admission.new_trip(1, None):
        pass


def test_hosted_surface_spa_internal_denial_and_readiness(tmp_path, monkeypatch):
    (tmp_path / "index.html").write_text("<h1>Sinan</h1>")
    monkeypatch.setattr(settings, "frontend_dist", str(tmp_path))
    monkeypatch.setattr(deep_health, "snapshot", lambda: {"status": "degraded"})
    app = FastAPI()
    app.add_middleware(PublicSurface)
    app.add_middleware(SecurityHeadersMiddleware)
    install_routes(app)
    client = TestClient(app)
    assert client.get("/demo").text == "<h1>Sinan</h1>"
    for path in ("/api/agent/v1/metrics", "/mcp", "/openapi.json", "/api/not-found", "/missing.js"):
        response = client.get(path)
        assert response.status_code == 404
        assert response.headers["x-content-type-options"] == "nosniff"
    assert client.get("/healthz").status_code == 503
    monkeypatch.setattr(deep_health, "snapshot", lambda: {"status": "up"})
    assert client.get("/healthz").status_code == 200


def test_shutdown_pools_share_budget_and_release_on_timeout(caplog):
    import asyncio

    from app.common.shutdown import drain

    release = threading.Event()

    class SlowPool:
        def shutdown(self):
            release.wait(3)

    async def check():
        try:
            await drain((SlowPool(), SlowPool()), timeout=0.01)
            assert not release.is_set()
            assert "global" in caplog.text
        finally:
            release.set()

    asyncio.run(check())


def test_operations_dependency_transition_and_failure_dedup(trial_db, monkeypatch):
    from app.common import operations

    events = []
    monkeypatch.setattr(operations, "_dependency_status", "")
    monkeypatch.setattr(operations, "_seen_failures", set())
    monkeypatch.setattr(operations, "notify", lambda event, detail: events.append((event, detail)))
    monkeypatch.setattr(deep_health, "snapshot", lambda: {"status": "degraded", "mysql": "down", "redis": "up"})
    operations.check()
    operations.check()
    assert [event for event, _ in events] == ["dependencies.degraded"]
    with db_session.session_scope() as session:
        session.add(
            ItineraryMain(user_id=1, title="failed", city="杭州", days=1, persons=1, status=3, gen_state="FAILED")
        )
    monkeypatch.setattr(deep_health, "snapshot", lambda: {"status": "up", "mysql": "up", "redis": "up"})
    operations.check()
    operations.check()
    assert [event for event, _ in events] == ["dependencies.degraded", "dependencies.up", "generation.failed"]


def test_railway_ip_source_ignores_forged_xff(monkeypatch):
    from fastapi import Request

    from app.common.client_ip import client_ip

    monkeypatch.setattr(settings, "railway_edge_headers", True)

    def request(real_ip, xff):
        return Request(
            {
                "type": "http",
                "client": ("10.0.0.2", 1234),
                "headers": [(b"x-real-ip", real_ip.encode()), (b"x-forwarded-for", xff.encode())],
            }
        )

    assert client_ip(request("203.0.113.10", "1.1.1.1")) == "203.0.113.10"
    assert client_ip(request("203.0.113.10", "2.2.2.2")) == "203.0.113.10"
    assert client_ip(request("203.0.113.11", "1.1.1.1")) == "203.0.113.11"
    assert client_ip(request("bad-ip", "1.1.1.1")) == "10.0.0.2"


def test_no_price_is_blocked_before_spending(trial_db, monkeypatch):
    user_id = register_trial()["id"]
    monkeypatch.setattr(settings, "llm_price_cny_json", "{}")
    with trial_billing.payer_scope(user_id), pytest.raises(ApiError) as exc:
        with trial_billing.billable_call("https://billing.example/v1", {"model": "sample", "max_tokens": 1}):
            pytest.fail("unpriced call must be rejected")
    assert exc.value.status == 503
    assert trial_billing.wallet_view(user_id)["balanceCny"] == 6
