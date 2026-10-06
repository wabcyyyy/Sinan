"""启动即迁移：把 Java 侧「Flyway 在 boot 时执行 db/migration/V*.sql」的行为补到 Python。

为什么需要它：切流量之后（M7-b）Python 是唯一对外后端，而 schema 创建原先靠 Java 启动时的
Flyway。少了这一步，**空库起服务会在第一个查询上炸**——不是启动失败，而是用户点开列表才 500。

三条必须同时成立的路（缺一条就会踩坑）：
1. 双跑期 Java 仍会先跑 Flyway，库里已有表、但没有 `alembic_version`：先
   `alembic stamp 0001_wrap_flyway_baseline` 登记基线（绝不重跑 V1 DDL），
   再 `upgrade head` 把基线之后的迁移（V2+）补齐；
2. Java 退役后新库一片空白：必须真的 `upgrade head` 按版本序建表（V1 → V2 → …）；
3. 已经打过点的库：直接 `upgrade head` 补增量。

判据用「核心表是否存在」而不是只看 `alembic_version`：双跑期的库里后者根本不存在。

**为什么老库打点是打到基线而不是 head**（SPEC v2.3 E23）：revision 已按文件拆分
（0001 管 V1、0002 管 V2，见 `app/db/migrations/versions/`）。若直接 `stamp head`，
基线之后的 DDL 会被整体跳过——库看着「已到最新」，实际缺列，点开列表才 500。

调用点在 `main.py` 的 `__main__` 分支（服务真正启动时），**不放 lifespan**：离线测试会构造
大量 TestClient，任何"启动即连真库"的行为都会把单测拖到 MySQL 上（本仓有 600+ 条离线用例）。
"""

from __future__ import annotations

import logging
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import inspect

from app.db import session as db_session

logger = logging.getLogger(__name__)

SERVICE_ROOT = Path(__file__).resolve().parents[2]
MIGRATIONS_DIR = SERVICE_ROOT / "app" / "db" / "migrations"
ALEMBIC_INI = SERVICE_ROOT / "alembic.ini"

# 用来判断「这个库是不是已经建过 schema」：随便挑一张 V1 里必有的表即可
PROBE_TABLE = "sys_user"

# V1 基线对应的 revision：老库打点必须打到这里（不是 head），否则基线之后的迁移会被跳过
BASELINE_REVISION = "0001_wrap_flyway_baseline"


def alembic_config() -> Config:
    config = Config(str(ALEMBIC_INI))
    # 显式给绝对路径：alembic.ini 里是相对路径，从别的 cwd 启动就找不到了
    config.set_main_option("script_location", str(MIGRATIONS_DIR))
    return config


def ensure_schema() -> str:
    """返回 'stamped'（既有库打点）/ 'upgraded'（执行了迁移）。失败时向上抛，启动即失败。"""
    inspector = inspect(db_session.get_engine())
    has_schema = inspector.has_table(PROBE_TABLE)
    has_version = inspector.has_table("alembic_version")

    config = alembic_config()
    if has_schema and not has_version:
        # 既有库（双跑期 Java/Flyway 建的）：先登记基线，绝不重跑 V1 DDL；
        # 再 upgrade 把基线之后的迁移（V2+）补齐
        command.stamp(config, BASELINE_REVISION)
        logger.info("schema present without alembic_version: stamped baseline %s", BASELINE_REVISION)
        command.upgrade(config, "head")
        logger.info("alembic upgrade head applied after baseline stamp")
        return "stamped"

    command.upgrade(config, "head")
    logger.info("alembic upgrade head applied%s", "" if has_schema else " (empty database)")
    return "upgraded"
