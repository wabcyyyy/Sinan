import json
from decimal import Decimal

import httpx
import pytest

from scripts.online_closeout_budget import OnlineBudget


def setup_budget(tmp_path, carried="0.450392"):
    prior = tmp_path / "prior.json"
    prior.write_text(json.dumps({"total_peak_upper_cny": carried}), encoding="utf-8")
    return OnlineBudget(tmp_path / "run", prior, approved=True), prior


def request(text):
    return httpx.Request(
        "POST",
        "https://api.deepseek.com/chat/completions",
        json={
            "model": "deepseek-flash",
            "thinking": {"type": "disabled"},
            "max_tokens": 4096,
            "messages": [{"role": "user", "content": text}],
        },
    )


def test_requires_new_approval_and_enforces_cumulative_cap_before_send(tmp_path):
    with pytest.raises(ValueError, match="未授权"):
        OnlineBudget(tmp_path, tmp_path / "absent.json")
    budget, _ = setup_budget(tmp_path, "13")
    with pytest.raises(ValueError, match="超过14元"):
        budget.before(request("anonymous"))
    assert budget.state["records"] == []


def test_unknown_usage_and_counters_survive_restart_without_retry(tmp_path):
    budget, prior = setup_budget(tmp_path)
    budget.before(request("anonymous-1"))
    reserved = budget.total()
    restored = OnlineBudget(budget.output, prior, approved=True)
    assert restored.total() == reserved > Decimal("0.450392")
    with pytest.raises(ValueError, match="重复请求"):
        restored.before(request("anonymous-1"))
    for index in range(2, 7):
        restored.before(request(f"anonymous-{index}"))
    with pytest.raises(ValueError, match="次数已用完"):
        restored.before(request("anonymous-7"))


def test_settlement_does_not_expand_call_limits_or_rewrite_history(tmp_path):
    budget, prior = setup_budget(tmp_path)
    req = request("anonymous")
    identifier = budget.before(req)
    budget.after(
        identifier, httpx.Response(200, json={"usage": {"prompt_tokens": 100, "completion_tokens": 20}}, request=req)
    )
    assert budget.total() == Decimal("0.450752")
    assert json.loads(prior.read_text("utf-8"))["total_peak_upper_cny"] == "0.450392"
    assert len(budget.state["records"]) == 1


def test_unapproved_hosts_and_non_anonymous_place_queries_are_blocked(tmp_path):
    budget, _ = setup_budget(tmp_path)
    for url in ("https://example.com", "https://nominatim.openstreetmap.org/search?q=private+address"):
        with pytest.raises(ValueError):
            budget.before(httpx.Request("GET", url))
    assert not budget.state["records"]


def test_named_official_closure_is_bound_without_claiming_other_days_or_prices():
    from scripts.serve_online_closeout import observed_louvre_closure

    row = {
        "url": "https://musee.louvre.fr/zh-hans/canguanfuwu/",
        "retrieved_at": "2026-10-25T06:55:00Z",
        "content": "#### 卢浮宫博物馆\n闭馆周二\n### 门票价格\n其他信息",
    }
    fact = observed_louvre_closure([row])
    assert fact is not None and fact.opening_hours is not None
    assert fact.opening_hours.rules[0].closed and fact.opening_hours.rules[0].weekdays == [1]
    assert fact.excerpt == "闭馆周二"
    assert observed_louvre_closure([{**row, "url": "https://example.com/mirror"}]) is None
    assert observed_louvre_closure([{**row, "content": "其他馆区：闭馆周二"}]) is None
