"""MySQL 连接池（pymysql）：避免每次查询新建 TCP 连接。

实现要点：
- 线程安全队列 + 惰性建连；空闲连接 ping 自愈；
- 上下文管理器 `connection()`：异常时 rollback 并丢弃坏连接；
- 池满时阻塞等待（默认 5s），超时抛错由仓储层吞掉返回空结果；
- Redis/MySQL 故障不影响「查失败返回空」的既有降级语义。

生产可替换为 DBUtils/SQLAlchemy Pool，对外保持 `connection()` 契约即可。
"""

from __future__ import annotations

import contextlib
import logging
import queue
import threading
from collections.abc import Iterator
from contextlib import contextmanager

import pymysql
from pymysql.connections import Connection

from app.common.config import settings

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_pool: queue.Queue[Connection] | None = None
_created = 0
_MAX_WAIT_SECONDS = 5.0


def _max_size() -> int:
    try:
        return max(1, int(settings.db_pool_max))
    except Exception:
        return 10


def _new_connection() -> Connection:
    return pymysql.connect(
        host=settings.db_host,
        port=settings.db_port,
        user=settings.db_user,
        password=settings.db_password,
        database=settings.db_name,
        charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor,
        connect_timeout=3,
        read_timeout=15,
        write_timeout=15,
        autocommit=False,
    )


def _ensure_pool() -> queue.Queue[Connection]:
    global _pool
    if _pool is None:
        with _lock:
            if _pool is None:
                _pool = queue.Queue(maxsize=_max_size())
    return _pool


def _discard(conn: Connection | None) -> None:
    global _created
    if conn is None:
        return
    with contextlib.suppress(Exception):
        conn.close()
    with _lock:
        _created = max(0, _created - 1)


def acquire() -> Connection:
    """从池取连接；必要时新建。失败向上抛，由调用方决定是否吞异常。"""
    global _created
    pool = _ensure_pool()
    while True:
        conn: Connection | None = None
        try:
            conn = pool.get_nowait()
        except queue.Empty:
            with _lock:
                if _created < _max_size():
                    _created += 1
                    try:
                        return _new_connection()
                    except Exception:
                        _created -= 1
                        raise
            try:
                conn = pool.get(timeout=_MAX_WAIT_SECONDS)
            except queue.Empty as exc:
                raise TimeoutError(f"MySQL connection pool exhausted (max={_max_size()})") from exc
        try:
            # 仅探测存活；断连则销毁并在下一轮新建（避免 deprecated reconnect）
            conn.ping()
            return conn
        except Exception as ex:
            logger.warning("pooled connection dead, recreating: %s", ex)
            _discard(conn)


def release(conn: Connection, *, broken: bool = False) -> None:
    """归还连接；broken=True 或连接已关闭则销毁。"""
    if conn is None:
        return
    if broken:
        _discard(conn)
        return
    try:
        if conn.get_autocommit():
            pass
        else:
            conn.rollback()
    except Exception:
        _discard(conn)
        return
    pool = _ensure_pool()
    try:
        pool.put_nowait(conn)
    except queue.Full:
        _discard(conn)


@contextmanager
def connection() -> Iterator[Connection]:
    """`with connection() as conn:` —— 正常归还，异常标记坏连接。"""
    conn = acquire()
    broken = False
    try:
        yield conn
    except Exception:
        broken = True
        raise
    finally:
        release(conn, broken=broken)


def close_all() -> None:
    """测试/进程退出：关闭池内全部连接。"""
    global _pool, _created
    with _lock:
        pool, _pool = _pool, None
        _created = 0
    if pool is None:
        return
    while True:
        try:
            conn = pool.get_nowait()
        except queue.Empty:
            break
        with contextlib.suppress(Exception):
            conn.close()


def pool_stats() -> dict:
    """可观测：当前已创建连接数与空闲数。"""
    pool = _pool
    return {
        "created": _created,
        "idle": pool.qsize() if pool is not None else 0,
        "max": _max_size(),
    }
