"""db_pool 单测：池复用、归还、耗尽与 close_all。"""

from unittest.mock import MagicMock

import pytest

from app.common import db_pool


@pytest.fixture(autouse=True)
def _reset_pool():
    db_pool.close_all()
    yield
    db_pool.close_all()


def _fake_conn():
    conn = MagicMock()
    conn.ping.return_value = None
    return conn


def test_acquire_creates_then_reuses(monkeypatch):
    monkeypatch.setattr(db_pool.settings, "db_pool_max", 2)
    created = []

    def factory():
        c = _fake_conn()
        created.append(c)
        return c

    monkeypatch.setattr(db_pool, "_new_connection", factory)
    c1 = db_pool.acquire()
    db_pool.release(c1)
    c2 = db_pool.acquire()
    assert c1 is c2
    assert len(created) == 1
    stats = db_pool.pool_stats()
    assert stats["max"] == 2
    assert stats["created"] == 1


def test_connection_context_returns_on_success(monkeypatch):
    monkeypatch.setattr(db_pool.settings, "db_pool_max", 2)
    conn = _fake_conn()
    monkeypatch.setattr(db_pool, "_new_connection", lambda: conn)
    with db_pool.connection() as c:
        assert c is conn
    # 归还后仍可再取到同一连接
    with db_pool.connection() as c2:
        assert c2 is conn


def test_connection_context_discards_on_error(monkeypatch):
    monkeypatch.setattr(db_pool.settings, "db_pool_max", 2)
    first = _fake_conn()
    second = _fake_conn()
    pool = [first, second]

    def factory():
        return pool.pop(0)

    monkeypatch.setattr(db_pool, "_new_connection", factory)
    with pytest.raises(RuntimeError):
        with db_pool.connection():
            raise RuntimeError("boom")
    # 坏连接被销毁，created 减 1
    assert db_pool.pool_stats()["created"] == 0
    with db_pool.connection() as c:
        assert c is second


def test_pool_exhaustion_raises_timeout(monkeypatch):
    monkeypatch.setattr(db_pool.settings, "db_pool_max", 1)
    monkeypatch.setattr(db_pool, "_MAX_WAIT_SECONDS", 0.05)
    conn = _fake_conn()
    monkeypatch.setattr(db_pool, "_new_connection", lambda: conn)
    held = db_pool.acquire()
    with pytest.raises(TimeoutError):
        db_pool.acquire()
    db_pool.release(held)


def test_dead_connection_replaced(monkeypatch):
    monkeypatch.setattr(db_pool.settings, "db_pool_max", 2)
    dead = _fake_conn()
    dead.ping.side_effect = Exception("gone")
    fresh = _fake_conn()
    pool = [fresh]

    monkeypatch.setattr(db_pool, "_new_connection", lambda: pool.pop(0) if pool else _fake_conn())
    # 手动塞进一个死连接
    db_pool._ensure_pool().put(dead)
    with db_pool.connection() as c:
        assert c is not dead
        assert c is fresh
