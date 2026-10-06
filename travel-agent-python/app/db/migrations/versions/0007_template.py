"""V7：模板发布列（SPEC C2.4）。

SQL 真相在 `app/db/migrations/sql/V7__template.sql`，
本 revision 只按序执行 V7，不复制、不改写 SQL；0006 的库升级时只补 V7。
"""

from __future__ import annotations

from alembic import op

from app.db.schema_source import SQL_MIGRATION_DIR, statements_between

revision = "0007_template"
down_revision = "0006_collaboration"
branch_labels = None
depends_on = None

V7_VERSION = 7


def upgrade() -> None:
    statements = statements_between(V7_VERSION, V7_VERSION)
    if not statements:
        raise RuntimeError(f"未找到 V7 迁移 SQL：{SQL_MIGRATION_DIR}（部署或目录搬迁问题）")

    applied_file = None
    for filename, statement in statements:
        if filename != applied_file:
            print(f"[alembic] applying {filename}")
            applied_file = filename
        op.execute(statement)


def downgrade() -> None:
    raise NotImplementedError("本仓库不提供 downgrade：业务库回退请走备份恢复，而不是 DROP 列/表。")
