"""执行与成本面表模型（V16/T1 规划任务 + V17/T2 成本账本）。

拆自 `app/db/models.py`；三张表共同点是**都刻意不继承 `SoftDelete`**：

- `planning_task`：软删过滤钩子会把调度表一起过滤掉，而"用户删行程"不等于在途任务可以
  失权乱写（删除流程必须先收敛任务）。
- `cost_ledger` / `llm_reservation`：费用账本与行程/账号的软删过滤无关——删了行程也不该
  让已经花掉的成本从账上消失（那等于给同一个用户再发一份额度）。

行为判据在 `app/services/planning_task_*` 与 `app/services/cost_ledger` / `cost_admission`，
算式在 `app/common/cost_policy.py`；本模块只声明列。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.orm_base import Base, LongText


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
