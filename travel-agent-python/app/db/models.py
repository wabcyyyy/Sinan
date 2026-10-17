"""业务层 ORM 模型的聚合入口：与 Flyway `V1__baseline_schema.sql` 逐列对齐的运行时投影。

分工（哲学 1：每类事实只有一份权威定义）：
- SQL 真相源在 `app/db/migrations/sql/V*.sql`，`scripts/check_schema_contract.py` 与
  `tests/test_db_schema_parity.py` 双向比对列清单；
- 声明基座（`Base` / `SoftDelete` / 方言 variant）与"为什么模型里不写非唯一索引"在
  `app/db/orm_base.py`；
- 表按域拆在 `models_itinerary.py`（行程本体）与 `models_execution.py`（规划任务 + 成本账本），
  本模块留下账号/偏好/协作/字典/开关/网关/反馈这些表，并 **import 全部兄弟模块**——
  既让 `from app.db.models import X` 这一条既有路径继续有效，也保证所有表落在
  同一个 `Base.metadata` 上（`app/db/session.py` 的软删钩子按 `Base.registry.mappers`
  枚举，少 import 一个模块就会少过滤一张表）。
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.models_execution import CostLedger, LlmReservation, PlanningTask
from app.db.models_itinerary import (
    BudgetDetail,
    Expense,
    ItineraryCare,
    ItineraryChatMessage,
    ItineraryDay,
    ItineraryItem,
    ItineraryMain,
    ItineraryVersion,
)
from app.db.orm_base import Base, LongText, PkBigInt, SoftDelete

__all__ = [
    "AddonAudit",
    "AddonState",
    "Base",
    "BudgetDetail",
    "CityConsumption",
    "CityGeo",
    "CostLedger",
    "Expense",
    "ExportTask",
    "ItemFeedback",
    "ItineraryCare",
    "ItineraryChatMessage",
    "ItineraryDay",
    "ItineraryInvitation",
    "ItineraryItem",
    "ItineraryMain",
    "ItineraryMember",
    "ItineraryVersion",
    "LlmReservation",
    "LongText",
    "PkBigInt",
    "PlanningTask",
    "SoftDelete",
    "SysUser",
    "UserLlmGateway",
    "UserPreference",
]


class SysUser(Base, SoftDelete):
    __tablename__ = "sys_user"
    __table_args__ = (UniqueConstraint("username", name="uk_username"),)

    id: Mapped[int] = mapped_column(PkBigInt, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(64), nullable=False)
    password: Mapped[str] = mapped_column(String(128), nullable=False, comment="BCrypt 加密密码")
    nickname: Mapped[str | None] = mapped_column(String(64))
    phone: Mapped[str | None] = mapped_column(String(20))
    status: Mapped[int] = mapped_column(Boolean, nullable=False, default=True, server_default="1")
    role: Mapped[str] = mapped_column(String(20), nullable=False, default="user", server_default="user")
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(), onupdate=func.now()
    )


class UserPreference(Base):
    __tablename__ = "user_preference"
    __table_args__ = (UniqueConstraint("user_id", "pref_label", name="uk_user_pref"),)

    id: Mapped[int] = mapped_column(PkBigInt, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    pref_label: Mapped[str] = mapped_column(String(32), nullable=False)
    count: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    source: Mapped[str] = mapped_column(String(24), nullable=False, default="explicit", server_default="explicit")
    confidence: Mapped[Decimal] = mapped_column(
        Numeric(5, 4), nullable=False, default=Decimal("1.0000"), server_default="1.0000"
    )
    negative: Mapped[int] = mapped_column(Boolean, nullable=False, default=False, server_default="0")
    hard_constraint: Mapped[int] = mapped_column(Boolean, nullable=False, default=False, server_default="0")
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=func.now())
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(), onupdate=func.now()
    )


class ItineraryMember(Base):
    """协作成员（SPEC C2.3）。**硬删**：移除后可重新邀请，故不继承 SoftDelete。

    owner 不在此表——owner 永远由 `itinerary_main.user_id` 判定，成员行只放
    editor/viewer，避免「owner 行 + 主表 user_id」双真相。
    """

    __tablename__ = "itinerary_member"
    __table_args__ = (UniqueConstraint("itinerary_id", "user_id", name="uk_itinerary_member"),)

    id: Mapped[int] = mapped_column(PkBigInt, primary_key=True, autoincrement=True)
    itinerary_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    role: Mapped[str] = mapped_column(String(12), nullable=False, comment="editor/viewer")
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=func.now())


class ItineraryInvitation(Base):
    """邀请（SPEC C2.3）：单次兑换、默认 7 天、owner 可撤销。

    明文 token 只在创建响应里出现一次，库内仅存 sha256 摘要；无受邀
    user_id——兑换时才定人，兑换人写进 accepted_by。
    """

    __tablename__ = "itinerary_invitation"
    __table_args__ = (UniqueConstraint("token_hash", name="uk_invitation_token_hash"),)

    id: Mapped[int] = mapped_column(PkBigInt, primary_key=True, autoincrement=True)
    itinerary_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    role: Mapped[str] = mapped_column(String(12), nullable=False, comment="editor/viewer")
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=func.now())
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime)
    accepted_by: Mapped[int | None] = mapped_column(BigInteger)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime)


class CityConsumption(Base):
    __tablename__ = "city_consumption"
    __table_args__ = (UniqueConstraint("city", name="uk_city"),)

    id: Mapped[int] = mapped_column(PkBigInt, primary_key=True, autoincrement=True)
    city: Mapped[str] = mapped_column(String(64), nullable=False)
    level: Mapped[str] = mapped_column(String(16), nullable=False, default="standard", server_default="standard")
    meal_price: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    transport_price: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    hotel_price: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)


class CityGeo(Base):
    """城市→国家字典（Atlas 归国 / 海外判定单一来源，V2）。

    刻意**不继承 SoftDelete**：字典表无 `deleted` 列（同 `poi_knowledge`）。
    未命中时调用方必须显式处置——Atlas 计入 dictMiss，禁止默认按 CN 处理。
    坐标仅兜底（行程无可用坐标时才用到）；未核实来源前保持 NULL。
    """

    __tablename__ = "city_geo"

    city_name: Mapped[str] = mapped_column(String(64), primary_key=True)
    country: Mapped[str] = mapped_column(String(64), nullable=False)
    country_code: Mapped[str] = mapped_column(String(2), nullable=False)
    lat: Mapped[Decimal | None] = mapped_column(Numeric(10, 7))
    lng: Mapped[Decimal | None] = mapped_column(Numeric(10, 7))
    is_domestic: Mapped[int] = mapped_column(Boolean, nullable=False, default=False, server_default="0")


class ExportTask(Base, SoftDelete):
    __tablename__ = "export_task"

    id: Mapped[int] = mapped_column(PkBigInt, primary_key=True, autoincrement=True)
    itinerary_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    task_type: Mapped[str] = mapped_column(String(16), nullable=False, default="PDF", server_default="PDF")
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="RUNNING", server_default="RUNNING")
    file_path: Mapped[str | None] = mapped_column(String(512))
    error_msg: Mapped[str | None] = mapped_column(String(512))
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)


class AddonState(Base):
    """运行时功能开关状态（G-3.1）：无行 = 走 env 默认值（INV-6）。"""

    __tablename__ = "addon_state"

    addon_key: Mapped[str] = mapped_column(String(48), primary_key=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(), onupdate=func.now()
    )
    updated_by: Mapped[str] = mapped_column(String(64), nullable=False)


class AddonAudit(Base):
    """功能开关切换审计（append-only）：只插入，不更新不删除。"""

    __tablename__ = "addon_audit"

    id: Mapped[int] = mapped_column(PkBigInt, primary_key=True, autoincrement=True)
    addon_key: Mapped[str] = mapped_column(String(48), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False)
    changed_by: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=func.now())


class UserLlmGateway(Base):
    """用户自带 LLM 网关（V11，BYOK）。**刻意不继承 SoftDelete**：配置是凭据面，
    删除即硬删（启用中删除视为同时停用），不保留墓碑行。

    脱敏红线（同 V8 惯例）：api_key_cipher 是 Fernet 密文、api_key_hint 只存尾 4 位，
    两者都不进任何公开 VO/日志/异常；单用户至多一条 enabled=1 由 service 层事务保证
    （MySQL 无法简洁表达部分唯一）。
    """

    __tablename__ = "user_llm_gateway"
    __table_args__ = (UniqueConstraint("user_id", "name", name="uq_gateway_user_name"),)

    id: Mapped[int] = mapped_column(PkBigInt, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False, comment="属主;跨用户读写一律404")
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    base_url: Mapped[str] = mapped_column(String(512), nullable=False, comment="OpenAI兼容网关地址")
    api_key_cipher: Mapped[str] = mapped_column(String(512), nullable=False, comment="Fernet密文,绝不进任何VO/日志")
    api_key_hint: Mapped[str | None] = mapped_column(String(16), comment="尾4位提示")
    model: Mapped[str] = mapped_column(String(128), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="0")
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(), onupdate=func.now()
    )


class ItemFeedback(Base):
    """条目对/错反馈（V8，SPEC C3.5）：一人一条可改（UNIQUE(item_id,user_id) upsert），
    撤销=硬删行（Q5，不留墓碑）。**刻意不继承 SoftDelete**：行程/条目软删后反馈行
    保留（Q6，eval 需要历史），不可见靠投影隔离——本表不进 share/模板/MCP 任何投影。
    """

    __tablename__ = "item_feedback"
    __table_args__ = (UniqueConstraint("item_id", "user_id", name="uq_feedback_item_user"),)

    id: Mapped[int] = mapped_column(PkBigInt, primary_key=True, autoincrement=True)
    itinerary_id: Mapped[int] = mapped_column(
        BigInteger, nullable=False, comment="冗余存行程 id,聚合按行程/城市走 join"
    )
    item_id: Mapped[int] = mapped_column(BigInteger, nullable=False, comment="itinerary_item.id")
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False, comment="记录实名,展示匿名")
    value: Mapped[int] = mapped_column(Boolean, nullable=False, comment="1=right 0=wrong")
    reason: Mapped[str | None] = mapped_column(
        String(32), comment="value=0 必填:wrong_location/wrong_time/wrong_price/not_interested/closed/other"
    )
    note: Mapped[str | None] = mapped_column(String(200), comment="可选备注")
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(), onupdate=func.now()
    )


class TrialInvite(Base):
    __tablename__ = "trial_invite"
    code_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    created_by: Mapped[str] = mapped_column(String(64), nullable=False)
    consumed_by: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=func.now())
    used_at: Mapped[datetime | None] = mapped_column(DateTime)


class TrialWallet(Base):
    __tablename__ = "trial_wallet"
    user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    balance_micros: Mapped[int] = mapped_column(BigInteger, nullable=False)
    spent_micros: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0, server_default="0")


class TrialLedger(Base):
    __tablename__ = "trial_ledger"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    reserved_micros: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0, server_default="0")
    amount_micros: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0, server_default="0")
    model: Mapped[str | None] = mapped_column(String(128))
    detail: Mapped[dict | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=func.now())
