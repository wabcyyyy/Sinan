"""ORM 声明基座：`Base`、逻辑删除标记与方言 variant（所有业务表模型共用的一份）。

业务表模型按域拆在同目录三个模块里，`app.db.models` 是唯一消费入口：
- `models.py`：账号与偏好、协作成员与邀请、城市字典、导出任务、功能开关、BYOK 网关、条目反馈；
- `models_itinerary.py`：行程本体（main / day / item / chat / version / budget / expense）；
- `models_execution.py`：规划任务与成本账本（V16/T1 + V17/T2）。

`app.db.models` 把它们全部 import 进来，所以 `from app.db.models import X` 的既有路径不变，
而 **所有表都注册在同一个 `Base.metadata`** 上（ORM/SQL 列级对齐的 `tests/test_db_schema_parity.py`
与离线建表都按那份 metadata 走，漏一张就红）。

非唯一索引**刻意不在模型里声明**：schema 由 SQL 迁移拥有，模型只是投影；且 MySQL
索引名是表内作用域（`idx_itinerary` 在多张表重复合法），SQLite 索引名是全局作用域，
照抄会让「从模型建表」的离线测试直接撞名。唯一约束则保留——它影响写入语义，测试需要。
"""

from __future__ import annotations

from sqlalchemy import BigInteger, Boolean, Integer, Text
from sqlalchemy.dialects.mysql import LONGTEXT
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# MySQL 用 LONGTEXT，其余方言（SQLite 测试）退回 Text
LongText = Text().with_variant(LONGTEXT(), "mysql")
# MySQL 主键需 BIGINT；SQLite 只有 INTEGER 主键才自增
PkBigInt = BigInteger().with_variant(Integer, "sqlite")


class Base(DeclarativeBase):
    pass


class SoftDelete:
    """带逻辑删除的实体共用此标记。

    过滤**不写在每个查询里**：Java 侧由 MyBatis-Plus @TableLogic 全局自动追加
    `deleted = 0`，等价的 SQLAlchemy 实现见 `app.db.session` 的 do_orm_execute 钩子。
    漏写会让已删除行程继续出现在列表 / 详情 / Atlas / 分享反查中。
    """

    deleted: Mapped[int] = mapped_column(Boolean, nullable=False, default=False, server_default="0")
