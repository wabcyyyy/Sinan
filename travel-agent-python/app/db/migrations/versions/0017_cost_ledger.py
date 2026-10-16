"""V17：成本账本与调用预留表（费用硬顶/幂等结算的持久权威，上线 v1 T2）。

SQL 真相在 `app/db/migrations/sql/V17__cost_ledger.sql`，
本 revision 只按序执行 V17，不复制、不改写 SQL；0016 的库升级时只补 V17。
"""

from __future__ import annotations

from alembic import op

from app.db.schema_source import SQL_MIGRATION_DIR, statements_between

revision = "0017_cost_ledger"
down_revision = "0016_planning_task"
branch_labels = None
depends_on = None

V17_VERSION = 17


def upgrade() -> None:
    statements = statements_between(V17_VERSION, V17_VERSION)
    if not statements:
        raise RuntimeError(f"未找到 V17 迁移 SQL：{SQL_MIGRATION_DIR}（部署或目录搬迁问题）")

    applied_file = None
    for filename, statement in statements:
        if filename != applied_file:
            print(f"[alembic] applying {filename}")
            applied_file = filename
        op.get_bind().exec_driver_sql(statement)


def downgrade() -> None:
    raise NotImplementedError("本仓库不提供 downgrade：费用账本退役请先备份，不允许删表丢账。")
