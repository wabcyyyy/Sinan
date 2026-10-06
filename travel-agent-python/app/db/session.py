"""业务层数据库会话：引擎、Session 工厂，以及逻辑删除的全局作用域。

逻辑删除（PLAN v3.0 §1.2）：Java 侧靠 MyBatis-Plus @TableLogic 自动给每条查询
追加 `deleted = 0`；SQLAlchemy 没有这个默认行为，因此这里用 Session 级
`do_orm_execute` 钩子实现同等强度的全局过滤——**不依赖调用点自觉写条件**。
需要看全部行（管理端、回收任务、契约测试）时显式
`.execution_options(include_deleted=True)`。

只管 SELECT：UPDATE/DELETE 不静默改写，写路径的归属与软删条件必须显式出现在语句里，
否则「按 id 改」这类语句的影响面会变得不可见。
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker, with_loader_criteria

from app.common.config import settings
from app.db.models import Base, SoftDelete

# 只有混入 SoftDelete 的表参与全局过滤（sys_user / itinerary_main / itinerary_day /
# itinerary_item / budget_detail / export_task）；其余表本就没有 deleted 列。
_SOFT_DELETE_CLASSES: tuple[type, ...] = tuple(
    mapper.class_ for mapper in Base.registry.mappers if issubclass(mapper.class_, SoftDelete)
)


def database_url() -> str:
    """与 Java 侧同库；注意环境变量名不同源：Java 读 MYSQL_*，本服务读 DB_*。"""
    return (
        f"mysql+pymysql://{settings.db_user}:{settings.db_password}"
        f"@{settings.db_host}:{settings.db_port}/{settings.db_name}?charset=utf8mb4"
    )


_engine: Engine | None = None
_session_factory: sessionmaker[Session] | None = None


def get_engine() -> Engine:
    global _engine
    if _engine is None:
        _engine = create_engine(
            database_url(),
            # socket 超时与 app/common/db_pool.py 的 raw 池同口径（connect 3s / read/write 15s）。
            # pymysql 默认 read_timeout=None：对端无声死掉的半开连接会让任何一条查询
            # 永久挂死——生成编排任务曾因此卡在 session_scope 上，行程永远停在 status=1
            # 无人收尾（2026-10-01 金路径 E2E 实证）。
            connect_args={"connect_timeout": 3, "read_timeout": 15, "write_timeout": 15},
            pool_pre_ping=True,
            pool_size=settings.db_pool_max,
            max_overflow=0,
        )
    return _engine


def init_engine(engine: Engine | None = None, session_factory: sessionmaker[Session] | None = None) -> None:
    """测试注入点：换 SQLite 内存库时不必碰环境变量。"""
    global _engine, _session_factory
    _engine = engine
    _session_factory = session_factory or (
        sessionmaker(bind=engine, expire_on_commit=False) if engine is not None else None
    )


def get_session_factory() -> sessionmaker[Session]:
    global _session_factory
    if _session_factory is None:
        _session_factory = sessionmaker(bind=get_engine(), expire_on_commit=False)
    return _session_factory


# 这里**没有** `Depends(get_session)`：同步生成器依赖的进入与退出会被 anyio 派到不同
# worker 线程的上下文副本，`session_scope` 的 ContextVar 传播在那条路上不成立（退出还会
# 报 "Token was created in a different Context"）。事务边界统一由服务层的
# `session_scope()` 自己开，等价于 Spring 把 @Transactional 放在 service 而不是 controller。

_active_session: ContextVar[Session | None] = ContextVar("ta_active_session", default=None)


@contextmanager
def session_scope() -> Iterator[Session]:
    """事务边界：嵌套调用复用同一 Session，只有最外层提交/回滚（等价 Spring REQUIRED）。

    没有这层传播时，每个服务函数各开一条 Session、各自提交，一次业务写操作就会在
    "快照已落库、明细写失败"处断成半成品——行程写路径必须要原子性。
    """
    outer = _active_session.get()
    if outer is not None:
        yield outer
        return
    session = get_session_factory()()
    token = _active_session.set(session)
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        _active_session.reset(token)
        session.close()


def _apply_soft_delete_scope(execute_state) -> None:
    if not execute_state.is_select or not _SOFT_DELETE_CLASSES:
        return
    if execute_state.execution_options.get("include_deleted", False):
        return
    execute_state.statement = execute_state.statement.options(
        *[
            with_loader_criteria(cls, lambda obj: obj.deleted == False, include_aliases=True)  # noqa: E712
            for cls in _SOFT_DELETE_CLASSES
        ]
    )


event.listen(Session, "do_orm_execute", _apply_soft_delete_scope)
