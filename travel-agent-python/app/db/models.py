"""业务层 ORM 模型：与 Flyway `V1__baseline_schema.sql` 逐列对齐。

迁移治理（PLAN v3.0 §1.4）：**SQL 文件仍是 schema 真相源**，本文件只是它的运行时投影。
两者由 `scripts/check_schema_contract.py` 在 CI 双向比对（实体有列而迁移没有 → 红；
迁移有而实体没有 → 仅信息，允许保留未映射的历史列）。

MySQL 专有写法统一走 variant，以便离线单测能在 SQLite 内存库上真跑 CRUD：
- BIGINT 自增主键在 SQLite 需要 INTEGER 才能真正自增；
- LONGTEXT / TINYINT(1) 在非 MySQL 上降级为 Text / SmallInteger。

非唯一索引**刻意不在模型里声明**：schema 由 SQL 迁移拥有，模型只是投影；且 MySQL
索引名是表内作用域（`idx_itinerary` 在多张表重复合法），SQLite 索引名是全局作用域，
照抄会让「从模型建表」的离线测试直接撞名。唯一约束则保留——它影响写入语义，测试需要。
"""

from __future__ import annotations

from datetime import date, datetime, time
from decimal import Decimal

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    Date,
    DateTime,
    Integer,
    Numeric,
    String,
    Text,
    Time,
    UniqueConstraint,
    func,
)
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


class ItineraryVersion(Base):
    __tablename__ = "itinerary_version"
    __table_args__ = (UniqueConstraint("itinerary_id", "version_no", name="uk_itinerary_version"),)

    id: Mapped[int] = mapped_column(PkBigInt, primary_key=True, autoincrement=True)
    itinerary_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    parent_version_id: Mapped[int | None] = mapped_column(BigInteger)
    version_no: Mapped[int] = mapped_column(Integer, nullable=False)
    # 实际取值 13 种（见 V2 的 MODIFY COMMENT 与 SPEC §7.4）：create/generate/add_item/
    # update_item/move_item/delete_item/reorder/nl_edit/delete/apply_plans/apply_hotel/
    # snapshot/restore；V1 的 DDL 注释 'snapshot/apply/restore' 已过时
    operation: Mapped[str] = mapped_column(String(24), nullable=False, default="snapshot", server_default="snapshot")
    summary: Mapped[str | None] = mapped_column(String(255))
    snapshot_json: Mapped[str] = mapped_column(LongText, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=func.now())


class ItineraryMain(Base, SoftDelete):
    __tablename__ = "itinerary_main"
    __table_args__ = (UniqueConstraint("share_token", name="uk_itinerary_share_token"),)

    id: Mapped[int] = mapped_column(PkBigInt, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    title: Mapped[str] = mapped_column(String(128), nullable=False)
    city: Mapped[str] = mapped_column(String(64), nullable=False)
    start_date: Mapped[date | None] = mapped_column(Date)
    end_date: Mapped[date | None] = mapped_column(Date)
    days: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    persons: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    budget: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    preferences: Mapped[str | None] = mapped_column(String(512))
    hotel_tier: Mapped[str | None] = mapped_column(String(16))
    stay_nights: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    # 面向用户的状态：1=草稿(生成中) 2=已生成 3=已取消
    status: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    plan_note: Mapped[str | None] = mapped_column(Text)
    suggestions_json: Mapped[str | None] = mapped_column(Text)
    trip_theme: Mapped[str | None] = mapped_column(String(64))
    # 运维态生成状态机；NULL = 迁移前存量（列表筛选 active 必须含 IS NULL 分支）
    gen_state: Mapped[str | None] = mapped_column(String(16))
    gen_started_at: Mapped[datetime | None] = mapped_column(DateTime)
    gen_finished_at: Mapped[datetime | None] = mapped_column(DateTime)
    gen_resumed: Mapped[int] = mapped_column(Boolean, nullable=False, default=False, server_default="0")
    # V2（atlas_share_covers）：封面快照三列 + 署名 + 收藏/归档 + 公开分享
    cover_url: Mapped[str | None] = mapped_column(String(512))
    cover_source: Mapped[str | None] = mapped_column(String(16))
    cover_ref: Mapped[str | None] = mapped_column(String(64))
    cover_credit: Mapped[str | None] = mapped_column(Text)
    favorite: Mapped[int] = mapped_column(Boolean, nullable=False, default=False, server_default="0")
    archived: Mapped[int] = mapped_column(Boolean, nullable=False, default=False, server_default="0")
    share_token: Mapped[str | None] = mapped_column(String(48))
    share_expires_at: Mapped[datetime | None] = mapped_column(DateTime)
    # V7（template）：发布快照物化列；published_at NULL = 未发布
    template_published_at: Mapped[datetime | None] = mapped_column(DateTime)
    template_summary: Mapped[dict | None] = mapped_column(JSON, comment="脱敏投影快照（模板广场/fork 数据源）")
    # V9（flight_quotes）：往返航班聚合报价（FlightQuote 数组）；NULL = 本轮未观测到
    flight_quotes: Mapped[list | None] = mapped_column(JSON, comment="往返航班报价（L14，观测事实）")
    # V9：出发地城市名（用户建行程时填）；查航班与实时价的唯一来源
    origin_city: Mapped[str | None] = mapped_column(String(64), comment="出发地城市名（L14）")
    # V10（gen_params）：用户原话参数——建壳写入、僵尸续跑重建命令时读回（恢复保真 P0-3）
    intent: Mapped[str | None] = mapped_column(String(800), comment="旅行意图(M1,最长800字)")
    requirements: Mapped[str | None] = mapped_column(Text, comment="补充要求(自由文本)")
    # V13（requirements_json）：结构化需求快照（M1a 单一真源）——建壳写入规范化
    # TripRequirements，恢复 rebuild 读回；NULL=旧行程，按旧默认规则不反推约束
    requirements_json: Mapped[dict | None] = mapped_column(
        JSON, comment="结构化需求快照(M1a):建壳写入,恢复读回;NULL=旧行程"
    )
    # V14（planning_revision）：规划修订号（M4 CAS）——内容写入口事务内 +1，
    # apply 以原子条件更新比对，过期 409；收藏/封面等非内容写不推进
    planning_revision: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0, server_default="0")
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(), onupdate=func.now()
    )


class ItineraryChatMessage(Base):
    __tablename__ = "itinerary_chat_message"

    id: Mapped[int] = mapped_column(PkBigInt, primary_key=True, autoincrement=True)
    itinerary_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False, comment="user/ai")
    content: Mapped[str] = mapped_column(Text, nullable=False)
    plans_json: Mapped[str | None] = mapped_column(LongText)
    hotel_options_json: Mapped[str | None] = mapped_column(LongText)
    changed: Mapped[int] = mapped_column(Boolean, nullable=False, default=False, server_default="0")
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=func.now())


class ItineraryDay(Base, SoftDelete):
    __tablename__ = "itinerary_day"
    __table_args__ = (
        UniqueConstraint("itinerary_id", "day_no", name="uk_itinerary_day_no"),
        UniqueConstraint("generation_action_id", name="uk_generation_action"),
    )

    id: Mapped[int] = mapped_column(PkBigInt, primary_key=True, autoincrement=True)
    itinerary_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    day_no: Mapped[int] = mapped_column(Integer, nullable=False)
    travel_date: Mapped[date | None] = mapped_column(Date)
    city: Mapped[str | None] = mapped_column(String(64))
    note: Mapped[str | None] = mapped_column(String(512))
    metadata_json: Mapped[str | None] = mapped_column(LongText)
    generation_action_id: Mapped[str | None] = mapped_column(String(96))
    generation_fingerprint: Mapped[str | None] = mapped_column(String(64))
    generation_status: Mapped[str] = mapped_column(
        String(24), nullable=False, default="PENDING", server_default="PENDING"
    )
    generation_error: Mapped[str | None] = mapped_column(String(512))
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(), onupdate=func.now()
    )


class ItineraryItem(Base, SoftDelete):
    __tablename__ = "itinerary_item"

    id: Mapped[int] = mapped_column(PkBigInt, primary_key=True, autoincrement=True)
    day_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    itinerary_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    item_type: Mapped[str] = mapped_column(String(16), nullable=False)
    poi_name: Mapped[str] = mapped_column(String(128), nullable=False)
    poi_id: Mapped[str | None] = mapped_column(String(64))
    address: Mapped[str | None] = mapped_column(String(255))
    latitude: Mapped[Decimal | None] = mapped_column(Numeric(10, 6))
    longitude: Mapped[Decimal | None] = mapped_column(Numeric(10, 6))
    start_time: Mapped[time | None] = mapped_column(Time)
    end_time: Mapped[time | None] = mapped_column(Time)
    duration_min: Mapped[int | None] = mapped_column(Integer)
    cost: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))
    # cost 的币种（V15）：'CNY' = 币种有据（模型声明后已换算 / 用户手填）；NULL = 未声明
    cost_currency: Mapped[str | None] = mapped_column(String(3))
    tag: Mapped[str | None] = mapped_column(String(32))
    remark: Mapped[str | None] = mapped_column(String(255))
    open_time: Mapped[str | None] = mapped_column(String(64))
    image_url: Mapped[str | None] = mapped_column(String(1024))
    source: Mapped[str | None] = mapped_column(String(128))
    source_updated_at: Mapped[datetime | None] = mapped_column(DateTime)
    verification_status: Mapped[str] = mapped_column(
        String(24), nullable=False, default="unverified", server_default="unverified"
    )
    value_kind: Mapped[str] = mapped_column(String(16), nullable=False, default="generated", server_default="generated")
    freshness_status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="unknown", server_default="unknown"
    )
    review_requirement: Mapped[str] = mapped_column(
        String(24), nullable=False, default="before_departure", server_default="before_departure"
    )
    fact_evidence_json: Mapped[str | None] = mapped_column(LongText)
    intro: Mapped[str | None] = mapped_column(String(600))
    why_note: Mapped[str | None] = mapped_column(String(255))
    sort_no: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(), onupdate=func.now()
    )


class BudgetDetail(Base, SoftDelete):
    __tablename__ = "budget_detail"

    id: Mapped[int] = mapped_column(PkBigInt, primary_key=True, autoincrement=True)
    itinerary_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    day_id: Mapped[int | None] = mapped_column(BigInteger)
    category: Mapped[str] = mapped_column(String(16), nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    item_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    remark: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(), onupdate=func.now()
    )


class Expense(Base, SoftDelete):
    __tablename__ = "expense"

    id: Mapped[int] = mapped_column(PkBigInt, primary_key=True, autoincrement=True)
    itinerary_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    category: Mapped[str] = mapped_column(String(16), nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    currency: Mapped[str] = mapped_column(String(8), nullable=False, default="CNY", server_default="CNY")
    day_no: Mapped[int | None] = mapped_column(Integer)
    item_id: Mapped[int | None] = mapped_column(BigInteger)
    spent_at: Mapped[date | None] = mapped_column(Date)
    payment_method: Mapped[str | None] = mapped_column(String(24))
    note: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=func.now())


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


class PlanningTask(Base):
    """规划任务的持久化执行面（V16，上线 v1 T1）：状态/租约/绝对截止/累计预算的单一权威。

    一趟行程同一时刻只有一行（主键就是 itinerary_id）：交付责任随 attempt 延续，
    接管与显式补齐都在同一行上推进，累计消耗不随行清零——所以反复重启/重试拿不到
    新的调用与 token 额度。所有权由 (task_id, lease_owner) 的条件更新判定，失权写入
    rowcount=0 即抛错，不再依赖进程内活跃表。

    **刻意不继承 SoftDelete**：软删过滤钩子会把这张调度表一起过滤掉——用户删行程
    不等于在途任务可以失权乱写；删除流程必须先收敛任务（T7 的数据删除负责那条）。
    """

    __tablename__ = "planning_task"

    itinerary_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False, comment="提交用户,排队准入按人计数")
    task_id: Mapped[str] = mapped_column(String(32), nullable=False, comment="当前持有者令牌,acquire 时轮换")
    lease_owner: Mapped[str] = mapped_column(String(64), nullable=False, comment="持有 worker 身份(pid+随机段)")
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    state: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default="QUEUED",
        server_default="QUEUED",
        comment="QUEUED|RUNNING|FINISHED|FAILED|CANCELLED|EXPIRED",
    )
    submit_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, comment="首次提交(交付责任起点)")
    deadline_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, comment="当前 attempt 的绝对截止时刻")
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime)
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime)
    cancel_requested_at: Mapped[datetime | None] = mapped_column(DateTime, comment="非空=用户已请求取消")
    consumed_llm_calls: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0", comment="本责任累计模型调用(跨 attempt 只增)"
    )
    consumed_prompt_tokens: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0, server_default="0")
    consumed_completion_tokens: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0, server_default="0")
    consumed_retrievals: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    consumed_research_calls: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    consumed_existence_checks: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    consumed_replans: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    budget_json: Mapped[str] = mapped_column(LongText, nullable=False, comment="当前 attempt 的 RunLimits 快照")
    result_code: Mapped[str | None] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(), onupdate=func.now()
    )


class CostLedger(Base):
    """成本账本（V17，上线 v1 T2）：**准入判据**的持久权威——用户日窗 / 平台日总额 / 行程补齐额度。

    一行 = 一个窗口。`charge_*` 在预留时就按保守值入账，结算时按真值调整；供应商没回 usage
    或进程崩溃（ABANDONED）**不回吐**，所以「反复失败重试」换不来免费额度，硬顶只增不减。
    上限比较写在 UPDATE 的 WHERE 里（同一行的 X 锁），因此并发预留不可能超卖。

    刻意**不**继承 SoftDelete：费用账本与行程/账号的软删过滤无关——删了行程也不该让
    已经花掉的成本从账上消失（那等于给同一个用户再发一份额度）。

    单位只有 token 与调用次数：本仓没有可信定价表，编造人民币金额只会造出一份
    「看着精确、实则无人负责」的账（PLAN §5 T2 原话：没有可信定价时用 token 硬顶）。
    """

    __tablename__ = "cost_ledger"

    scope_key: Mapped[str] = mapped_column(String(96), primary_key=True, comment="账本键，见 cost_policy")
    scope_kind: Mapped[str] = mapped_column(String(16), nullable=False, comment="platform|user|trip")
    window_day: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0", comment="YYYYMMDD；0=终身窗"
    )
    attribution: Mapped[str] = mapped_column(
        String(16), nullable=False, default="platform", server_default="platform", comment="platform|byok"
    )
    charge_prompt_tokens: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0, server_default="0")
    charge_completion_tokens: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0, server_default="0")
    charged_calls: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    open_reservations: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    abandoned_reservations: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    settled_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    repairs: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0", comment="trip:已用补齐额度"
    )
    repair_failures: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0", comment="trip:连续补齐失败次数(成功即清零)"
    )
    backoff_until: Mapped[datetime | None] = mapped_column(DateTime, comment="trip:退避到期时刻")
    breaker_tripped_at: Mapped[datetime | None] = mapped_column(DateTime, comment="非空=已触顶熔断,停止发新请求")
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(), onupdate=func.now()
    )


class LlmReservation(Base):
    """单笔模型调用预留（V17）：持久预留、幂等结算、崩溃收口与计费归属的唯一权威。

    `reservation_id` 就是 `RunLimits.reserve_llm` 返回的令牌，所以「结算」是一次
    `WHERE state='OPEN'` 的 CAS：命中即首次结算，未命中即已结算或已被收口——一笔预留
    恰好结算一次，重复结算与崩溃后补结算都被同一判据挡住。

    与 `usage_store` 的 `llm_calls`（调用明细，每次 HTTP 尝试一行）不是一回事：
    明细面向仪表盘与对账，**不是**准入依据；本表面向准入与结算不变式，不做聚合查询。
    """

    __tablename__ = "llm_reservation"

    reservation_id: Mapped[str] = mapped_column(
        String(36), primary_key=True, comment="预留令牌（reserve_llm 返回的 uuid）"
    )
    scope_key: Mapped[str] = mapped_column(String(96), nullable=False, comment="主归属账本键,按行程/用户反查用")
    charged_scopes: Mapped[str] = mapped_column(
        String(255), nullable=False, comment="预留级联记账的窗口键清单(结算/收口按它回放,跨日不串窗)"
    )
    attribution: Mapped[str] = mapped_column(String(16), nullable=False, comment="platform|byok")
    attribution_note: Mapped[str | None] = mapped_column(String(48), comment="归属降级原因,如 byok_lookup_failed")
    task_id: Mapped[str | None] = mapped_column(String(32))
    user_id: Mapped[int | None] = mapped_column(BigInteger)
    itinerary_id: Mapped[int | None] = mapped_column(BigInteger)
    scene: Mapped[str] = mapped_column(String(32), nullable=False, default="other", server_default="other")
    reserved_prompt_tokens: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0, server_default="0")
    reserved_completion_tokens: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0, server_default="0")
    charged_prompt_tokens: Mapped[int | None] = mapped_column(BigInteger)
    charged_completion_tokens: Mapped[int | None] = mapped_column(BigInteger)
    usage_complete: Mapped[int] = mapped_column(Boolean, nullable=False, default=False, server_default="0")
    state: Mapped[str] = mapped_column(
        String(16), nullable=False, default="OPEN", server_default="OPEN", comment="OPEN|SETTLED|ABANDONED"
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=func.now())
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, comment="结算宽限到期:过期仍 OPEN=进程已死")
    settled_at: Mapped[datetime | None] = mapped_column(DateTime)
