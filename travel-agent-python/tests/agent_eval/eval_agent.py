"""离线 Agent 评测入口。

用法：
    uv run python tests/agent_eval/eval_agent.py               # 产品路径（默认，进指标棘轮）
    uv run python tests/agent_eval/eval_agent.py --path graph  # 统一图 trip 分支（Agent 面）

评测使用固定权威 fixture 数据 + mock 开放模式输出（fixture_open_day/trip），
不依赖 MySQL、外部 API 或真实 LLM；报告可稳定复现。两条路径（2026-10-12 起默认 stream）：

- `stream`：用户实际走的业务层链路——`itinerary_generation.generate` 建壳、`plan_days`
  研究 → 整段流式 → 落库天终检 → 违规/缺失天逐日补齐，全部在临时 SQLite 上真跑，
  指标按**落库后读回的交付结果**计算（见 product_path.py）。报告落 report/offline/report.*；
- `graph`：统一图 trip 分支（parse → research → generate → reflect → format），只服务
  Agent 面 /v1/generate；报告落 report/offline/report_graph.*，不进指标棘轮。

真实 LLM 评测见 llm_eval.py（固定模型/temperature/Prompt 版本另跑）。
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
from app.agent.generation.orchestration import day_stream, open_plans, workflow
from app.agent.generation.output import prices as pricing
from app.agent.grounding import facts as grounding
from app.agent.research import reasoning
from app.agent.runtime.trace import trace_run
from app.agent.tools import flight_quotes
from app.agent.tools import impl as tools
from app.common import llm_client, redis_client
from app.common.config import settings
from app.prompts.open_generation import OPEN_DAY_PROMPT_VERSION, OPEN_TRIP_PROMPT_VERSION
from app.schemas.trip import GenerateRequest
from tests.agent_eval import mock_llm, product_path
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


def _enter_offline_mocks(stack: ExitStack, coords: bool) -> None:
    """两条评测路径共用的离线护栏与 mock（检索 fixture、mock 开放模式输出、外网哨兵）。

    离线护栏（G-2.0）：仅有 fixture 检索还不够——实时价与联网补池默认开启，
    会真的去打 LLM 网关（失败被内部吞掉降级），导致报告随环境凭据/网络抖动
    漂移、耗时不可控，CI 上更会因无外网而产出另一套数字。这里显式关闭，
    并把实时价入口换成"一旦调用即报错"的哨兵：离线不是靠约定，而是被证明。
    Nominatim 同理（无坐标变体会按名解析真坐标），一律钉死关闭。
    Redis 指向不可达地址走进程内降级：本机若跑着 Redis，证据票/缓存会跨次运行串味。

    用 ExitStack 而非 `with (a, b, ...)`：条目数已越过 CPython 的 20 层静态嵌套
    上限（SyntaxError: too many statically nested blocks），拍平后继续加哨兵不会
    再撞这个编译期硬墙。
    """
    enter = stack.enter_context
    enter(patch.object(settings, "redis_url", "redis://127.0.0.1:1/0"))
    redis_client.reset_for_tests()
    stack.callback(redis_client.reset_for_tests)
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
            tools,
            "search_foods",
            lambda city, limit=10, dietary=None: mock_llm.search_foods(city, limit, dietary=dietary, coords=coords),
        )
    )
    enter(patch.object(tools, "get_consumption", mock_llm.get_consumption))
    enter(
        patch.object(tools, "search_hotels", lambda city, limit=6: mock_llm.search_hotels(city, limit, coords=coords))
    )
    enter(patch.object(reasoning, "plan_research", mock_llm.plan_research))
    enter(patch.object(reasoning, "evaluate_research", mock_llm.evaluate_research))
    enter(patch.object(tools, "attach_poi_images", mock_llm.attach_poi_images))
    enter(
        patch.object(
            tools,
            "search_local_poi",
            lambda city, name, category=None: mock_llm.search_local_poi(city, name, category=category, coords=coords),
        )
    )
    # 存在性判定替身（D11A）：接地走这里，不再隐式拿 fixture 目录自证
    enter(patch.object(grounding, "resolve_poi", lambda name, city: mock_llm.resolve_poi(name, city, coords=coords)))
    enter(patch.object(open_plans, "llm_open_day", mock_llm.fixture_open_day))
    enter(patch.object(open_plans, "llm_open_trip", mock_llm.fixture_open_trip))
    # 逐日补齐（day 分支）经 day_stream 自己的模块绑定取 llm_open_day，与整段腿不是同一个名字
    enter(patch.object(day_stream, "llm_open_day", mock_llm.fixture_open_day))


class LlmLeak(AssertionError):
    """离线评测链路里出现了真实 LLM HTTP 调用。"""


def _guard_llm_http(stack: ExitStack) -> list[str]:
    """LLM HTTP 出口哨兵：任何真实调用都记账并抛错。

    只抛错不够——生成链会把 LLM 失败吞掉、降级成兜底结果，报告照样出数（2026-10-12
    实测：逐日补齐漏 mock 时打了真网关，拿到 401 后降级，报告看不出来）。所以另记一本账，
    用例结束后由 run_case 断言为空。
    """
    leaks: list[str] = []

    def refuse(*_args, **_kwargs):
        leaks.append("llm_client._get_http_client")
        raise LlmLeak("离线评测不得发起真实 LLM 调用")

    stack.enter_context(patch.object(llm_client, "_get_http_client", refuse))
    return leaks


def run_case(case: dict, path: str = "stream") -> dict:
    """跑一个用例。path=stream（默认）= 产品路径：业务层 plan_days 真跑在临时库上，
    指标按落库后读回的交付结果算；path=graph = 统一图 trip 分支（Agent 面 /v1/generate）。"""
    case = with_prompt_version(case)
    # 路线缓存属于进程级优化；每个 fixture 用例先清空，避免前一个城市的
    # 缓存改变本用例的 Trace 工具数和耗时，保证报告可复现。
    clear_route_cache()
    # C3.2 坐标参数化：case 可带 "coords": false 走无坐标边界变体
    coords = case.get("coords", True)
    run_id = f"fixture-{case['city']}-{case['days']}"
    # LLM-only 口径下的离线评测：mock 开放模式的模型输出（而非旧的
    # 确定性 fallback），其余编排/引用落地/终检链路真实执行。
    with ExitStack() as stack:
        _enter_offline_mocks(stack, coords)
        leaks = _guard_llm_http(stack)
        # 目录在护栏之后建：它会签发证据票，必须落进本次评测所用的缓存
        fixture = mock_llm.catalog(case["city"], coords=coords)
        if path == "stream":
            response, trace = product_path.run_product_path(case, run_id)
        else:
            recorder = stack.enter_context(trace_run(run_id))
            response = workflow.run_generate(build_generate_request(case))
            trace = recorder.to_dict()
        # 票据和指标必须共用本次离线缓存；退出 mock 后可能切回真实 Redis。
        result = evaluate_response(response, case, fixture, trace)
        result.update(evaluate_depth(response, case))
    if leaks:
        raise LlmLeak(f"{case['city']} {case['days']} 天：评测链路发起了 {len(leaks)} 次真实 LLM 调用（有入口漏 mock）")
    return result


def _average(results: list[dict], key: str) -> float | None:
    values = [result[key] for result in results if result[key] is not None]
    return round(sum(values) / len(values), 4) if values else None


#: trace_complete 的节点口径按路径分：产品路径的生成腿是 stream 分支（研究段在生产里
#: 不包 run，不产 node 事件），逐日补齐天另有 day 分支节点；图路径是 trip 分支五节点
_REQUIRED_NODES = {
    "stream": {"stream_generate"},
    "graph": {"parse", "research", "generate", "reflect", "format"},
}
_MODES = {"stream": "offline-fixture-product-path", "graph": "offline-fixture-trip-graph"}


def build_report(cases: list[dict], dataset: dict | None = None, path: str = "stream") -> dict:
    results = [run_case(case, path) for case in cases]
    required_nodes = _REQUIRED_NODES[path]
    return {
        "mode": _MODES[path],
        "generation_path": path,
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
            # 交付后复检：对最终交付的天再跑一遍终检判官。stream 路径上生产已经终检并
            # 逐日补齐过，这个率 = 补齐后仍不合规的天占比；graph 路径 = 生产会重置几成天。
            "terminal_reset_day_rate": _average(results, "terminal_reset_day_rate"),
            "success_status_rate": round(sum(r["status"] == "success" for r in results) / max(len(results), 1), 4),
            "degraded_status_rate": round(sum(r["status"] == "degraded" for r in results) / max(len(results), 1), 4),
            "failed_status_rate": round(sum(r["status"] == "failed" for r in results) / max(len(results), 1), 4),
            "fallback_success_rate": round(sum(r["fallback_success"] for r in results) / max(len(results), 1), 4),
            "trace_complete_rate": round(
                sum(set(r["trace"]["nodes"]) >= required_nodes and r["trace"]["tool_count"] >= 2 for r in results)
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
    stem = "report" if report.get("generation_path", "stream") == "stream" else "report_graph"
    (REPORT_DIR / f"{stem}.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    metrics = report["metrics"]
    path_label = {
        "stream": "产品路径（业务层 plan_days：研究 → 整段流式 → 落库终检 → 逐日补齐，临时 SQLite）",
        "graph": "统一图 trip 分支（Agent 面 /v1/generate）",
    }[report.get("generation_path", "stream")]
    lines = [
        "# Agent 离线评测报告",
        "",
        f"- 生成路径：{path_label}",
        "- 数据模式：固定权威 fixture + mock 开放模式输出（不依赖外部服务）",
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
        field_reference = result["field_reference_rate"]
        field_text = f"{field_reference:.2%}" if field_reference is not None else "-"
        lines.append(
            f"| {case['city']} | {case['days']} | {result['poi_authority_rate']:.2%} | "
            f"{field_text} | {result['time_conflict_rate']:.2%} | "
            f"{result['route_violation_rate']:.2%} | "
            f"{result['attraction_duplicate_rate']:.2%} | {result['budget_deviation_rate']:.2%} | "
            f"{result['trace']['tool_count']} |"
        )
    (REPORT_DIR / f"{stem}.md").write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=0, help="只运行前 N 个用例")
    parser.add_argument(
        "--cases", default=str(CASES_PATH), help="用例文件：默认 cases.json；主题化同题评测传 themed_cases.json"
    )
    parser.add_argument(
        "--path",
        choices=("stream", "graph"),
        default="stream",
        help="stream=产品路径（业务层 plan_days，进棘轮）；graph=统一图 trip 分支（Agent 面）",
    )
    args = parser.parse_args()
    cases_path = Path(args.cases)
    # 数据集过 schema + 指纹（PR-0 数据集治理）：错题在加载点拦下，指纹随报告落盘
    cases, dataset = load_dataset(cases_path)
    report = build_report(cases[: args.limit] if args.limit else cases, dataset=dataset, path=args.path)
    write_report(report)
    print(json.dumps(report["metrics"], ensure_ascii=False, indent=2))
    print(f"报告已生成：{REPORT_DIR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
