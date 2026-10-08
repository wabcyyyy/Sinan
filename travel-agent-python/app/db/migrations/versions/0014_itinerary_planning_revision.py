"""V14：规划修订号（M4 并发应用 CAS）。

SQL 真相在 `app/db/migrations/sql/V14__itinerary_planning_revision.sql`，
本 revision 只按序执行 V14，不复制、不改写 SQL；0013 的库升级时只补 V14。
"""

from __future__ import annotations

from alembic import op

from app.db.schema_source import SQL_MIGRATION_DIR, statements_between

revision = "0014_itinerary_planning_revision"
down_revision = "0013_itinerary_requirements_json"
branch_labels = None
depends_on = None

V14_VERSION = 14


def upgrade() -> None:
    statements = statements_between(V14_VERSION, V14_VERSION)
    if not statements:
        raise RuntimeError(f"未找到 V14 迁移 SQL：{SQL_MIGRATION_DIR}（部署或目录搬迁问题）")

    applied_file = None
    for filename, statement in statements:
        if filename != applied_file:
            print(f"[alembic] applying {filename}")
            applied_file = filename
        op.get_bind().exec_driver_sql(statement)


def downgrade() -> None:
    raise NotImplementedError("本仓库不提供 downgrade：业务库回退请走备份恢复，而不是删修订号列。")
