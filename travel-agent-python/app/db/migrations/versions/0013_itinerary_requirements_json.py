"""V13：结构化需求持久化（M1a 需求单一真源）。

SQL 真相在 `app/db/migrations/sql/V13__itinerary_requirements_json.sql`，
本 revision 只按序执行 V13，不复制、不改写 SQL；0012 的库升级时只补 V13。
"""

from __future__ import annotations

from alembic import op

from app.db.schema_source import SQL_MIGRATION_DIR, statements_between

revision = "0013_itinerary_requirements_json"
down_revision = "0012_city_geo_domestic_expansion"
branch_labels = None
depends_on = None

V13_VERSION = 13


def upgrade() -> None:
    statements = statements_between(V13_VERSION, V13_VERSION)
    if not statements:
        raise RuntimeError(f"未找到 V13 迁移 SQL：{SQL_MIGRATION_DIR}（部署或目录搬迁问题）")

    applied_file = None
    for filename, statement in statements:
        if filename != applied_file:
            print(f"[alembic] applying {filename}")
            applied_file = filename
        op.get_bind().exec_driver_sql(statement)


def downgrade() -> None:
    raise NotImplementedError("本仓库不提供 downgrade：业务库回退请走备份恢复，而不是删需求快照列。")
