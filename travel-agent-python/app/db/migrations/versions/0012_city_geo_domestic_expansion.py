"""V12：city_geo 补种国内旅游城市（GROUND-1）。

SQL 真相在 `app/db/migrations/sql/V12__city_geo_domestic_expansion.sql`，
本 revision 只按序执行 V12，不复制、不改写 SQL；0011 的库升级时只补 V12。
"""

from __future__ import annotations

from alembic import op

from app.db.schema_source import SQL_MIGRATION_DIR, statements_between

revision = "0012_city_geo_domestic_expansion"
down_revision = "0011_user_llm_gateway"
branch_labels = None
depends_on = None

V12_VERSION = 12


def upgrade() -> None:
    statements = statements_between(V12_VERSION, V12_VERSION)
    if not statements:
        raise RuntimeError(f"未找到 V12 迁移 SQL：{SQL_MIGRATION_DIR}（部署或目录搬迁问题）")

    applied_file = None
    for filename, statement in statements:
        if filename != applied_file:
            print(f"[alembic] applying {filename}")
            applied_file = filename
        op.get_bind().exec_driver_sql(statement)


def downgrade() -> None:
    raise NotImplementedError("本仓库不提供 downgrade：业务库回退请走备份恢复，而不是删字典行。")
