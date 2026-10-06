"""离线 Agent 评测入口。

用法：
    uv run python tests/agent_eval/eval_agent.py

评测使用固定权威 fixture 数据 + mock 开放模式输出（fixture_open_day/trip），
不依赖 MySQL、外部 API 或真实 LLM，走真实的编排/引用落地/反思/格式化链路；
报告可以在面试前稳定复现。真实 LLM 评测应在此基础上固定模型/temperature/Prompt 版本另跑。
"""

from __future__ import annotations

import argparse
import json
import sys
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from app.agent.data.route_service import clear_route_cache
from app.agent.generation.orchestration import open_plans, workflow
from app.agent.generation.output import prices as pricing
from app.agent.grounding import facts as grounding
from app.agent.research import reasoning
from app.agent.runtime.trace import trace_run
from app.agent.tools import flight_quotes
from app.agent.tools import impl as tools
from app.common.config import settings
from app.prompts.open_generation import OPEN_DAY_PROMPT_VERSION, OPEN_TRIP_PROMPT_VERSION
from app.schemas.trip import GenerateRequest
from tests.agent_eval import mock_llm
from tests.agent_eval.dataset_schema import load_dataset
from tests.agent_eval.metrics import evaluate_depth, evaluate_response

CASES_PATH = Path(__file__).with_name("cases.json")
# 报告分两面（PR-0）：offline = mock 离线评测（进指标棘轮 + 确定性校验）；
# nightly = 真实 LLM（eval_gate 防倒退，不进棘轮）。互不覆盖。
REPORT_DIR = Path(__file__).with_name("report") / "offline"


def _forbid_network_call(*args, **kwargs):
    """实时价/联网入口的哨兵：被调用即报错，证明这条评测链路真的离线。"""
    raise AssertionError("离线评测不得发起实时价/联网查询")


# 生成契约字段白名单：case 里的 name/prompt_version 是评测元数据，
# 不属于 GenerateRequest；intent 等契约字段按白名单自然透传（M5）。
_REQUEST_FIELDS = frozenset(GenerateRequest.model_fields)


def build_generate_request(case: dict) -> GenerateRequest:
    """case → 生成请求：过滤评测元数据（name/prompt_version），intent 透传给生成链路。"""
    return GenerateRequest(**{k: v for k, v in case.items() if k in _REQUEST_FIELDS})


def with_prompt_version(case: dict) -> dict:
    """运行时填充 prompt_version 占位：报告里的 case 记录实际生成契约版本。"""
    return {**case, "prompt_version": f"{OPEN_DAY_PROMPT_VERSION}/{OPEN_TRIP_PROMPT_VERSION}"}


def run_case(case: dict) -> dict:
    case = with_prompt_version(case)
    # 路线缓存属于进程级优化；每个 fixture 用例先清空，避免前一个城市的
    # 缓存改变本用例的 Trace 工具数和耗时，保证报告可复现。
    clear_route_cache()
    # C3.2 坐标参数化：case 可带 "coords": false 走无坐标边界变体
    coords = case.get("coords", True)
    fixture = mock_llm.catalog(case["city"], coords=coords)
    # LLM-only 口径下的离线评测：mock 开放模式的模型输出（而非旧的
    # 确定性 fallback），走真实的编排/引用落地/反思/格式化链路。
    #
    # 离线护栏（G-2.0）：仅有 fixture 检索还不够——实时价与联网补池默认开启，
    # 会真的去打 LLM 网关（失败被内部吞掉降级），导致报告随环境凭据/网络抖动
    # 漂移、耗时不可控，CI 上更会因无外网而产出另一套数字。这里显式关闭，
    # 并把实时价入口换成"一旦调用即报错"的哨兵：离线不是靠约定，而是被证明。
    # Nominatim 同理（无坐标变体会按名解析真坐标），一律钉死关闭。
    #
    # 用 ExitStack 而非 `with (a, b, ...)`：条目数已越过 CPython 的 20 层静态嵌套
    # 上限（SyntaxError: too many statically nested blocks），拍平后继续加哨兵不会
    # 再撞这个编译期硬墙。
    with ExitStack() as stack:
        enter = stack.enter_context
        enter(patch.object(workflow.settings, "llm_api_key", "fixture"))
        enter(patch.object(settings, "live_price_search", False))
        enter(patch.object(settings, "live_food_price_search", False))
        enter(patch.object(settings, "web_search_enabled", False))
        enter(patch.object(settings, "llm_generation_web_search", False))
        enter(patch.object(settings, "nominatim_enabled", False))
        # 天气（C3.1）同为外网入口：离线评测整体关闭（synthesize 短路，不打坐标/预报）
        enter(patch.object(workflow.settings, "weather_enabled", False))
        enter(patch.object(pricing, "query_live_price", _forbid_network_call))
        enter(patch.object(pricing, "query_live_food_price", _forbid_network_call))
        # L14 航班报价两条外呼通道：聚合价（Aviasales）与实时价（SerpApi）同为
        # 外网入口，一律钉死。评测题集当前不带 origin_city（不带就不查），这两条
        # 哨兵在现有题集上是"装了没用上"；装它的意义是未来任何带出发地的题集都
        # 不会偷偷打真网（离线纪律由哨兵证明，不靠约定）。
        enter(patch.object(flight_quotes.flight_prices, "fetch_price_dates", _forbid_network_call))
        enter(patch.object(flight_quotes.live_quotes, "fetch_live_quotes", _forbid_network_call))
        # LA2 酒店实时价（google_hotels）同为外网入口，一律钉死
        enter(patch("app.agent.data.live_quotes.fetch_hotel_live_quotes", _forbid_network_call))
        enter(
            patch.object(
                tools,
                "search_attractions",
                lambda city, prefs, limit=30: mock_llm.search_attractions(city, prefs, limit, coords=coords),
            )
        )
        enter(
            patch.object(
                tools, "search_foods", lambda city, limit=10: mock_llm.search_foods(city, limit, coords=coords)
            )
        )
        enter(patch.object(tools, "get_consumption", mock_llm.get_consumption))
        enter(
            patch.object(
                tools, "search_hotels", lambda city, limit=6: mock_llm.search_hotels(city, limit, coords=coords)
            )
        )
        enter(patch.object(reasoning, "plan_research", mock_llm.plan_research))
        enter(patch.object(reasoning, "evaluate_research", mock_llm.evaluate_research))
        enter(patch.object(tools, "attach_poi_images", mock_llm.attach_poi_images))
        enter(
            patch.object(
                tools,
                "search_local_poi",
                lambda city, name, category=None: mock_llm.search_local_poi(
                    city, name, category=category, coords=coords
                ),
            )
        )
        # 存在性判定替身（D11A）：接地走这里，不再隐式拿 fixture 目录自证
        enter(
            patch.object(grounding, "resolve_poi", lambda name, city: mock_llm.resolve_poi(name, city, coords=coords))
        )
        enter(patch.object(open_plans, "llm_open_day", mock_llm.fixture_open_day))
        enter(patch.object(open_plans, "llm_open_trip", mock_llm.fixture_open_trip))
        recorder = enter(trace_run(f"fixture-{case['city']}-{case['days']}"))
        response = workflow.run_generate(build_generate_request(case))
    result = evaluate_response(response, case, fixture, recorder.to_dict())
    result.update(evaluate_depth(response, case))
    return result


def _average(results: list[dict], key: str) -> float:
    return round(sum(result[key] for result in results) / max(len(results), 1), 4)


def build_report(cases: list[dict], dataset: dict | None = None) -> dict:
    results = [run_case(case) for case in cases]
    return {
        "mode": "offline-fixture-fallback",
        "case_count": len(results),
        # 数据集指纹（PR-0）：评测结论可回溯到"哪份题"（此前只记 prompt_version）
        "dataset": dataset,
        "metrics": {
            "poi_authority_rate": _average(results, "poi_authority_rate"),
            "poi_grounded_rate": _average(results, "poi_grounded_rate"),
            "poi_refuted_count": sum(int(r.get("poi_refuted_count") or 0) for r in results),
            "field_reference_rate": _average(results, "field_reference_rate"),
            "time_conflict_rate": _average(results, "time_conflict_rate"),
            "route_violation_rate": _average(results, "route_violation_rate"),
            "attraction_duplicate_rate": _average(results, "attraction_duplicate_rate"),
            "budget_deviation_rate": _average(results, "budget_deviation_rate"),
            "success_status_rate": round(sum(r["status"] == "success" for r in results) / max(len(results), 1), 4),
            "degraded_status_rate": round(sum(r["status"] == "degraded" for r in results) / max(len(results), 1), 4),
            "failed_status_rate": round(sum(r["status"] == "failed" for r in results) / max(len(results), 1), 4),
            "fallback_success_rate": round(sum(r["fallback_success"] for r in results) / max(len(results), 1), 4),
            "trace_complete_rate": round(
                sum(
                    set(r["trace"]["nodes"]) >= {"parse", "research", "generate", "reflect", "format"}
                    and r["trace"]["tool_count"] >= 2
                    for r in results
                )
                / max(len(results), 1),
                4,
            ),
            "research_rounds_avg": _average(results, "research_rounds"),
            "research_pack_avg": _average(results, "research_pack"),
            # C3.2 深度指标：坐标有效率 / 深链可解析率 / 类目合理率
            "coord_valid_rate": _average(results, "coord_valid_rate"),
            "deeplink_resolvable_rate": _average(results, "deeplink_resolvable_rate"),
            "category_reasonable_rate": _average(results, "category_reasonable_rate"),
        },
        "details": results,
    }


def write_report(report: dict) -> None:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    (REPORT_DIR / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    metrics = report["metrics"]
    lines = [
        "# Agent 离线评测报告",
        "",
        "- 数据模式：固定权威 fixture + fallback 生成器（不依赖外部服务）",
        f"- 用例数：{report['case_count']}",
    ]
    dataset = report.get("dataset")
    if dataset:
        lines.append(f"- 数据集：{dataset['file']}（sha256 {dataset['sha256'][:12]}…）")
    lines += [
        "",
        "| 指标 | 结果 |",
        "| --- | ---: |",
    ]
    for key, value in metrics.items():
        # 均值型指标（research_rounds_avg 等）不是比例，按数值渲染而非百分比
        if key.endswith("_avg"):
            lines.append(f"| {key} | {value:g} |")
        else:
            lines.append(f"| {key} | {value:.2%} |")
    lines += [
        "",
        "| 城市 | 天数 | 权威 POI | 字段引用 | 冲突率 | 路线违规 | 重复率 | 预算偏差 | 轨迹工具数 |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for result in report["details"]:
        case = result["case"]
        lines.append(
            f"| {case['city']} | {case['days']} | {result['poi_authority_rate']:.2%} | "
            f"{result['field_reference_rate']:.2%} | {result['time_conflict_rate']:.2%} | "
            f"{result['route_violation_rate']:.2%} | "
            f"{result['attraction_duplicate_rate']:.2%} | {result['budget_deviation_rate']:.2%} | "
            f"{result['trace']['tool_count']} |"
        )
    (REPORT_DIR / "report.md").write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=0, help="只运行前 N 个用例")
    parser.add_argument(
        "--cases", default=str(CASES_PATH), help="用例文件：默认 cases.json；主题化同题评测传 themed_cases.json"
    )
    args = parser.parse_args()
    cases_path = Path(args.cases)
    # 数据集过 schema + 指纹（PR-0 数据集治理）：错题在加载点拦下，指纹随报告落盘
    cases, dataset = load_dataset(cases_path)
    report = build_report(cases[: args.limit] if args.limit else cases, dataset=dataset)
    write_report(report)
    print(json.dumps(report["metrics"], ensure_ascii=False, indent=2))
    print(f"报告已生成：{REPORT_DIR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
