"""V10：生成参数持久化（恢复保真）。

SQL 真相在 `app/db/migrations/sql/V10__gen_params.sql`，
本 revision 只按序执行 V10，不复制、不改写 SQL；0009 的库升级时只补 V10。
"""

from __future__ import annotations

from alembic import op

from app.db.schema_source import SQL_MIGRATION_DIR, statements_between

revision = "0010_gen_params"
down_revision = "0009_flight_quotes"
branch_labels = None
depends_on = None

V10_VERSION = 10


def upgrade() -> None:
    statements = statements_between(V10_VERSION, V10_VERSION)
    if not statements:
        raise RuntimeError(f"未找到 V10 迁移 SQL：{SQL_MIGRATION_DIR}（部署或目录搬迁问题）")

    applied_file = None
    for filename, statement in statements:
        if filename != applied_file:
            print(f"[alembic] applying {filename}")
            applied_file = filename
        # exec_driver_sql：V10 列注释含半角冒号（「M1:建壳时写入」），text() 会当绑定参数
        op.get_bind().exec_driver_sql(statement)


def downgrade() -> None:
    raise NotImplementedError("本仓库不提供 downgrade：业务库回退请走备份恢复，而不是 DROP 列/表。")
