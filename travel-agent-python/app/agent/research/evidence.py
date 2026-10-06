"""多 Agent 研究的统一契约：研究任务卡与证据包。

职责：
- ResearchTask：Supervisor 分解后派发给各研究 Agent 的输入（谁查、查什么、查多少）；
- EvidencePack：研究 Agent 的唯一输出，是"证据流"的载体——下游整合只消费证据包，
  保证"研究 Agent 只产证据、Supervisor LLM 才是内容来源"的 LLM-only 口径。

实现要点：
- 内部契约用 dataclass（不跨 HTTP 边界），跨层时由调用方序列化为 dict；
- domain 收敛为 Literal，三个域之外的输入在构建任务卡时即被拒绝。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

ResearchDomain = Literal["hotel", "attraction", "food"]

# 已注册的研究域；新增品类 Agent（如"体验/活动"）时在此登记并补 DomainConfig。
RESEARCH_DOMAINS: tuple[ResearchDomain, ...] = ("hotel", "attraction", "food")


@dataclass
class ResearchTask:
    """一次研究任务：Supervisor 发给单个研究 Agent 的指令。"""

    domain: ResearchDomain
    city: str
    preferences: list[str] = field(default_factory=list)
    budget: float | None = None
    hotel_tier: str | None = None
    limit: int = 30
    # M1 意图贯通：仅铺设字段，研究链路暂不消费；从 intent 抽关键词补池
    # （intent_keywords）留 M3 接入。
    intent: str | None = None
    intent_keywords: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.domain not in RESEARCH_DOMAINS:
            raise ValueError(f"未知研究域：{self.domain}")
        if not self.city or not str(self.city).strip():
            raise ValueError("研究任务缺少目的地")


@dataclass
class EvidencePack:
    """研究 Agent 的输出：检索到的证据 + 质量元信息。

    items 沿用现有 POI 契约字段（name/category/coords/ticket_price/source…），
    由 Supervisor 直接映射回 candidates/foods/hotels，下游零改动。
    """

    domain: ResearchDomain
    items: list[dict] = field(default_factory=list)
    confidence: float = 0.0
    rounds: int = 1
    gaps: list[str] = field(default_factory=list)
    degraded: bool = False

    def to_dict(self) -> dict:
        return {
            "domain": self.domain,
            "count": len(self.items),
            "confidence": round(self.confidence, 2),
            "rounds": self.rounds,
            "gaps": list(self.gaps),
            "degraded": self.degraded,
        }
