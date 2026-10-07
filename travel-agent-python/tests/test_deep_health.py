"""深度健康探测 /api/agent/health/deep（审计 §3.2.6/§3.5.4 → P1-6）。

- 探针语义：MySQL SELECT 1 / Redis ping，任何异常→该项 down、整体 degraded，
  探活端点自身绝不 500（依赖挂掉时「报 down」正是它的本职）；
- ~10s 结果缓存：窗口内重复探活不重复真探（防拨测把依赖打爆）；
- 端点降级：依赖 down → HTTP 503 + status=degraded，信封 data 带逐依赖状态；
- 浅探 /health 与 Dockerfile/compose HEALTHCHECK 不动（报告拍板），顺带钉住
  /health 仍是静态响应。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.common import deep_health
from main import app as assembled_app


class _FailingRedisStub:
    """ping 即抛：模拟 Redis 真故障（区别于熔断窗）。"""

    def ping(self) -> bool:
        raise ConnectionError("redis gone")


@pytest.fixture(autouse=True)
def _reset_deep_health_cache():
    """结果缓存是模块级、窗口 10s 跨用例：前后各清一次，用例间不串味。"""
    deep_health.reset_for_tests()
    yield
    deep_health.reset_for_tests()


def test_probe_mysql_exception_reports_down(monkeypatch, caplog) -> None:
    """MySQL 连不上/查询异常：只记 warning、返回 False，不把异常抛给端点。"""

    class _BrokenSessionScope:
        def __enter__(self):
            raise OSError("can't connect to MySQL")

        def __exit__(self, *args):
            return False

    monkeypatch.setattr(deep_health, "session_scope", lambda: _BrokenSessionScope())
    with caplog.at_level("WARNING", logger="app.common.deep_health"):
        assert deep_health.probe_mysql() is False
    assert any("mysql probe failed" in r.getMessage() for r in caplog.records)


def test_probe_redis_reports_down_for_connection_failure_and_breaker(monkeypatch) -> None:
    """真故障（ping 抛）与熔断窗（client()=None）都如实报 down，且开熔断窗走既有降级。"""

    class _RedisModuleStub:
        def __init__(self) -> None:
            self.failures: list[BaseException] = []

        def client(self) -> object:
            return _FailingRedisStub()

        def note_failure(self, exc: BaseException) -> None:
            self.failures.append(exc)

    stub = _RedisModuleStub()
    monkeypatch.setattr(deep_health, "redis_client", stub)
    assert deep_health.probe_redis() is False
    assert len(stub.failures) == 1, "ping 失败要记 note_failure（开熔断窗，与其他调用方同款）"

    class _BreakerOpenStub:
        def client(self) -> None:
            return None  # 熔断窗内：共享客户端直接给 None

        def note_failure(self, exc: BaseException) -> None:  # pragma: no cover - 不应触达
            raise AssertionError("熔断窗内不应再 note_failure")

    monkeypatch.setattr(deep_health, "redis_client", _BreakerOpenStub())
    assert deep_health.probe_redis() is False


def test_snapshot_caches_result_within_ttl(monkeypatch) -> None:
    """缓存窗口内的第二次探活读缓存；reset 后重新真探。"""
    calls = {"mysql": 0, "redis": 0}
    monkeypatch.setattr(deep_health, "probe_mysql", lambda: (calls.__setitem__("mysql", calls["mysql"] + 1), True)[1])
    monkeypatch.setattr(deep_health, "probe_redis", lambda: (calls.__setitem__("redis", calls["redis"] + 1), True)[1])

    first = deep_health.snapshot()
    assert first == {"status": "up", "mysql": "up", "redis": "up"}
    second = deep_health.snapshot()
    assert second == first
    assert calls == {"mysql": 1, "redis": 1}, "10s 窗口内不重复真探"

    deep_health.reset_for_tests()
    deep_health.snapshot()
    assert calls == {"mysql": 2, "redis": 2}


def test_snapshot_returns_copy_not_cached_object(monkeypatch) -> None:
    """返回值是缓存的拷贝：调用方改返回值不得污染后续探活结论。"""
    monkeypatch.setattr(deep_health, "probe_mysql", lambda: True)
    monkeypatch.setattr(deep_health, "probe_redis", lambda: True)
    first = deep_health.snapshot()
    first["mysql"] = "tampered"
    assert deep_health.snapshot()["mysql"] == "up"


def test_endpoint_degraded_returns_503_with_per_dependency_state(monkeypatch) -> None:
    """依赖全挂：HTTP 503 + status=degraded，信封 data 逐依赖说明；响应带 request-id。"""
    monkeypatch.setattr(deep_health, "probe_mysql", lambda: False)
    monkeypatch.setattr(deep_health, "probe_redis", lambda: False)
    client = TestClient(assembled_app)
    response = client.get("/api/agent/health/deep")
    assert response.status_code == 503
    assert response.json() == {
        "code": 503,
        "message": "degraded",
        "data": {"status": "degraded", "mysql": "down", "redis": "down"},
    }
    assert response.headers.get("x-request-id"), "探活响应也应可按 request-id 归并"


def test_endpoint_healthy_returns_200(monkeypatch) -> None:
    monkeypatch.setattr(deep_health, "probe_mysql", lambda: True)
    monkeypatch.setattr(deep_health, "probe_redis", lambda: True)
    client = TestClient(assembled_app)
    response = client.get("/api/agent/health/deep")
    assert response.status_code == 200
    assert response.json()["data"] == {"status": "up", "mysql": "up", "redis": "up"}


def test_shallow_health_stays_static() -> None:
    """浅探保持静态 {status:up}：Dockerfile/compose HEALTHCHECK 的既有契约（报告拍板不动）。"""
    client = TestClient(assembled_app)
    response = client.get("/api/agent/health")
    assert response.status_code == 200
    assert response.json()["data"] == {"status": "up"}
