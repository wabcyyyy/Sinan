"""V16：规划任务持久化表（执行状态/租约/预算的单一权威）。

SQL 真相在 `app/db/migrations/sql/V16__planning_task.sql`，
本 revision 只按序执行 V16，不复制、不改写 SQL；0015 的库升级时只补 V16。
"""

from __future__ import annotations

from alembic import op

from app.db.schema_source import SQL_MIGRATION_DIR, statements_between

revision = "0016_planning_task"
down_revision = "0015_item_cost_currency"
branch_labels = None
depends_on = None

V16_VERSION = 16


def upgrade() -> None:
    statements = statements_between(V16_VERSION, V16_VERSION)
    if not statements:
        raise RuntimeError(f"未找到 V16 迁移 SQL：{SQL_MIGRATION_DIR}（部署或目录搬迁问题）")

    applied_file = None
    for filename, statement in statements:
        if filename != applied_file:
            print(f"[alembic] applying {filename}")
            applied_file = filename
        op.get_bind().exec_driver_sql(statement)


def downgrade() -> None:
    raise NotImplementedError("本仓库不提供 downgrade：任务表退役请走备份恢复，而不是删表丢历史。")
