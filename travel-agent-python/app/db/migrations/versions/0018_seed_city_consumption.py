"""V18：城市消费基准并入迁移链（修新装库 city_consumption 恒空）。

SQL 真相在 `app/db/migrations/sql/V18__seed_city_consumption.sql`，
本 revision 只按序执行 V18，不复制、不改写 SQL；0017 的库升级时只补 V18。
"""

from __future__ import annotations

from alembic import op

from app.db.schema_source import SQL_MIGRATION_DIR, statements_between

revision = "0018_seed_city_consumption"
down_revision = "0017_cost_ledger"
branch_labels = None
depends_on = None

V18_VERSION = 18


def upgrade() -> None:
    statements = statements_between(V18_VERSION, V18_VERSION)
    if not statements:
        raise RuntimeError(f"未找到 V18 迁移 SQL：{SQL_MIGRATION_DIR}（部署或目录搬迁问题）")

    applied_file = None
    for filename, statement in statements:
        if filename != applied_file:
            print(f"[alembic] applying {filename}")
            applied_file = filename
        op.get_bind().exec_driver_sql(statement)


def downgrade() -> None:
    raise NotImplementedError("本仓库不提供 downgrade：业务库回退请走备份恢复，而不是删种子行。")
