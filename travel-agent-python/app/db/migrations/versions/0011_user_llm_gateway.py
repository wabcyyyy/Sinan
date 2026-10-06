"""V11：用户自带 LLM 网关（BYOK）。

SQL 真相在 `app/db/migrations/sql/V11__user_llm_gateway.sql`，
本 revision 只按序执行 V11，不复制、不改写 SQL；0010 的库升级时只补 V11。
"""

from __future__ import annotations

from alembic import op

from app.db.schema_source import SQL_MIGRATION_DIR, statements_between

revision = "0011_user_llm_gateway"
down_revision = "0010_gen_params"
branch_labels = None
depends_on = None

V11_VERSION = 11


def upgrade() -> None:
    statements = statements_between(V11_VERSION, V11_VERSION)
    if not statements:
        raise RuntimeError(f"未找到 V11 迁移 SQL：{SQL_MIGRATION_DIR}（部署或目录搬迁问题）")

    applied_file = None
    for filename, statement in statements:
        if filename != applied_file:
            print(f"[alembic] applying {filename}")
            applied_file = filename
        # exec_driver_sql：列注释含半角冒号（「user_id;跨用户读写一律404」），text() 会当绑定参数
        op.get_bind().exec_driver_sql(statement)


def downgrade() -> None:
    raise NotImplementedError("本仓库不提供 downgrade：业务库回退请走备份恢复，而不是 DROP 列/表。")
