"""P4 有界主动取证；查询单位不是人民币，也不是供应商计费。"""

from typing import Literal

from pydantic import Field

from app.schemas.common import WireModel


class ActiveEvidenceOptions(WireModel):
    strategy: Literal["fixed", "all", "decision", "bottleneck"] = "fixed"
    max_queries: int = Field(default=3, ge=1, le=6)
    budget_units: int = Field(default=4, ge=1, le=12)
    time_limit_seconds: float = Field(default=8, ge=0.5, le=15)
    allow_source_search: bool = False


class EvidenceProbe(WireModel):
    gap_id: str
    kind: Literal["opening", "price", "duration", "route"]
    targets: list[str]
    importance: int
    cost_units: int
    fact_state: str
    low_status: str
    high_status: str
    sensitivity: int
    score: float
    reason: str


class EvidenceQueryRecord(WireModel):
    gap_id: str
    tool: str
    outcome: Literal["updated", "unknown", "ask_user", "rejected", "error"]
    cost_units: int
    decision_changed: bool = False
    source_url: str | None = None
    retrieved_at: str | None = None
    binding_basis: str | None = None
    reason: str


class ActiveEvidenceReport(WireModel):
    policy: str = "bottleneck_heuristic"
    probes: list[EvidenceProbe] = Field(default_factory=list, max_length=48)
    queries: list[EvidenceQueryRecord] = Field(default_factory=list, max_length=6)
    questions: list[str] = Field(default_factory=list, max_length=8)
    spent_units: int = 0
    budget_units: int = 0
    stop_reason: str = "disabled"
    solve_calls: int = 0
    elapsed_seconds: float = 0
    retrieval_calls: int = 0
    llm_calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    assumptions: list[str] = Field(
        default_factory=lambda: [
            "单字段双端是假设敏感性试验，不是概率或可信区间；其余未知保持当前假设",
            "决策价值为启发式近似，不是严格期望信息价值；单字段不变不证明联合查询无价值",
            "查询成本单位不是人民币；初始候选研究开销另计，未核实事实继续保持未知",
        ]
    )
