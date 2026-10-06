"""V4：退役 POI 语料库与酒店房型价目。

SQL 真相在 `app/db/migrations/sql/V4__retire_poi_knowledge.sql`，
本 revision 只按序执行 V4，不复制、不改写 SQL；0003 的库升级时只补 V4。
"""

from __future__ import annotations

from alembic import op

from app.db.schema_source import SQL_MIGRATION_DIR, statements_between

revision = "0004_retire_poi_knowledge"
down_revision = "0003_addons"
branch_labels = None
depends_on = None

V4_VERSION = 4


def upgrade() -> None:
    statements = statements_between(V4_VERSION, V4_VERSION)
    if not statements:
        raise RuntimeError(f"未找到 V4 迁移 SQL：{SQL_MIGRATION_DIR}（部署或目录搬迁问题）")

    applied_file = None
    for filename, statement in statements:
        if filename != applied_file:
            print(f"[alembic] applying {filename}")
            applied_file = filename
        op.execute(statement)


def downgrade() -> None:
    raise NotImplementedError("本仓库不提供 downgrade：业务库回退请走备份恢复，而不是 DROP 列/表。")
