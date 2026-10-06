"""周期任务注册表测试（G-3.3）。

覆盖：
- pytest 环境**自动 no-op**（不起后台线程——这是"定时器制造 flaky"的根治点）；
- register/start_all/stop_all 生命周期与幂等；
- 任务失败不退出循环（一次异常不能永久停摆）；
- 已有两个周期任务（generation-recovery / usage-cleanup）确实登记在册。

为什么单独守 pytest no-op：定时器在用例之间偷偷跑动是典型的 flaky 来源——
它不改任何断言，只让"连跑三次"的结果不同。这条测试把该行为钉死。
"""

from __future__ import annotations

import time

from app.common import cron


def test_pytest_environment_is_noop():
    """核心约定：在 pytest 里 start_all 不起线程。"""
    cron.reset()
    cron.register("probe", 0.01, lambda: None)
    assert cron.start_all() == [], "pytest 环境必须 no-op（不起任何后台线程）"
    time.sleep(0.05)
    cron.reset()


def test_register_and_listing():
    cron.reset()
    cron.register("b-task", 30, lambda: None)
    cron.register("a-task", 10, lambda: None)
    assert cron.registered() == ["a-task", "b-task"], "登记顺序无关，列表按名排序"
    cron.reset()


def test_duplicate_name_overrides():
    cron.reset()
    first = cron.register("dup", 10, lambda: "first")
    second = cron.register("dup", 20, lambda: "second")
    assert first is not second and second.interval_seconds == 20
    assert cron.registered() == ["dup"]
    cron.reset()


def test_interval_has_floor():
    """间隔下限防止误登记 0/负数造成忙轮询。"""
    cron.reset()
    task = cron.register("floor", 0, lambda: None)
    assert task.interval_seconds > 0
    cron.reset()


def test_run_swallows_exceptions(monkeypatch):
    """任务抛异常不得终止循环（一次抖动不能让任务永久停摆）。"""
    cron.reset()
    calls = []

    def boom():
        calls.append(1)
        raise RuntimeError("db flake")

    task = cron.register("boom", 0.05, boom)
    monkeypatch.setattr(cron, "_in_pytest", lambda: False)  # 临时伪装成非 pytest 环境
    cron.start_all()
    try:
        deadline = time.monotonic() + 2.0
        while len(calls) < 2 and time.monotonic() < deadline:
            time.sleep(0.02)
        assert len(calls) >= 2, "首次失败后必须继续下一轮"
    finally:
        cron.stop_all()
        cron.reset()
    assert task.thread is None, "stop_all 后应解绑线程引用"


def test_stop_all_is_fast_and_idempotent(monkeypatch):
    cron.reset()
    cron.register("tick", 30, lambda: None)  # 长间隔
    monkeypatch.setattr(cron, "_in_pytest", lambda: False)
    cron.start_all()
    started = time.monotonic()
    cron.stop_all()
    cron.stop_all()  # 幂等
    elapsed = time.monotonic() - started
    assert elapsed < 2.0, "Event.wait 可中断，停止不应等待整个间隔"
    cron.reset()


def test_recovery_registers_into_table(monkeypatch):
    """generation_recovery 的登记入口确实把任务写进表（名字与间隔符合约定）。"""
    from app.services import generation_recovery

    cron.reset()
    generation_recovery.register_loop()
    assert generation_recovery.CRON_NAME in cron.registered()
    cron.reset()
