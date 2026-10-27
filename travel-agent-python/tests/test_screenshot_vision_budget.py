import json

import httpx
import pytest

from scripts.eval_screenshot_vision import MAX_OUTPUT, BudgetGuard, cost_upper


def payload(text):
    return {
        "model": "deepseek-flash",
        "max_tokens": MAX_OUTPUT,
        "thinking": {"type": "disabled"},
        "messages": [{"role": "user", "content": text}],
    }


def request(body, url="https://api.deepseek.com/chat/completions"):
    return httpx.Request("POST", url, json=body)


def test_reserves_before_request_and_denies_retry_or_fifth_call(tmp_path):
    allowed = [payload(str(i)) for i in range(4)]
    path = tmp_path / "result.json"
    guard = BudgetGuard(allowed, {"output_path": str(path)})
    for body in allowed:
        guard.before(request(body))
        assert len(json.loads(path.read_text(encoding="utf-8"))["calls"]) == len(guard.records)
    assert guard.reserved < 1
    with pytest.raises(ValueError):
        guard.before(request(allowed[0]))
    assert len(guard.records) == 4


def test_unaffordable_request_never_consumes_or_sends(tmp_path):
    body = payload("x" * 500_000)
    guard = BudgetGuard([body], {"output_path": str(tmp_path / "result.json")})
    with pytest.raises(ValueError, match="budget"):
        guard.before(request(body))
    assert not guard.records and guard.reserved == 0
    assert cost_upper(500_000) > 1


def test_rejects_unpriced_endpoint_changed_payload_and_larger_output(tmp_path):
    body = payload("test")
    guard = BudgetGuard([body], {"output_path": str(tmp_path / "result.json")})
    with pytest.raises(ValueError):
        guard.before(request(body, "https://example.com/chat/completions"))
    with pytest.raises(ValueError):
        guard.before(request({**body, "max_tokens": MAX_OUTPUT + 1}))
    with pytest.raises(ValueError):
        guard.before(request(payload("changed image or prompt")))
    assert not guard.records
