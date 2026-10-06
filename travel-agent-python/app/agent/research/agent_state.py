"""Agent 图状态的**单一定义**（PR-2 State 单一化）。

day / trip / research 三条分支共用一个 Pydantic 模型（未用到的字段保持默认）：

- LangGraph 把**模型实例**传给节点，节点一律属性访问（PR-2 已删
  ``get`` / ``__getitem__`` / ``__contains__`` dict 兼容访问器并就地迁移全部节点）；
  节点返回部分更新 dict 的约定不变，LangGraph coerce 回模型（``extra="forbid"``：
  未知键响亮失败）；``invoke()`` 的返回值是 **dict 快照**，编排层照旧 dict 读取。
- 为什么落在 research 域：状态携带 ``ResearchTask`` / ``EvidencePack``（research
  域内类型），research 是域阶梯上最浅的合法位置——generation → research 是既有
  依赖方向（见 app/agent/README.md），反过来 research import generation 会破阶梯
  （import-linter + tests/test_agent_domain_ladder.py 双机检）。
- PR-3 的 checkpoint / 断点恢复同样消费本模型：状态可序列化性只此一个改造点。
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from app.agent.research.evidence import EvidencePack, ResearchTask
from app.schemas.trip import ChatTurnResponse, DailyPlan, GenerateDayRequest, GenerateRequest, GenerateResponse

#: `UnifiedAgentState.mode` 的取值词表（图 dispatch 按此分流，全仓共用一份）。
MODE_TRIP = "trip"
MODE_DAY = "day"
MODE_STREAM = "stream"


class UnifiedAgentState(BaseModel):
    """day / trip / research 共用状态；未用到的字段保持默认即可。

    M0：由 TypedDict(total=False) 改为 Pydantic BaseModel（同名字段、全字段带默认值）；
    PR-2：吸收 AgentState（trip 字段的旧 TypedDict 副本）与 ResearchAgentState 两份
    重复定义（哲学 1：每类事实只有一份权威定义），删除 dict 兼容访问器、节点全部
    改属性访问（哲学 4：就地迁移，不留兼容壳）。检索计划字段因与当日计划 `plan`
    撞名，改称 `research_plan`（原 ResearchAgentState.plan）。

    - extra="forbid"：节点返回未知键时响亮失败（保持 LangGraph 对非法更新键的
      报错语义，防止静默丢字段）；
    - dict 入口不变：``unified_agent_graph.invoke(empty_*_state(req))`` 仍传 dict，
      LangGraph 自行 coerce 成模型实例；节点返回部分更新 dict 的约定也不变。
    """

    model_config = ConfigDict(extra="forbid")

    mode: str = "trip"

    # ---- day ----
    day_request: GenerateDayRequest | None = None
    plan: DailyPlan | None = None
    source: str = ""
    force_fallback: bool = False

    # ---- trip（与 workflow 门面的节点函数对齐）----
    request: GenerateRequest | None = None
    requirements: dict = Field(default_factory=dict)
    candidates: list[dict] = Field(default_factory=list)
    foods: list[dict] = Field(default_factory=list)
    hotels: list[dict] = Field(default_factory=list)
    consumption: dict | None = None
    weather: list[dict] | None = None
    daily_plans: list[dict] = Field(default_factory=list)
    budget_estimate: dict = Field(default_factory=dict)
    raw_suggestions: list[dict] = Field(default_factory=list)
    result: GenerateResponse | None = None
    fix_count: int = 0
    degraded_reason: str | None = None
    schedule_report: dict = Field(default_factory=dict)
    critic_report: dict = Field(default_factory=dict)
    research_report: dict = Field(default_factory=dict)
    refill_count: int = 0

    # ---- research 子图（仅 run_research 的子图入口注入；day/trip 主图不触达）----
    task: ResearchTask | None = None
    # 原 ResearchAgentState.plan（LLM 检索计划）；与 day 分支的当日计划 plan 撞名
    research_plan: dict = Field(default_factory=dict)
    items: list[dict] = Field(default_factory=list)
    round: int = 0
    sufficient: bool = True
    extra_keywords: list[str] = Field(default_factory=list)
    quota_note: str = ""
    pack: EvidencePack | None = None

    # ---- chat 确认流（PR-6 HITL）----
    # 提案与确认复核的载体（均为 app.schemas 契约类型，编辑域图跨暂停读写）
    chat_reply: ChatTurnResponse | None = None
    chat_confirm: dict | None = None

    # ---- shared ----
    attempts: int = 0
    error: str | None = None
    feedback: str = ""
    validation_issues: list[str] = Field(default_factory=list)
    validation_log: list[str] = Field(default_factory=list)
