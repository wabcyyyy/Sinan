import pytest

from app.agent.runtime.run_limits import RunLimitExceeded, RunLimits


def test_run_limits_enforce_llm_and_token_budgets():
    limits = RunLimits(deadline_seconds=60, max_llm_calls=1, max_tokens=10)
    limits.check("llm")
    limits.record_llm(5, 5)
    with pytest.raises(RunLimitExceeded, match="模型调用"):
        limits.check("llm")

    limits = RunLimits(deadline_seconds=60, max_llm_calls=2, max_tokens=10)
    with pytest.raises(RunLimitExceeded, match="Token"):
        limits.record_llm(8, 3)


def test_run_limits_detect_no_progress():
    limits = RunLimits(deadline_seconds=60, max_llm_calls=2, max_tokens=10, max_replans=3)
    limits.record_replan(False)
    with pytest.raises(RunLimitExceeded, match="无进展"):
        limits.record_replan(False)


def test_research_lane_is_its_own_bucket():
    """研究额度只管研究：用完只停补池，不牵连检索与模型两道（生成与落地要用它们）。

    这道 lane 存在的理由就是"研究不能把生成饿死"，所以隔离性本身就是被测行为。
    """
    limits = RunLimits(deadline_seconds=60, max_research_calls=2, max_retrievals=48, max_llm_calls=32)
    limits.check("research")
    limits.record_research()
    limits.check("research")
    limits.record_research()
    with pytest.raises(RunLimitExceeded, match="研究额度"):
        limits.check("research")
    limits.check("retrieval")
    limits.check("llm")
    assert limits.snapshot()["research_calls"] == 2

    unlimited = RunLimits(deadline_seconds=60, max_research_calls=0)
    for _ in range(50):
        unlimited.record_research()
