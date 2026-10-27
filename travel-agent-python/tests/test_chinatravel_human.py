"""Human-only unlimited scope, process-safe money accounting and final-object export boundaries."""

import json
import subprocess
import sys
import time
from decimal import Decimal

import pytest

from scripts.chinatravel_original_client import OriginalBudget
from scripts.sinan_nl_budget import APPROVAL, HUMAN_UNLIMITED, NIGHT_UPGRADE


@pytest.fixture(autouse=True)
def project_thinking(monkeypatch):
    from app.common.config import settings

    monkeypatch.setattr(settings, "llm_enable_thinking", True)
    monkeypatch.setattr(settings, "llm_reasoning_effort", "low")
    monkeypatch.setenv("LLM_ENABLE_THINKING", "true")
    monkeypatch.setenv("LLM_REASONING_EFFORT", "low")


def ledger_fixture(tmp_path, authorized=True):
    # Nonspendable fixture: never provided to an HTTP client or copied from the real ledger.
    state = {
        "approval": APPROVAL,
        "approval_upgrade": NIGHT_UPGRADE,
        "cap_cny": None,
        "cap_mode": "unlimited",
        "carried_peak_upper_cny": "0",
        "active_authorization": {"id": HUMAN_UNLIMITED if authorized else "missing", "uids": ["fixture"]},
        "records": [{"id": "old-unknown", "status": "issued_usage_unknown", "reserved_cny": "29"}],
    }
    path = tmp_path / "nonspendable-ledger.json"
    path.write_text(json.dumps(state), encoding="utf8")
    return path


def payload():
    return {
        "model": "deepseek-flash",
        "thinking": {"type": "enabled"},
        "reasoning_effort": "low",
        "max_tokens": 4096,
        "messages": [{"role": "user", "content": "synthetic fixture"}],
    }


def test_explicit_unlimited_scope_keeps_unknown_and_rejects_legacy_ids(tmp_path):
    path = ledger_fixture(tmp_path)
    book = OriginalBudget(path, approved=True)
    with pytest.raises(ValueError, match="Human154 only"):
        book.reserve("formal-v1-fixture-A-0", payload())
    book.reserve("human-v1-fixture-A-0", payload())
    assert book.total() > 30
    book.settle("human-v1-fixture-A-0", {"usage": {"prompt_tokens": 10, "completion_tokens": 5}})
    assert book.total() == Decimal("29.00006")
    assert book.state["records"][0]["reserved_cny"] == "29"
    with pytest.raises(ValueError, match="duplicate"):
        book.reserve("human-v1-fixture-A-0", payload())


def test_null_cap_without_human_authorization_is_rejected(tmp_path):
    with pytest.raises(ValueError, match="authorization"):
        OriginalBudget(ledger_fixture(tmp_path, authorized=False), approved=True)


def test_current_client_never_creates_an_implicit_legacy_budget(tmp_path):
    path = tmp_path / "missing-budget.json"
    with pytest.raises(ValueError, match="existing cumulative ledger"):
        OriginalBudget(path, approved=True)
    assert not path.exists()


def test_current_guard_rejects_legacy_disabled_thinking_without_reserving(tmp_path):
    book = OriginalBudget(ledger_fixture(tmp_path), approved=True)
    with pytest.raises(ValueError, match="thinking"):
        book.reserve("human-v1-fixture-A-0", {**payload(), "thinking": {"type": "disabled"}})
    assert len(book.state["records"]) == 1


def test_client_inherits_project_options_in_actual_payload_without_network(tmp_path, monkeypatch):
    from types import SimpleNamespace

    import httpx

    from app.common import model_registry
    from scripts import chinatravel_original_client as module

    ledger_fixture(tmp_path).rename(tmp_path / "budget.json")  # Synthetic nonspendable ledger only.
    monkeypatch.setattr(module, "SOURCE", tmp_path)
    monkeypatch.setattr(
        model_registry,
        "binding",
        lambda *_: SimpleNamespace(
            base_url="https://api.deepseek.com",
            model="deepseek-flash",
            provider=SimpleNamespace(api_key="example-only-dummy-not-a-credential"),
        ),
    )
    observed = []

    def handle(request):
        observed.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "{}"}}], "usage": {"prompt_tokens": 10, "completion_tokens": 5}},
        )

    original_client = httpx.Client
    monkeypatch.setattr(
        module.httpx, "Client", lambda **kwargs: original_client(**kwargs, transport=httpx.MockTransport(handle))
    )
    client = module.OriginalClient("human-v1-fixture-A", output=tmp_path)
    assert client.complete("fixture", system_prompt="fixture") == "{}"
    assert observed[0]["thinking"] == {"type": "enabled"}
    assert observed[0]["reasoning_effort"] == "low"
    assert client.ledger.total() == Decimal("29.00006")


def test_four_process_reservations_and_settlements_do_not_lose_updates(tmp_path):
    path = ledger_fixture(tmp_path)
    code = """
import json,sys,time
from pathlib import Path
from scripts.chinatravel_original_client import OriginalBudget
p=Path(sys.argv[1]);index=sys.argv[2];book=OriginalBudget(p,approved=True)
payload={'model':'deepseek-flash','thinking':{'type':'enabled'},'reasoning_effort':'low','max_tokens':4096,
         'messages':[{'role':'user','content':'fixture'}]}
identifier='human-v1-fixture-A-'+index
book.reserve(identifier,payload)
(p.parent/('ready'+index)).write_text('ready')
deadline=time.monotonic()+8
while not (p.parent/'go').exists():
 if time.monotonic()>deadline:raise TimeoutError('barrier')
 time.sleep(.02)
book.settle(identifier,{'usage':{'prompt_tokens':10,'completion_tokens':5}})
"""
    children = [
        subprocess.Popen(
            [sys.executable, "-c", code, str(path), str(i)], stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
        for i in range(4)
    ]
    try:
        deadline = time.monotonic() + 8
        while len(list(tmp_path.glob("ready*"))) < 4 and time.monotonic() < deadline:
            time.sleep(0.02)
        assert len(list(tmp_path.glob("ready*"))) == 4  # No transaction lock is held across the simulated HTTP wait.
        records = json.loads(path.read_text("utf8"))["records"]
        assert len(records) == 5 and sum(r.get("request_state") == "in_flight" for r in records) == 4
        (tmp_path / "go").write_text("go")
        for child in children:
            _, error = child.communicate(timeout=10)
            assert child.returncode == 0, error.decode(errors="replace")
        book = OriginalBudget(path, approved=True)
        assert len(book.state["records"]) == 5
        assert len({r["id"] for r in book.state["records"]}) == 5
        assert book.total() == Decimal("29.00024")
        assert book.state["records"][0]["status"] == "issued_usage_unknown"
    finally:
        for child in children:
            if child.poll() is None:
                child.kill()
                child.wait()


def test_shared_export_rejects_internal_candidate_without_final_days():
    from app.agent.generation.content.complete_travel import project
    from app.schemas.travel_plan import TravelPlan
    from scripts.eval_chinatravel_original import shared_final

    with pytest.raises(ValueError, match="shared complete business"):
        shared_final([])
    plan = TravelPlan.model_validate(
        {
            "people_number": 2,
            "start_city": "上海",
            "target_city": "杭州",
            "itinerary": [
                {
                    "day": 1,
                    "activities": [
                        {
                            "type": "attraction",
                            "position": "synthetic fixture",
                            "start_time": "09:00",
                            "end_time": "10:00",
                            "price": 10,
                            "cost": 20,
                            "tickets": 2,
                            "transports": [],
                        }
                    ],
                }
            ],
        }
    )
    assert shared_final(project(plan)) == plan.official()
