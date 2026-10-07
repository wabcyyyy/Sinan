"""可复现的路线/优化器消融评测。

Baseline A 使用坐标生成候选，Baseline B 使用注入的固定路线服务替身并启用
Schedule Optimizer。路线替身不是线上高德数据，报告明确标记为 fixture-route，
用于验证编排和指标口径，而不是冒充真实交通效果。

用法：
    .venv\\Scripts\\python.exe tests/agent_eval/eval_baselines.py
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from itertools import pairwise
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from app.agent.data.route_service import RouteService, clear_route_cache
from app.agent.generation.content import generators
from app.agent.generation.content.reflect import _route_from_matrix, validate_plans
from app.agent.generation.output import schedule_optimizer
from app.agent.generation.rules.transfer_time import item_end, item_start
from app.common.config import settings
from tests.agent_eval import mock_llm
from tests.agent_eval.dataset_schema import load_dataset

CASES_PATH = Path(__file__).with_name("cases.json")
REPORT_DIR = Path(__file__).with_name("report") / "offline"


def _fixture_fetcher(first: dict, second: dict, mode: str, departure: str | None) -> dict:
    """固定路线替身：景点 1↔2 模拟跨区域长距离，其余边为短途。"""
    first_id = str(first.get("poi_id") or first.get("id") or "")
    second_id = str(second.get("poi_id") or second.get("id") or "")
    duration = 180 if {first_id, second_id} == {"1", "2"} else 30
    return {
        "from_poi_id": first_id,
        "to_poi_id": second_id,
        "mode": mode,
        "distance_m": duration * 40,
        "duration_min": duration,
        "source": "fixture-route",
        "confidence": 0.9,
        "degraded": False,
    }


def _route_violation_counts(plans: list[dict], matrix: dict) -> tuple[int, int]:
    violated = 0
    pairs = 0
    for plan in plans:
        active = sorted(
            [item for item in plan.get("items") or [] if item.get("item_type") in ("attraction", "food")],
            key=item_start,
        )
        for previous, following in pairwise(active):
            pairs += 1
            route = _route_from_matrix(previous, following, matrix)
            required = int((route or {}).get("duration_min") or 0)
            if required and item_start(following) - item_end(previous) < required:
                violated += 1
    return violated, pairs


def _run_variant(case: dict, *, optimizer_enabled: bool, route: RouteService) -> dict:
    catalog = mock_llm.catalog(case["city"])
    attractions = catalog["attractions"]
    foods = catalog["foods"]
    hotels = catalog["hotels"]
    consumption = catalog["consumption"]
    _route_matrix = route.matrix(attractions[: min(len(attractions), 7)], mode="walking")
    started = time.perf_counter()
    with (
        patch.object(settings, "schedule_optimizer_enabled", optimizer_enabled),
        patch.object(settings, "route_service_enabled", optimizer_enabled),
        patch.object(schedule_optimizer, "default_route_service", route),
    ):
        plans, _ = generators.fallback_generate(
            case["city"],
            case["days"],
            case["persons"],
            case.get("preferences") or [],
            hotels=hotels,
            attractions=attractions,
            foods=foods,
            consumption=consumption,
            budget_limit=None,
        )
    duration_ms = round((time.perf_counter() - started) * 1000, 2)
    # 使用同一条 fixture-route 作为验收真值，确保 A 的“坐标合理”不会被误报为
    # 真实交通通过；B 的优化结果必须在相同真值下复核。
    active = [
        item for plan in plans for item in plan.get("items") or [] if item.get("item_type") in ("attraction", "food")
    ]
    truth_matrix = route.matrix(active, mode="walking")
    issues, _ = validate_plans(plans, route_matrix=truth_matrix)
    violated, pairs = _route_violation_counts(plans, truth_matrix)
    return {
        "initial_or_final_pass": not issues,
        "issue_count": len(issues),
        "route_violations": violated,
        "route_pairs": pairs,
        "duration_ms": duration_ms,
        "item_count": sum(len(plan.get("items") or []) for plan in plans),
    }


def build_report(cases: list[dict], dataset: dict | None = None) -> dict:
    variants = {}
    for name, enabled in (("baseline_a_coordinate", False), ("baseline_b_route_optimizer", True)):
        rows = []
        for case in cases:
            clear_route_cache()
            route = RouteService(fetcher=_fixture_fetcher)
            rows.append({"case": case, **_run_variant(case, optimizer_enabled=enabled, route=route)})
        total_pairs = sum(row["route_pairs"] for row in rows)
        variants[name] = {
            "mode": "fixture-route" if enabled else "coordinate-generated / fixture-route-evaluated",
            "case_count": len(rows),
            "initial_pass_rate" if not enabled else "final_pass_rate": round(
                sum(row["initial_or_final_pass"] for row in rows) / max(len(rows), 1), 4
            ),
            "route_violation_rate": round(sum(row["route_violations"] for row in rows) / max(total_pairs, 1), 4),
            # 两个 variant 都明确使用 deterministic fallback，不能解读为真实 LLM
            # fallback 率；这个字段只记录评测执行模式。
            "fallback_rate": 1.0,
            "average_duration_ms": round(sum(row["duration_ms"] for row in rows) / max(len(rows), 1), 2),
            "details": rows,
        }
    return {
        "mode": "offline-ablation",
        "route_provider": "fixture-route (deterministic substitute, not live traffic)",
        # 数据集指纹（PR-0）：与 eval_agent 同款，评测结论可回溯到"哪份题"
        "dataset": dataset,
        "variants": variants,
    }


def write_report(report: dict) -> None:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    (REPORT_DIR / "baseline_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = [
        "# 路线与优化器离线消融报告",
        "",
        "> 路线数据来自固定 `fixture-route` 替身，不代表线上高德交通效果。",
        "",
        "| 方案 | 首次/最终通过率 | 路线违规率 | fallback 率（执行模式） | 平均耗时 ms |",
        "| --- | ---: | ---: | ---: | ---: |",
    ]
    for name, result in report["variants"].items():
        pass_key = "initial_pass_rate" if "initial_pass_rate" in result else "final_pass_rate"
        lines.append(
            f"| {name} | {result[pass_key]:.2%} | {result['route_violation_rate']:.2%} | "
            f"{result['fallback_rate']:.2%} | {result['average_duration_ms']:.2f} |"
        )
    (REPORT_DIR / "baseline_report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=0)
    args = parser.parse_args()
    cases, dataset = load_dataset(CASES_PATH)
    report = build_report(cases[: args.limit] if args.limit else cases, dataset=dataset)
    write_report(report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
