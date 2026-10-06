"""可靠性单测共享的假钟（L1）。

同时替换 `time.monotonic` 与 `time.sleep`：sleep 不真等、直接推进时钟——
重试退避、车道节流预约槽、熔断冷却窗的用例 thus 毫秒级跑完且确定性。
`app.common.external_client` 与被测代码共用同一个全局 time 模块，
monkeypatch 一次两边生效（与 test_external_client 既有 monkeypatch 手法一致）。
"""

from __future__ import annotations

from itertools import pairwise

import pytest


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture()
def fake_clock(monkeypatch: pytest.MonkeyPatch) -> FakeClock:
    import time as time_module

    clock = FakeClock()
    monkeypatch.setattr(time_module, "monotonic", clock.monotonic)
    monkeypatch.setattr(time_module, "sleep", clock.sleep)
    return clock


def assert_min_spacing(stamps: list[float], interval: float, tolerance: float = 0.05) -> None:
    """断言相邻时间戳间隔 ≥ interval - tolerance（真实时钟并发测试的容差口径）。"""
    ordered = sorted(stamps)
    for earlier, later in pairwise(ordered):
        gap = later - earlier
        assert gap >= interval - tolerance, f"相邻外呼间隔 {gap:.3f}s 低于下限 {interval:.3f}s"


__all__: list[str] = ["FakeClock", "assert_min_spacing"]
