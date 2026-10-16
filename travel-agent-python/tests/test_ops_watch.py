"""运维告警通道与巡检（T8b）的钉。

只钉"会发/不重复发/不发就不碰网络"这三件事：
真实送达需要配一个真目标并演一次，那是 T9 的事，不在离线用例里假称通过。
"""

from __future__ import annotations

import time

import pytest

from app.common import alerting, llm_breaker, ops_watch


class _Recorder:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def client(self):
        outer = self

        class _Client:
            @staticmethod
            def post(url, json=None, timeout=None):
                outer.calls.append({"url": url, "payload": json, "timeout": timeout})

                class _Resp:
                    status_code = 202

                return _Resp()

        return _Client()

    @property
    def events(self) -> list[str]:
        return [str(call["payload"].get("event")) for call in self.calls]


@pytest.fixture(autouse=True)
def _clean(monkeypatch, tmp_path):
    alerting.reset_state()
    ops_watch.reset_state()
    yield
    alerting.reset_state()
    ops_watch.reset_state()


def _armed(monkeypatch, recorder: _Recorder) -> None:
    monkeypatch.setattr(alerting, "webhook", lambda: "https://alerts.example/hook")
    monkeypatch.setattr(alerting, "api_client", recorder.client)


def test_no_configured_target_never_touches_the_network(monkeypatch) -> None:
    recorder = _Recorder()
    monkeypatch.setattr(alerting, "webhook", lambda: "")
    monkeypatch.setattr(alerting, "api_client", recorder.client)

    assert alerting.send("ops.anything", {"a": 1}) is False
    assert recorder.calls == []


def test_legacy_llm_key_still_works_as_the_same_single_channel(monkeypatch) -> None:
    """两个键是同一出口的兼容别名，不是两条通道。"""
    recorder = _Recorder()
    monkeypatch.setattr(alerting.settings, "alert_webhook_url", "", raising=False)
    monkeypatch.setattr(alerting.settings, "llm_alert_webhook_url", "https://legacy.example/hook")
    monkeypatch.setattr(alerting, "api_client", recorder.client)

    assert alerting.webhook() == "https://legacy.example/hook"
    assert alerting.send("llm.error_rate", {"channel": "x"}, cooldown_seconds=0.0) is True
    assert recorder.calls[0]["url"] == "https://legacy.example/hook"

    # 配了新键就以新键为准：回落只发生在没配新键时，两者不会同时成为出口
    monkeypatch.setattr(alerting.settings, "alert_webhook_url", "https://new.example/hook", raising=False)
    assert alerting.webhook() == "https://new.example/hook"


def test_cooldown_is_per_event_and_per_scope(monkeypatch) -> None:
    recorder = _Recorder()
    _armed(monkeypatch, recorder)

    assert alerting.send("ops.disk_low", {"free_mb": 1}, scope="data", cooldown_seconds=3600) is True
    assert alerting.send("ops.disk_low", {"free_mb": 1}, scope="data", cooldown_seconds=3600) is False
    assert alerting.send("ops.disk_low", {"free_mb": 1}, scope="backup", cooldown_seconds=3600) is True
    assert alerting.send("ops.backup_stale", {}, scope="backup", cooldown_seconds=3600) is True
    assert len(recorder.calls) == 3


def test_breaker_alerts_go_through_the_one_channel(monkeypatch) -> None:
    """`llm_breaker` 不再自带发送器：它只是 alerting 的一个调用方。"""
    recorder = _Recorder()
    _armed(monkeypatch, recorder)
    assert not hasattr(llm_breaker, "_alert_last_sent"), "旧的第二套冷却表应已移除"

    llm_breaker._fire_alert("circuit_open", "https://gw.example", {"cooldown_seconds": 30})
    assert recorder.events == ["llm.circuit_open"]
    assert recorder.calls[0]["payload"]["scope"] == "https://gw.example"


def test_disk_check_is_edge_triggered(monkeypatch, tmp_path) -> None:
    recorder = _Recorder()
    _armed(monkeypatch, recorder)
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    monkeypatch.setattr(ops_watch.settings, "export_dir", str(data_dir))

    class _Usage:
        def __init__(self, free: int) -> None:
            self.free = free

    free_bytes = 100 * 1_048_576
    monkeypatch.setattr(ops_watch.shutil, "disk_usage", lambda _p: _Usage(free_bytes))
    monkeypatch.setattr(ops_watch.settings, "alert_disk_min_free_mb", 50, raising=False)
    ops_watch.check_disk()
    assert recorder.events == [], "高于阈值不发"

    monkeypatch.setattr(ops_watch.settings, "alert_disk_min_free_mb", 200, raising=False)
    ops_watch.check_disk()
    ops_watch.check_disk()
    assert recorder.events == ["ops.disk_low"], "越线只报一次（边沿触发）"

    monkeypatch.setattr(ops_watch.settings, "alert_disk_min_free_mb", 50, raising=False)
    ops_watch.check_disk()
    assert recorder.events == ["ops.disk_low", "ops.disk_recovered"]


def test_backup_age_check_skips_when_unconfigured_and_alerts_when_stale(monkeypatch, tmp_path) -> None:
    recorder = _Recorder()
    _armed(monkeypatch, recorder)
    monkeypatch.setattr(ops_watch.settings, "alert_backup_dir", "", raising=False)
    monkeypatch.setattr(ops_watch.settings, "alert_backup_max_age_hours", 24, raising=False)
    ops_watch.check_backups()
    assert recorder.events == [], "没配备份目录就如实跳过，不假称通过"

    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    old = backup_dir / "night.sql"
    old.write_bytes(b"--")
    ancient = time.time() - 100 * 3600
    import os

    os.utime(old, (ancient, ancient))
    monkeypatch.setattr(ops_watch.settings, "alert_backup_dir", str(backup_dir), raising=False)
    ops_watch.check_backups()
    assert recorder.events == ["ops.backup_stale"]

    fresh = backup_dir / "today.sql"
    fresh.write_bytes(b"--")
    ops_watch.check_backups()
    assert recorder.events == ["ops.backup_stale", "ops.backup_recovered"]


def test_dependency_check_reuses_the_probe_and_reports_both_edges(monkeypatch) -> None:
    recorder = _Recorder()
    _armed(monkeypatch, recorder)
    monkeypatch.setattr(ops_watch, "snapshot", lambda: {"status": "degraded", "mysql": "down", "redis": "up"})
    ops_watch.check_dependencies()
    ops_watch.check_dependencies()
    assert recorder.events == ["ops.dependencies_down"]
    assert recorder.calls[0]["payload"]["detail"]["down"] == ["mysql"]

    monkeypatch.setattr(ops_watch, "snapshot", lambda: {"status": "ok", "mysql": "up", "redis": "up"})
    ops_watch.check_dependencies()
    assert recorder.events == ["ops.dependencies_down", "ops.dependencies_recovered"]


def test_run_once_swallows_a_broken_check(monkeypatch) -> None:
    """观测面异常不得冒进 cron 循环：一个检查坏了，其它检查照跑。"""
    recorder = _Recorder()
    _armed(monkeypatch, recorder)

    def boom() -> None:
        raise RuntimeError("probe exploded")

    monkeypatch.setattr(ops_watch, "check_disk", boom)
    monkeypatch.setattr(ops_watch, "snapshot", lambda: {"status": "degraded", "mysql": "down", "redis": "up"})
    ops_watch.run_once()
    assert recorder.events == ["ops.dependencies_down"]
