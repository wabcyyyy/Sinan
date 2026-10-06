"""LLM 重试退避抖动单测（L1 补充 3：注释是意图，代码向注释对齐）。

抖动带回无法逐值断言，验收口径 = 区间钳制 + 随机分量存在：
- 区间：min(2^attempt × 0.3, 2.0) × [0.8, 1.2]；
- 随机分量：同 attempt 大样本采样必有多值（无抖动时恒等于基数）。
"""

from __future__ import annotations

import random

import httpx
import pytest

import app.agent  # noqa: F401  先完成 agent 门面导入：存量导入环 llm_client→facade→web_search→llm_client（pyproject importlinter 豁免条目注明的既有债）。
from app.common import llm_client


def _base(attempt: int) -> float:
    return min(2.0**attempt * 0.3, 2.0)


def test_backoff_delay_stays_within_jitter_band():
    for attempt in range(5):
        base = _base(attempt)
        values = [llm_client._backoff_delay(attempt) for _ in range(200)]
        assert all(base * 0.8 <= v <= base * 1.2 for v in values), f"attempt={attempt} 的退避必须落在 ±20% 抖动带内"


def test_backoff_has_random_component():
    values = {round(llm_client._backoff_delay(2), 6) for _ in range(50)}
    assert len(values) > 1, "退避序列必须有随机分量（同步网关的瞬时过载会被整齐的重试波放大）"


def test_backoff_fixed_seed_is_reproducible():
    """固定种子断言区间与取值（L1 验收字面口径）：种子 → uniform 抽样可复现。"""
    random.seed(42)
    first = llm_client._backoff_delay(2)
    random.seed(42)
    second = llm_client._backoff_delay(2)
    assert first == second, "同一固定种子下退避序列必须可复现"
    expected = min(2.0**2 * 0.3, 2.0) * random.Random(42).uniform(0.8, 1.2)
    assert first == pytest.approx(expected)


def test_retry_sleep_sleeps_jittered_delay(monkeypatch):
    sleeps: list[float] = []
    monkeypatch.setattr(llm_client.time, "sleep", sleeps.append)
    llm_client._retry_sleep(1)
    assert len(sleeps) == 1
    base = _base(1)
    assert base * 0.8 <= sleeps[0] <= base * 1.2


def test_describe_error_keeps_type_name_when_str_empty():
    """API-1：httpx.ConnectTimeout 的 str() 是空串——只记它会让日志与 usage 库在
    通道级故障时只剩空消息/unknown（2026-10-05 夜审：622 次失败全落 'unknown'），
    可排障错误串必须带异常类型名。"""
    assert llm_client._describe_error(httpx.ConnectTimeout("")) == "ConnectTimeout"
    assert llm_client._describe_error(httpx.ReadTimeout("timed out")) == "ReadTimeout: timed out"
    assert llm_client._describe_error(ValueError("boom")) == "ValueError: boom"


def test_probe_knobs_single_attempt_short_read_no_redirects(monkeypatch):
    """BYOK 连通性探测的紧约束（2026-10-02 终审）：不重试 + 短读超时 + 不跟随重定向。

    黑洞地址 × 默认 240s 读超时 ×2 次尝试会把同步 /test 端点的线程占用放大到
    ~480s；这里钉死三根旋钮真的传到了 httpx 层。
    """
    calls: list[dict] = []

    class _Client:
        def post(self, _url, **kwargs):
            calls.append(kwargs)
            raise httpx.ConnectTimeout("black-hole")

        def is_closed(self):
            return False

    monkeypatch.setattr(llm_client, "_get_http_client", lambda: _Client())
    probe = llm_client.LLMClient(timeout=15.0, max_attempts=1, follow_redirects=False)
    with pytest.raises(httpx.ConnectTimeout):
        probe.chat_response([{"role": "user", "content": "ping"}])
    assert len(calls) == 1, "max_attempts=1 时超时不得重试"
    assert calls[0]["follow_redirects"] is False
    assert calls[0]["timeout"].read == 15.0
    assert calls[0]["timeout"].connect == llm_client.settings.llm_connect_timeout
