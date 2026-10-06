"""按序执行 **V1 基线**（revision 与迁移文件的划分约定见 0002 的 docstring）。

不复制 SQL、不改写 SQL：见 `app/db/schema_source` 与 `app/db/migrations/env.py`。

为什么只应用 V1 而不是「全部 V*.sql」：本 revision 是基线的唯一负责人。若它把后续
文件（V2+）也一并执行，空库会在 0001 里建完 V2，随后 0002 再执行一次同名 DDL
（Duplicate column）直接炸；而老库走 stamp 路径时又不会经过本 revision。每个 V 文件
一个 revision、各管各的文件，「空库 upgrade」与「老库 stamp 后 upgrade」两条路才都成立。

已有 Flyway 建过表的库**不要**跑本 revision（DDL 会撞「table already exists」），
应执行 `alembic stamp 0001_wrap_flyway_baseline` 打点；这里做了显式探测并给出
该指令——宁可启动即失败，也不要静默跳过导致「以为建了表其实没有」。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from app.db.schema_source import SQL_MIGRATION_DIR, statements_upto

revision = "0001_wrap_flyway_baseline"
down_revision = None
branch_labels = None
depends_on = None

BASELINE_MAX_VERSION = 1


def _already_migrated(bind) -> bool:
    return sa.inspect(bind).has_table("sys_user")


def upgrade() -> None:
    statements = statements_upto(BASELINE_MAX_VERSION)
    if not statements:
        raise RuntimeError(f"未找到 V1 基线 SQL：{SQL_MIGRATION_DIR}（部署或目录搬迁问题）")

    bind = op.get_bind()
    if _already_migrated(bind):
        existing = [name for name, _ in statements if name == "V1__baseline_schema.sql"]
        raise RuntimeError(
            "目标库已存在业务表（sys_user 可见）。这通常意味着它由 Flyway 建过表："
            "请改用 `alembic stamp 0001_wrap_flyway_baseline` 打点而不是重跑 DDL。"
            + (f"（待执行文件：{existing}）" if existing else "")
        )

    applied_file = None
    for filename, statement in statements:
        if filename != applied_file:
            print(f"[alembic] applying {filename}")
            applied_file = filename
        op.execute(statement)


def downgrade() -> None:
    raise NotImplementedError(
        "基线 revision 不提供 downgrade：Flyway 时代即无回滚，业务库回退请走备份恢复，而不是 DROP 全表。"
    )
