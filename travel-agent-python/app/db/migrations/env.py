"""Alembic 运行环境。

设计取舍：**不使用 autogenerate**。schema 真相源是 `db/migration/V*.sql`（Flyway
时代的文件，双跑期由 `app.db.schema_source` 定位到同一份文件），revision 只做
「按版本序执行这些 SQL」。若允许 autogenerate 反向生成迁移，就会出现两份互相
漂移的 schema 定义——那正是本次后端迁移要消灭的问题。

已用 Flyway 建过表的既有库不要 upgrade，用 `alembic stamp` 打点，避免重复执行 DDL。
"""

from __future__ import annotations

import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context

# 允许 `alembic` 从任意 cwd 启动：把服务根目录加入 sys.path
SERVICE_ROOT = Path(__file__).resolve().parents[3]
if str(SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(SERVICE_ROOT))

from app.db.schema_source import SQL_MIGRATION_DIR  # noqa: E402

config = context.config

if config.config_file_name is not None:
    # disable_existing_loggers 默认 True：alembic 一跑就会把进程里已创建的
    # logger 全部禁用——生产"启动即迁移"先于 uvicorn 建好 app.* logger，
    # 迁移一结束全站日志静默（全量测试里 schema bootstrap 之后的用例
    # caplog 全空即此因，2026-09-30 实录）。显式关掉这个行为。
    fileConfig(config.config_file_name, disable_existing_loggers=False)

# 刻意留空：见模块 docstring
target_metadata = None


def _url() -> str:
    from app.db.session import database_url

    return database_url()


def run_migrations_offline() -> None:
    context.configure(
        url=_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        compare_type=False,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    # 复用 app.db.session 的引擎：生产环境它就是按 settings 惰性创建的 MySQL 引擎，
    # 离线测试里则是注入的 SQLite 引擎——否则「既有库只打点」这条分支没法在没有 MySQL
    # 的环境里被验证（`ensure_schema` 的两种走势都靠它）。
    from app.db import session as db_session

    connectable = db_session.get_engine()
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=False,
            # SQL 目录缺失属于部署错误，不静默跳过
            attributes={"schema_dir": str(SQL_MIGRATION_DIR)},
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
