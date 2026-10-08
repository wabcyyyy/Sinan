"""多 Agent 研究编排离线评测（固定 fixture，不依赖外部模型/网络）。

用法：
    uv run python tests/agent_eval/eval_research.py [--city 杭州]

内容：
- 按域评估三个研究 Agent 的证据质量（规模/置信度/推理轮次/降级/缺口）；
- 验证 Supervisor 缺口补查有效性（首轮景点证据为空 → 补查后证据可合并）。

评测走真实的 run_research / run_research_context / run_refill 链路，
仅把检索工具与 LLM 推理替换为确定性 fixture，结果可复现。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from app.agent.grounding import facts as grounding
from app.agent.research import (
    merge_candidates,
    reasoning,
    run_refill,
    run_research,
    run_research_context,
)
from app.agent.research.evidence import ResearchDomain, ResearchTask
from app.agent.tools import impl as tools
from app.common.config import settings
from app.schemas.trip import GenerateRequest
from tests.agent_eval import mock_llm


def _forbid_network_call(*args, **kwargs):
    """联网入口哨兵：被调用即报错，证明这条评测链路真的离线。"""
    raise AssertionError("离线评测不得发起联网查询")


def _research_patch():
    """研究链路注入：检索工具 + LLM 推理全部替换为确定性 fixture。

    附带离线护栏（G-2.0）：`_run_search` 在证据不足(<3)时会走联网补池，
    仅 mock 检索工具并不足以保证离线——默认开启的 WEB_SEARCH_ENABLED 会让
    报告随网络抖动漂移、CI 上产出另一套数字。这里显式关闭并把联网入口换成
    哨兵，使"离线可复现"成为被证明的事实。
    """
    return (
        patch.object(settings, "web_search_enabled", False),
        patch.object(settings, "llm_generation_web_search", False),
        # 天气（C3.1）同为外网入口：离线评测整体关闭（synthesize 短路）
        patch.object(settings, "weather_enabled", False),
        # 函数内导入，必须 patch 源模块（factory 模块级没有该属性）
        patch("app.agent.data.web_search.search_places_via_web", _forbid_network_call),
        # L14 航班报价两条外呼通道（聚合价/实时价）同为外网入口，一律钉死
        patch("app.agent.data.flight_prices.fetch_price_dates", _forbid_network_call),
        patch("app.agent.data.live_quotes.fetch_live_quotes", _forbid_network_call),
        # LA2 酒店实时价（google_hotels）同为外网入口，一律钉死
        patch("app.agent.data.live_quotes.fetch_hotel_live_quotes", _forbid_network_call),
        patch.object(tools, "search_attractions", mock_llm.search_attractions),
        patch.object(tools, "search_foods", mock_llm.search_foods),
        patch.object(tools, "search_hotels", mock_llm.search_hotels),
        patch.object(tools, "get_consumption", mock_llm.get_consumption),
        patch.object(tools, "search_local_poi", mock_llm.search_local_poi),
        patch.object(grounding, "resolve_poi", mock_llm.resolve_poi),
        patch.object(reasoning, "plan_research", mock_llm.plan_research),
        patch.object(reasoning, "evaluate_research", mock_llm.evaluate_research),
    )


def run_domain_case(domain: ResearchDomain, city: str) -> dict:
    """单个研究域的证据质量快照。"""
    patches = _research_patch()
    for item in patches:
        item.start()
    try:
        pack = run_research(ResearchTask(domain=domain, city=city, limit=30))
    finally:
        for item in reversed(patches):
            item.stop()
    return {"domain": domain, "city": city, **pack.to_dict()}


def run_refill_case(city: str) -> dict:
    """Supervisor 补查有效性：首轮景点证据为空 → 补查后证据可合并。"""
    state = {"calls": 0}

    def search_attractions(city, preferences, limit=30):
        state["calls"] += 1
        if state["calls"] == 1:
            return []
        return [
            {
                "id": 1,
                "name": "补查景点",
                "category": "attraction",
                "latitude": 30.0,
                "longitude": 120.0,
                "ticket_price": 40,
            }
        ]

    patches = [*_research_patch(), patch.object(tools, "search_attractions", search_attractions)]
    for item in patches:
        item.start()
    try:
        req = GenerateRequest(city=city, days=1)
        context = run_research_context(req)
        first_round_empty = not context["candidates"]
        pack = run_refill("attraction", req)
        merged = merge_candidates(context["candidates"], pack.items)
        merged_names = {p.get("name") for p in merged}
        refill_filled = bool(merged) and "补查景点" in merged_names
    finally:
        for item in reversed(patches):
            item.stop()
    return {
        "city": city,
        "first_round_empty": first_round_empty,
        "refill_items": len(pack.items),
        "refill_filled": refill_filled,
    }


def build_report(city: str = "杭州") -> dict:
    domain_names: tuple[ResearchDomain, ...] = ("attraction", "food", "hotel")
    domains = [run_domain_case(domain, city) for domain in domain_names]
    refill = run_refill_case(city)
    total = max(len(domains), 1)
    return {
        "mode": "offline-fixture-research",
        "city": city,
        "domains": domains,
        "refill": refill,
        "metrics": {
            "avg_rounds": round(sum(d["rounds"] for d in domains) / total, 2),
            "pack_total": sum(d["count"] for d in domains),
            "degraded_rate": round(sum(1 for d in domains if d["degraded"]) / total, 2),
            "refill_effectiveness": 1.0 if refill["refill_filled"] else 0.0,
        },
    }


def write_report(report: dict) -> None:
    # 报告分面（PR-0）：offline = mock 离线评测（进指标棘轮），nightly = 真实 LLM
    report_dir = Path(__file__).with_name("report") / "offline"
    report_dir.mkdir(parents=True, exist_ok=True)
    (report_dir / "research_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = [
        "# 多 Agent 研究编排评测报告",
        "",
        f"- 城市：{report['city']}",
        "- 数据模式：固定权威 fixture + 确定性 LLM 推理（不依赖外部服务）",
        "",
        "| 域 | 证据数 | 置信度 | 轮次 | 降级 | 缺口 |",
        "| --- | ---: | ---: | ---: | ---: | --- |",
    ]
    for domain in report["domains"]:
        lines.append(
            f"| {domain['domain']} | {domain['count']} | {domain['confidence']} | "
            f"{domain['rounds']} | {domain['degraded']} | {'；'.join(domain['gaps']) or '-'} |"
        )
    lines.append("")
    lines.append(
        f"- 补查有效性：首轮景点证据为空 = {report['refill']['first_round_empty']}；"
        f"补查后证据可合并 = {report['refill']['refill_filled']}"
    )
    lines.append(
        f"- 指标：平均推理轮次 {report['metrics']['avg_rounds']}，"
        f"证据总量 {report['metrics']['pack_total']}，"
        f"降级率 {report['metrics']['degraded_rate']}，"
        f"补查有效率 {report['metrics']['refill_effectiveness']}"
    )
    (report_dir / "research_report.md").write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--city", default="杭州")
    args = parser.parse_args()
    report = build_report(args.city)
    write_report(report)
    print(json.dumps(report["metrics"], ensure_ascii=False, indent=2))
    print(f"报告已生成：{Path(__file__).with_name('report') / 'offline'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
