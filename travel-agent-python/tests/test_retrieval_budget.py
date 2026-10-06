"""检索预算计入 run_limits 的回归测试。"""

from app.agent.runtime.run_limits import RunLimitExceeded, RunLimits


def test_retrieval_budget_counts_and_raises():
    limits = RunLimits(max_retrievals=2)
    limits.record_retrieval(1)
    limits.record_retrieval(1)
    assert limits.retrievals == 2
    try:
        limits.record_retrieval(1)
        raised = False
    except RunLimitExceeded:
        raised = True
    assert raised


def test_retrieval_budget_disabled_when_zero():
    limits = RunLimits(max_retrievals=0)
    limits.record_retrieval(5)
    assert limits.retrievals == 5


def test_snapshot_includes_retrievals():
    s = RunLimits(max_retrievals=10).snapshot()
    assert s["retrievals"] == 0
    assert s["max_retrievals"] == 10
