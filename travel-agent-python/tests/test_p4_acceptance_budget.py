"""P4 付费前清单与预算边界；构造 HTTP request，不创建网络 client。"""

import json
from decimal import Decimal

import httpx
import pytest

from scripts.p4_acceptance_budget import HARD_BUDGET, AcceptanceBudget
from scripts.prepare_p4_acceptance import build_plan


@pytest.fixture
def plan(monkeypatch):
    from app.common.config import settings

    monkeypatch.setattr(settings, "llm_base_url", "https://api.deepseek.com")
    monkeypatch.setattr(settings, "llm_api_key", "example-only-p4")
    monkeypatch.setattr(settings, "llm_model", "deepseek-flash")
    for role in ("main", "fast", "search"):
        monkeypatch.setattr(settings, f"llm_role_{role}", "deepseek-flash")
    return build_plan()


def request(row):
    return httpx.Request("POST", f"https://api.deepseek.com{row['path']}", json=row["payload"])


def test_default_denies_every_paid_call_and_manifest_contains_no_execute(plan, tmp_path):
    guard = AcceptanceBudget(plan, tmp_path / "ledger.json")
    with pytest.raises(ValueError, match="授权"):
        guard.before(request(plan["requests"][0]))
    assert not guard.records and not guard.path.exists()
    assert not plan["paid_authorized"] and plan["mode"] == "preparation_only"
    assert "example-only-p4" not in json.dumps(plan)


def test_all_worst_context_reservations_fit_and_failures_cannot_be_retried(plan, tmp_path):
    guard = AcceptanceBudget(plan, tmp_path / "ledger.json", approved=True)
    for row in plan["requests"]:
        guard.before(request(row))
    assert guard.reserved == Decimal("4.238992") < HARD_BUDGET
    assert len(guard.records) == 4
    assert all(r["status"] == "issued_usage_unknown" for r in guard.records)
    with pytest.raises(ValueError):
        guard.before(request(plan["requests"][0]))
    assert guard.reserved == Decimal("4.238992") and len(guard.records) == 4
    with pytest.raises(ValueError, match="已存在"):
        AcceptanceBudget(plan, guard.path, approved=True)


@pytest.mark.parametrize("change", ["model", "thinking", "output", "tools", "endpoint", "query"])
def test_changed_payload_or_unapproved_endpoint_is_rejected_before_issue(plan, tmp_path, change):
    row = json.loads(json.dumps(plan["requests"][0 if change != "query" else 2]))
    if change == "model":
        row["payload"]["model"] = "deepseek-v4-pro"
    elif change == "thinking":
        row["payload"]["thinking"]["type"] = "enabled"
    elif change == "output":
        row["payload"]["max_tokens"] += 1
    elif change == "tools":
        row["payload"]["tools"] = [{"type": "web_search"}]
    elif change == "query":
        row["payload"]["query"] += " unreviewed"
    req = (
        request(row)
        if change != "endpoint"
        else httpx.Request("POST", "https://example.com/chat/completions", json=row["payload"])
    )
    guard = AcceptanceBudget(plan, tmp_path / "ledger.json", approved=True)
    with pytest.raises(ValueError):
        guard.before(req)
    assert not guard.records and not guard.path.exists()


def test_existing_llm_transport_is_guarded_before_mock_network_and_has_no_retry(plan, tmp_path, monkeypatch):
    from app.common import llm_client
    from app.common.config import settings

    monkeypatch.setattr(settings, "llm_enable_thinking", False)
    monkeypatch.setattr(settings, "trial_billing_enabled", False)
    monkeypatch.setattr(llm_client, "_record_usage", lambda *_a, **_kw: None)
    issued = []

    def fail(request):
        issued.append(request.url.path)
        return httpx.Response(502, json={"error": "frozen failure, no actual network"})

    guard = AcceptanceBudget(plan, tmp_path / "ledger.json", approved=True)
    with httpx.Client(
        transport=httpx.MockTransport(fail), follow_redirects=False, event_hooks={"request": [guard.before]}
    ) as http:
        client = llm_client.LLMClient(
            base_url="https://api.deepseek.com",
            api_key="example-only-p4",
            model="deepseek-flash",
            max_attempts=1,
            follow_redirects=False,
        )
        monkeypatch.setattr(client, "_http", lambda: http)
        payload = plan["requests"][0]["payload"]

        def call():
            return client.chat_response(
                payload["messages"],
                temperature=payload["temperature"],
                max_tokens=payload["max_tokens"],
                response_format=payload["response_format"],
            )

        with pytest.raises(httpx.HTTPStatusError):
            call()
        assert len(issued) == 1 and len(guard.records) == 1
        with pytest.raises(ValueError, match="修复/重试"):
            call()
        assert len(issued) == 1  # 重复尝试在 MockTransport 之前即被拒绝。


def test_search_adapter_is_guarded_without_model_or_real_network(plan, tmp_path, monkeypatch):
    from app.agent.data import deepseek_search
    from app.common.model_registry import Provider, RoleBinding

    issued = []

    def respond(request):
        issued.append(request.url.path)
        return httpx.Response(200, json={"results": []})

    guard = AcceptanceBudget(plan, tmp_path / "ledger.json", approved=True)
    bound = RoleBinding(
        "search",
        Provider("default", "https://api.deepseek.com", "example-only-p4", frozenset({"web_search_api"})),
        "deepseek-flash",
    )
    with httpx.Client(
        transport=httpx.MockTransport(respond), follow_redirects=False, event_hooks={"request": [guard.before]}
    ) as http:
        monkeypatch.setattr(deepseek_search, "api_client", lambda: http)
        assert deepseek_search.search_pages(plan["requests"][2]["payload"]["query"], bound) == []
    assert issued == ["/v1/tools/search"] and guard.reserved == Decimal("0.005")
