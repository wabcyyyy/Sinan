"""多 Agent 研究包：领域研究 Agent + Supervisor 整合生成（LLM-only 口径）。

对外入口：
- run_research(task)：执行单个研究域的一次研究，返回 EvidencePack；
- run_research_context(req)：Supervisor 一站式（分解→并行→整合），
  供 workflow.search 与 plan-context 消费，输出形状与旧 search_pois 一致；
- run_refill(domain, req)：Supervisor 缺口补查（reflect 校验驱动）；
- merge_candidates(existing, supplement)：补查证据按名称去重合并。
"""

from app.agent.research.evidence import EvidencePack, ResearchTask
from app.agent.research.factory import run_research
from app.agent.research.supervisor import (
    decompose,
    merge_candidates,
    run_refill,
    run_research_context,
    run_research_parallel,
    synthesize,
)

__all__ = [
    "EvidencePack",
    "ResearchTask",
    "decompose",
    "merge_candidates",
    "run_refill",
    "run_research",
    "run_research_context",
    "run_research_parallel",
    "synthesize",
]
