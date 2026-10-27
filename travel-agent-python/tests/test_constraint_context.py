"""阶段快照的身份隔离及失权写保护。"""

import pytest

from app.agent import TaskOwnershipLost, load_constraint_context, save_constraint_context, use_task_limits
from app.db.models import PlanningTask
from app.db.session import session_scope
from app.services import constraint_context, planning_task_store
from tests.test_replanning_business import _base

pytest_plugins = ("tests.test_m1b_requirements_pipeline",)


def test_snapshot_identity_and_stale_owner(client, monkeypatch):
    identifier, _, _ = _base(client, monkeypatch)
    limits, _ = planning_task_store.open_attempt(1, identifier)
    deadline = limits.deadline_at
    save_constraint_context(1, identifier, "fingerprint", deadline, {"marker": "saved"})
    assert load_constraint_context(1, identifier, "fingerprint", deadline) == {"marker": "saved"}
    for user, itinerary, fingerprint, expiry in (
        (2, identifier, "fingerprint", deadline),
        (1, identifier + 1, "fingerprint", deadline),
        (1, identifier, "changed", deadline),
        (1, identifier, "fingerprint", deadline + 1),
    ):
        assert load_constraint_context(user, itinerary, fingerprint, expiry) is None
    with session_scope() as session:
        session.get(PlanningTask, identifier).task_id = "new-owner"
    with use_task_limits(limits), pytest.raises(TaskOwnershipLost):
        constraint_context.save_context(1, identifier, "fingerprint", {"marker": "stale"})
    assert load_constraint_context(1, identifier, "fingerprint", deadline) == {"marker": "saved"}
