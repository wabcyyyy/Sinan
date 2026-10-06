"""V3：运行时功能开关（Addon 体系，G-3.1）。

约定（与 0001/0002 相同）：SQL 真相在
`app/db/migrations/sql/V3__addons.sql`，本 revision 只按序执行它，
不复制、不改写；**一个 V 文件对应一个 revision，各管各的文件**。

- 空库：`upgrade head` 依次跑 0001（V1）→ 0002（V2）→ 0003（V3）；
- 已在 0002 的库：`upgrade head` 只补 V3。

注意：MySQL DDL 隐式提交，V3 文件本身不是原子事务。半途失败时 alembic 版本号
停在 0002；恢复路径是人工核对补齐缺失语句后 `alembic stamp 0003_addons`。
"""

from __future__ import annotations

from alembic import op

from app.db.schema_source import SQL_MIGRATION_DIR, statements_between

revision = "0003_addons"
down_revision = "0002_atlas_share_covers"
branch_labels = None
depends_on = None

V3_VERSION = 3


def upgrade() -> None:
    statements = statements_between(V3_VERSION, V3_VERSION)
    if not statements:
        raise RuntimeError(f"未找到 V3 迁移 SQL：{SQL_MIGRATION_DIR}（部署或目录搬迁问题）")

    applied_file = None
    for filename, statement in statements:
        if filename != applied_file:
            print(f"[alembic] applying {filename}")
            applied_file = filename
        op.execute(statement)


def downgrade() -> None:
    raise NotImplementedError("本仓库不提供 downgrade：业务库回退请走备份恢复，而不是 DROP 列/表。")
