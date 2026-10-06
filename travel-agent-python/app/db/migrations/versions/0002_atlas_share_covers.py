"""V2：行程封面 / 收藏归档 / 公开分享 / 城市字典（atlas_share_covers）。

约定（与 0001 相同，SPEC v2.3 S0-13）：SQL 真相在
`app/db/migrations/sql/V2__atlas_share_covers.sql`，本 revision 只按序执行它，
不复制、不改写；**一个 V 文件对应一个 revision，各管各的文件**。这样两条路都成立：

- 空库：`upgrade head` 依次跑 0001（V1）→ 0002（V2）；
- Flyway 时代老库（有表、无 alembic_version）：`migrate.ensure_schema` 先
  `stamp 0001_wrap_flyway_baseline` 再 `upgrade head`，只补 V2，不重跑 V1。

注意：MySQL DDL 隐式提交，V2 文件本身不是原子事务。半途失败时 alembic 版本号
停在 0001、库可能半套 V2；恢复路径是人工核对补齐缺失语句后
`alembic stamp 0002_atlas_share_covers`（无 Flyway repair 了）。
"""

from __future__ import annotations

from alembic import op

from app.db.schema_source import SQL_MIGRATION_DIR, statements_between

revision = "0002_atlas_share_covers"
down_revision = "0001_wrap_flyway_baseline"
branch_labels = None
depends_on = None

V2_VERSION = 2


def upgrade() -> None:
    statements = statements_between(V2_VERSION, V2_VERSION)
    if not statements:
        raise RuntimeError(f"未找到 V2 迁移 SQL：{SQL_MIGRATION_DIR}（部署或目录搬迁问题）")

    applied_file = None
    for filename, statement in statements:
        if filename != applied_file:
            print(f"[alembic] applying {filename}")
            applied_file = filename
        op.execute(statement)


def downgrade() -> None:
    raise NotImplementedError("本仓库不提供 downgrade：业务库回退请走备份恢复，而不是 DROP 列/表。")
