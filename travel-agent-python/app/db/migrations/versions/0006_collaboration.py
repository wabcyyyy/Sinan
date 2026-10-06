"""V6：协作成员与邀请（SPEC C2.3）。

SQL 真相在 `app/db/migrations/sql/V6__itinerary_member.sql`，
本 revision 只按序执行 V6，不复制、不改写 SQL；0005 的库升级时只补 V6。
"""

from __future__ import annotations

from alembic import op

from app.db.schema_source import SQL_MIGRATION_DIR, statements_between

revision = "0006_collaboration"
down_revision = "0005_expense"
branch_labels = None
depends_on = None

V6_VERSION = 6


def upgrade() -> None:
    statements = statements_between(V6_VERSION, V6_VERSION)
    if not statements:
        raise RuntimeError(f"未找到 V6 迁移 SQL：{SQL_MIGRATION_DIR}（部署或目录搬迁问题）")

    applied_file = None
    for filename, statement in statements:
        if filename != applied_file:
            print(f"[alembic] applying {filename}")
            applied_file = filename
        op.execute(statement)


def downgrade() -> None:
    raise NotImplementedError("本仓库不提供 downgrade：业务库回退请走备份恢复，而不是 DROP 列/表。")
