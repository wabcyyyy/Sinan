"""V15：行程项费用币种（条目 cost 的币种显式化）。

SQL 真相在 `app/db/migrations/sql/V15__item_cost_currency.sql`，
本 revision 只按序执行 V15，不复制、不改写 SQL；0014 的库升级时只补 V15。
"""

from __future__ import annotations

from alembic import op

from app.db.schema_source import SQL_MIGRATION_DIR, statements_between

revision = "0015_item_cost_currency"
down_revision = "0014_itinerary_planning_revision"
branch_labels = None
depends_on = None

V15_VERSION = 15


def upgrade() -> None:
    statements = statements_between(V15_VERSION, V15_VERSION)
    if not statements:
        raise RuntimeError(f"未找到 V15 迁移 SQL：{SQL_MIGRATION_DIR}（部署或目录搬迁问题）")

    applied_file = None
    for filename, statement in statements:
        if filename != applied_file:
            print(f"[alembic] applying {filename}")
            applied_file = filename
        op.get_bind().exec_driver_sql(statement)


def downgrade() -> None:
    raise NotImplementedError("本仓库不提供 downgrade：业务库回退请走备份恢复，而不是删币种列。")
