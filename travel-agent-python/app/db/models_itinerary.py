"""行程本体表模型（业务层 ORM）：一趟行程的交付物与它的历史。

拆自 `app/db/models.py`（同域三张表一组，见那里的模块 docstring）；`Base` / `SoftDelete`
与方言 variant 在 `app/db/orm_base`，本模块的表仍由 `app.db.models` 统一 import，
所以都注册在同一个 `Base.metadata` 上。

与 Flyway `V1__baseline_schema.sql` 逐列对齐：SQL 才是 schema 真相源，这里只是运行时投影。
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
from sqlalchemy.orm import Mapped, mapped_column

from app.db.orm_base import Base, LongText, PkBigInt, SoftDelete


class ItineraryPreparation(Base):
    __tablename__ = "itinerary_preparation"

    itinerary_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    revision: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0, server_default="0")
    data_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=func.now())


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
    generation_event_seq: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0, server_default="0")
    event_wire_seq: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0, server_default="0")
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


class ItineraryCare(Base):
    """出发前照看任务的持久状态（V20，个人 Agent V1）：一行 = 一趟行程的照看授权与最近结果。

    刻意**不继承 SoftDelete**（同 planning_task 的理由）：它没有 deleted 列，也不该有——
    行程软删后这一行自然不可达（GET/PUT 都先复核 itinerary_main 归属，404），
    扫描面（S2 起）按「主行程存在且未出发」再筛，不依赖本表自身可删。

    日期口径：`last_ai_attempt_on` 是**上海自然日**（Asia/Shanghai，由 app/common/clock
    统一解释，禁止主机 date.today()）；其余时刻按既有约定存应用侧 naive 本地时刻。
    """

    __tablename__ = "itinerary_care"

    itinerary_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    # enabled 也表示用户同意按现有额度机制消耗后台 AI（规格 §4.1）
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="0")
    watched_item_ids_json: Mapped[list | None] = mapped_column(JSON, comment="关注天气影响的活动 item ID（1-20）")
    next_check_at: Mapped[datetime | None] = mapped_column(DateTime, comment="到期扫描时刻；NULL=不参与扫描")
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime, comment="最近一次检查完成时刻（checkedAt）")
    last_status: Mapped[str | None] = mapped_column(String(24), comment="用户可见状态（pending/no_risk/...）")
    last_result_json: Mapped[dict | None] = mapped_column(
        JSON, comment="有界结果：逐日观测/受影响项/原因/pendingMessageId"
    )
    notified_risks_json: Mapped[list | None] = mapped_column(JSON, comment="已呈现或已忽略的风险键（去重权威）")
    last_ai_attempt_on: Mapped[date | None] = mapped_column(Date, comment="上海自然日：每日至多一次自动 AI 尝试")
    check_token: Mapped[str | None] = mapped_column(String(36), comment="争抢令牌：失效 token 不得发布结果")
    checking_until: Mapped[datetime | None] = mapped_column(DateTime, comment="争抢截止：过期 token 可重领")
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(), onupdate=func.now()
    )


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
