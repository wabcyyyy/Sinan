"""真实 LLM 评测入口。

该脚本不会伪造“真实评测”结果：没有 LLM_API_KEY 时直接退出；运行时记录模型、固定
temperature、知识库城市和两遍结果签名，供分析稳定性、反思重试和 token 成本。

两条生成路径，用 `--path` 选（默认 `stream`）：

- `stream`：**用户在产品里走的那条**——`run_plan_context` 先做研究，然后
  `run_generate_trip_stream` 逐日产事件，评测把 `day`/`day_patch`/`suggestions`/`done`
  收割回 `GenerateResponse` 再算指标。与研究"分开包 run"的形状是刻意复刻的：生产里
  `run_plan_context` 就不在 `observe_run` 内（`app/services/itinerary_generation.py`
  注 3），所以研究不受 RunLimits/deadline 记账，生成有自己的一份 90s。
  因此报告里 `research_rounds` / `research_pack` 恒为 0：那是"研究不在这个 run 里"
  的事实，不是丢数。
- `graph`：同步 `/v1/generate` 的一张图（研究+生成+反思+落地共用一个 run 与一份
  预算）。它是遗留面，但仍是公开入口，所以留着随时可测。
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from app.agent.generation.content.day_prompts import GENERATION_TEMPERATURE
from app.agent.generation.orchestration import workflow
from app.agent.generation.orchestration.plan_context import run_plan_context
from app.agent.generation.orchestration.stream_branch import run_generate_trip_stream
from app.agent.generation.rules.generation_core import estimate_plans_total, stay_nights
from app.agent.runtime.observability import observe_run
from app.agent.tools import impl as tools
from app.common.config import settings
from app.prompts.open_generation import OPEN_DAY_PROMPT_VERSION, OPEN_TRIP_PROMPT_VERSION
from app.schemas.trip import DailyPlan, GenerateDayRequest, GenerateRequest, GenerateResponse, Suggestion
from tests.agent_eval.dataset_schema import load_dataset
from tests.agent_eval.metrics import evaluate_depth, evaluate_narrative, evaluate_response
from tests.agent_eval.prompt_digest import prompt_text_sha256

CASES_PATH = Path(__file__).with_name("cases.json")
THEMED_CASES_PATH = Path(__file__).with_name("themed_cases.json")
# 报告分面（PR-0）：真实 LLM 产物落 nightly/（eval_gate 消费），与 mock 离线基线
# （report/offline/，进指标棘轮）分开——nightly 重跑不得污染基线目录。
REPORT_DIR = Path(__file__).with_name("report") / "nightly"
PROMPT_VERSION = "workflow-v2-authority-route-20260828"

# 生成契约字段白名单：case 里的 name/prompt_version 是评测元数据，
# 不属于 GenerateRequest；intent 等契约字段按白名单自然透传（M5）。
_REQUEST_FIELDS = frozenset(GenerateRequest.model_fields)


def build_generate_request(case: dict) -> GenerateRequest:
    """case → 生成请求：过滤评测元数据（name/prompt_version），intent 透传给生成链路。"""
    return GenerateRequest(**{k: v for k, v in case.items() if k in _REQUEST_FIELDS})


def _catalog(city: str) -> dict:
    # 语料库退役：真机评测的候选目录走同一套外部检索层（OTM+联网补池）
    return {
        "attractions": tools.search_attractions(city, [], 200),
        "foods": tools.search_foods(city, 50),
        "hotels": tools.search_hotels(city, 30),
        "consumption": tools.get_consumption(city) or {},
    }


def _signature(response) -> str:
    return "|".join(
        ">".join(item.poi_name for item in plan.items if item.item_type == "attraction")
        for plan in response.daily_plans
    )


def _trace_stats(trace: dict) -> dict:
    events = trace.get("events") or []
    prompt_tokens = sum(
        int((event.get("metadata") or {}).get("prompt_tokens") or 0)
        for event in events
        if event.get("name") == "llm.request"
    )
    completion_tokens = sum(
        int((event.get("metadata") or {}).get("completion_tokens") or 0)
        for event in events
        if event.get("name") == "llm.request"
    )
    return {
        "llm_calls": sum(event.get("kind") == "llm" and event.get("name") == "llm.request" for event in events),
        "tool_calls": sum(event.get("kind") == "tool" for event in events),
        "retry_count": sum(event.get("kind") == "route" and event.get("name") in ("fix", "retry") for event in events),
        "fallback_count": sum(event.get("kind") == "route" and event.get("name") == "fallback" for event in events),
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": prompt_tokens + completion_tokens,
    }


def _run_record(case: dict, catalog: dict, response, trace_data: dict) -> tuple[dict, str]:
    """一次运行的记录：两条路径都归一到同一个 response 形状，指标口径因此可比。"""
    run = {
        "run_id": trace_data["run_id"],
        "status": response.status,
        "status_reason": response.status_reason,
        "stats": _trace_stats(trace_data),
        "trace": trace_data,
        "quality": evaluate_response(response, case, catalog, trace_data),
        # M5 叙事化指标：与质量指标并列落报告，主题化评测的核心观测面
        "narrative": evaluate_narrative(response, case),
        # C3.2 深度指标：坐标有效率/深链可解析率/类目合理率
        "depth": evaluate_depth(response, case),
    }
    return run, _signature(response)


def _failed_record(run_id: str, trace_data: dict, failure: BaseException) -> tuple[dict, str]:
    return {
        "run_id": run_id,
        "status": "failed",
        "status_reason": str(failure)[:200],
        "stats": _trace_stats(trace_data),
        "trace": trace_data,
        "quality": None,
        "narrative": None,
    }, ""


def _run_graph_once(case: dict, suffix: str, catalog: dict) -> tuple[dict, str]:
    """同步图路径：研究 + 生成 + 反思 + 落地共用一个 run、一份预算。"""
    response = None
    failure: Exception | None = None
    with observe_run(f"llm-{case['city']}-{case['days']}-{suffix}") as trace:
        try:
            response = workflow.run_generate(build_generate_request(case))
        except Exception as exc:
            failure = exc
    trace_data = trace.to_dict()
    if failure is not None:
        return _failed_record(trace_data["run_id"], trace_data, failure)
    assert response is not None
    return _run_record(case, catalog, response, trace_data)


def _stream_request(case: dict, context: dict) -> GenerateDayRequest:
    """case + 研究上下文 → 整段流式请求（字段对齐 `itinerary_generation._plan_whole_trip`）。

    刻意不自己发明形状：产品怎么拼这个请求，评测就怎么拼，否则量到的不是同一条路。
    """
    days = max(int(case.get("days") or 1), 1)
    return GenerateDayRequest(
        city=case["city"],
        persons=case.get("persons", 1),
        budget=case.get("budget"),
        start_date=case.get("start_date"),
        day_no=1,
        days=days,
        used_names=[],
        hotel_tier=case.get("hotel_tier"),
        chosen_hotel=None,
        needs_hotel=stay_nights(days) > 0,
        requirements=case.get("requirements"),
        intent=case.get("intent"),
        region_hint=case.get("region_hint"),
        request_id=f"eval-{case['city']}-{days}d",
        context=context,
    )


def _harvest_stream(
    events: list[dict],
) -> tuple[list[DailyPlan], list[Suggestion], dict | None, str | None]:
    """把 NDJSON 事件收成（逐日计划, 备选池, done, error）。

    `day_patch` 按 day_no 覆盖同一天（酒店摊铺等确定性修补的语义，与服务层落库一致）。
    """
    by_day_no: dict[int, DailyPlan] = {}
    suggestions: list[Suggestion] = []
    done: dict | None = None
    error: str | None = None
    for event in events:
        event_type = str(event.get("type") or "")
        if event_type in ("day", "day_patch"):
            plan = event.get("plan")
            if plan:
                # 走一遍真模型：流里出的东西必须过 DailyPlan 契约，否则评测替产品吞了脏数据
                parsed = DailyPlan.model_validate(plan)
                by_day_no[int(parsed.day_no)] = parsed
        elif event_type == "suggestions":
            # 同上：备选池条目也过一遍契约（它带着 poiId/坐标，是前端打点的输入）
            suggestions = [Suggestion.model_validate(row) for row in event.get("items") or []]
        elif event_type == "done":
            done = event
        elif event_type == "error":
            error = str(event.get("message") or "流式生成报错")
    return list(by_day_no.values()), suggestions, done, error


def _stream_status(
    plans: list[DailyPlan], days: int, done: dict | None, error: str | None
) -> tuple[Literal["success", "degraded", "failed"], str | None]:
    """流式路径的状态口径（graph 路径由图给，这里必须自己定，所以要写明）。

    - error 事件或一天都没出 → `failed`（没有任何可交付，不冒充降级成功）；
    - 没有 done / 天数不齐 / 有天空点位 → `degraded`（如实说明缺哪一段）；
    - 其余 → `success`。
    """
    if error:
        return "failed", error
    if not plans:
        return "failed", "流式未产出任何一天"
    empty_days = [int(plan.day_no) for plan in plans if not plan.items]
    if done is None:
        return "degraded", f"流式未收到 done 事件（已产出 {len(plans)}/{days} 天）"
    if not done.get("complete") or len(plans) < days:
        return "degraded", str(done.get("message") or f"仅产出 {len(plans)}/{days} 天")
    if empty_days:
        return "degraded", f"第 {'、'.join(str(day) for day in empty_days)} 天没有可交付点位"
    return "success", None


def _run_stream_once(case: dict, suffix: str, catalog: dict) -> tuple[dict, str]:
    """产品同构的一次流式生成：研究在 run 外、生成在自己的 run 内（预算各一份）。

    与 `itinerary_generation` 一致：研究炸了就没有可生成的上下文，此时如实记一条
    failed（带合成 run_id，不假装有 run），而不是让评测抛异常。
    """
    city = case["city"]
    days = max(int(case.get("days") or 1), 1)
    run_id = f"llm-{city}-{days}-{suffix}-stream"
    empty_trace = {"run_id": run_id, "events": []}
    try:
        context = run_plan_context(
            city,
            case.get("preferences") or [],
            start_date=case.get("start_date"),
            days=days,
        )
    except Exception as exc:
        return _failed_record(run_id, empty_trace, exc)

    request = _stream_request(case, context)
    failure: Exception | None = None
    events: list[dict] = []
    with observe_run(run_id) as trace:
        try:
            events = [dict(event) for event in run_generate_trip_stream(request)]
        except Exception as exc:
            failure = exc
    trace_data = trace.to_dict()
    if failure is not None:
        return _failed_record(trace_data["run_id"], trace_data, failure)
    plans, suggestions, done, error = _harvest_stream(events)
    status, reason = _stream_status(plans, days, done, error)
    response = GenerateResponse(
        city=city,
        days=days,
        title=str((done or {}).get("trip_theme") or f"{city}{days}日行程"),
        trip_theme=(done or {}).get("trip_theme"),
        daily_plans=plans,
        suggestions=suggestions,
        budget_estimate=estimate_plans_total(
            [plan.model_dump() for plan in plans], case.get("persons", 1), days, context.get("consumption")
        ),
        # 研究发生在这个 run 之外（与服务一致），所以没有 schedule_report 可带：
        # 报告里 research_rounds/research_pack 为 0 是这条路径的事实，不是丢数。
        schedule_report={},
        destination_status="researched" if context.get("candidates") else "draft_only",
        status=status,
        status_reason=reason,
    )
    return _run_record(case, catalog, response, trace_data)


def _run_once(case: dict, suffix: str, path: str) -> tuple[dict, str]:
    catalog = _catalog(case["city"])
    runner = _run_stream_once if path == "stream" else _run_graph_once
    return runner(case, suffix, catalog)


def _report_markdown(report: dict) -> str:
    """评测报告的 Markdown 渲染：每 case 一节（固定运行头部 + 指标表）。

    头部固定 model/temperature/两套开放生成 Prompt 版本，指标表合并
    evaluate_response 的质量指标与 evaluate_narrative 的叙事指标；
    无 intent 的基线 case 的主题命中类指标为 None，渲染为 "-"。
    A2 修订：**两种模式都写 .md**（此前 `if themed:` 使非主题化 run 没有 md 产物）。
    """
    themed = report.get("mode") == "real-llm-themed"
    lines = [
        "# 主题化评测报告（真实 LLM）" if themed else "# 真实 LLM 评测报告",
        "",
        f"- 生成路径：`{report.get('generation_path', 'graph')}` ｜ model：`{report['model']}` ｜ "
        f"temperature：{report['temperature']} ｜ "
        f"open_day prompt：`{report['open_day_prompt_version']}` ｜ "
        f"open_trip prompt：`{report['open_trip_prompt_version']}`",
        f"- 用例数：{report['case_count']} ｜ 两遍一致率：{report['consistency_rate']:.2%}",
        "- 两个口径别混读：`route_violation_rate` 量的是**业务终检前**的中间产物"
        "（评测只调 run_generate_trip_stream，不跑落库后的 validate_plans）；"
        "`terminal_reset_day_rate` 用生产同一判官算「生产会重置几成天」——"
        "那才是用户实际会撞到的缺口信号（重置天要重烧一次 LLM，或最终落 PENDING）。",
        "",
    ]
    for detail in report["details"]:
        case = detail["case"]
        run1 = detail["run1"]
        run2 = detail["run2"]
        quality = run1.get("quality") or {}
        narrative = run1.get("narrative") or {}
        depth = run1.get("depth") or {}
        title = case.get("name") or f"{case['city']}-{case['days']}d"
        lines += [
            f"## {title}（{case['city']} {case['days']} 日）",
            "",
            f"- prompt_version：`{case.get('prompt_version')}` ｜ "
            f"run1 status：{run1['status']} ｜ run2 status：{run2['status']} ｜ "
            f"两遍一致：{'是' if detail['consistent'] else '否'}",
            "",
            "| 指标 | 结果 |",
            "| --- | ---: |",
        ]
        rows = [
            ("poi_authority_rate", quality.get("poi_authority_rate")),
            ("field_reference_rate", quality.get("field_reference_rate")),
            ("time_conflict_rate", quality.get("time_conflict_rate")),
            ("route_violation_rate", quality.get("route_violation_rate")),
            ("attraction_duplicate_rate", quality.get("attraction_duplicate_rate")),
            ("budget_deviation_rate", quality.get("budget_deviation_rate")),
            ("terminal_reset_day_rate", quality.get("terminal_reset_day_rate")),
            ("coord_valid_rate", depth.get("coord_valid_rate")),
            ("deeplink_resolvable_rate", depth.get("deeplink_resolvable_rate")),
            ("category_reasonable_rate", depth.get("category_reasonable_rate")),
            ("theme_sentence_rate", narrative.get("theme_sentence_rate")),
            ("why_coverage", narrative.get("why_coverage")),
            ("practical_notes_rate", narrative.get("practical_notes_rate")),
            ("theme_hit_rate", narrative.get("theme_hit_rate")),
            ("poi_relevance", narrative.get("poi_relevance")),
            ("coord_available_rate", narrative.get("coord_available_rate")),
            ("pending_review_count", narrative.get("pending_review_count")),
        ]
        for name, value in rows:
            if value is None:
                shown = "-"
            elif isinstance(value, int) and not isinstance(value, bool):
                shown = str(value)  # 计数型指标（pending_review_count）不按百分比渲染
            else:
                shown = f"{float(value):.2%}"
            lines.append(f"| {name} | {shown} |")
        lines.append("")
    return "\n".join(lines)


def main() -> int:
    if not settings.llm_api_key:
        print("未配置 LLM_API_KEY，拒绝冒充真实 LLM 评测；请配置后再运行。", file=sys.stderr)
        return 2
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument(
        "--path",
        choices=("stream", "graph"),
        default="stream",
        help="stream=产品走的逐日流式（研究另算一个 run）；graph=同步 /v1/generate 那张共用预算的图",
    )
    parser.add_argument(
        "--cases", default=str(CASES_PATH), help="用例文件：默认 cases.json；主题化同题评测传 themed_cases.json"
    )
    parser.add_argument(
        "--out-stem",
        default="",
        help="报告文件名主干（默认按 path/主题化推导）：按路径分落盘时用，避免 stream 与 graph 互相覆盖",
    )
    args = parser.parse_args()
    cases_path = Path(args.cases)
    # 文件名以 themed 开头即走主题化报告输出（themed_report.json + .md）
    themed = cases_path.name.startswith("themed")
    # 数据集过 schema + 指纹（PR-0 数据集治理）：错题在加载点拦下，指纹随报告落盘
    cases, dataset = load_dataset(cases_path)
    if args.limit:
        cases = cases[: args.limit]
    # prompt_version 占位在运行时填充实际契约版本（M5），随 case 落报告
    cases = [{**case, "prompt_version": f"{OPEN_DAY_PROMPT_VERSION}/{OPEN_TRIP_PROMPT_VERSION}"} for case in cases]
    details = []
    same_count = 0
    status_counts = {"success": 0, "degraded": 0, "failed": 0}
    aggregate_stats = {
        "llm_calls": 0,
        "retry_count": 0,
        "fallback_count": 0,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
    }
    for case in cases:
        first, signature1 = _run_once(case, "run1", args.path)
        second, signature2 = _run_once(case, "run2", args.path)
        for run in (first, second):
            status_counts[run["status"]] = status_counts.get(run["status"], 0) + 1
            for key in aggregate_stats:
                aggregate_stats[key] += run["stats"].get(key, 0)
        same = signature1 == signature2
        same_count += same
        details.append(
            {
                "case": case,
                "consistent": same,
                "signature1": signature1,
                "signature2": signature2,
                "run1": first,
                "run2": second,
                "failure_reasons": [
                    run["status_reason"]
                    for run in (first, second)
                    if run.get("status") != "success" and run.get("status_reason")
                ],
            }
        )
    report = {
        # 产物自带新鲜度：口径变更后旧报告可据此识别为过期
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "mode": "real-llm-themed" if themed else "real-llm",
        # 数据集指纹（PR-0）：真实评测结论同样可回溯到"哪份题"
        "dataset": dataset,
        # 口径的一部分：同一条 case 在两条路径上的预算形状不同，数值不可跨路径比较
        "generation_path": args.path,
        "model": settings.llm_model,
        # 与开放模式真实生成调用同源（day_prompts.GENERATION_TEMPERATURE）
        "temperature": GENERATION_TEMPERATURE,
        "prompt_version": PROMPT_VERSION,
        # M5：报告头部固定两套开放生成 Prompt 的契约版本
        "open_day_prompt_version": OPEN_DAY_PROMPT_VERSION,
        "open_trip_prompt_version": OPEN_TRIP_PROMPT_VERSION,
        # Prompt **正文**指纹（P2-6）：版本号是手抄常量、改串不 bump 时无人发现；
        # 指纹由源码现值算出，eval_gate 侧用同一函数重算比对。
        "prompt_sha256": prompt_text_sha256(),
        "case_count": len(details),
        "run_count": len(details) * 2,
        "consistency_rate": round(same_count / max(len(details), 1), 4),
        "status_counts": status_counts,
        # C3.2 深度指标聚合（run1 口径；eval_gate 的防倒退下限消费这些值）
        "depth_metrics": {
            key: round(
                sum(float((detail["run1"].get("depth") or {}).get(key) or 0) for detail in details)
                / max(len(details), 1),
                4,
            )
            for key in ("coord_valid_rate", "deeplink_resolvable_rate", "category_reasonable_rate")
        },
        "aggregate_stats": aggregate_stats,
        "failed_cases": [
            detail["case"]
            for detail in details
            if any(detail[key].get("status") == "failed" for key in ("run1", "run2"))
        ],
        "degraded_cases": [
            detail["case"]
            for detail in details
            if any(detail[key].get("status") == "degraded" for key in ("run1", "run2"))
        ],
        "details": details,
    }
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    # 报告文件名：主题化同题评测（--cases themed_cases.json）输出 themed_report.json+md，
    # 不覆盖既有 llm_report.json；`--out-stem` 供按**路径**分别落盘——nightly 现在同时
    # 跑 stream（产品路径）与 graph（trip 图路径，唯一带终检的链），两份报告的深度口径
    # 不可混用，必须各存各的（审查 P2-6）。
    if args.out_stem:
        report_stem = args.out_stem
    elif themed:
        report_stem = "themed_report"
    elif args.path == "graph":
        report_stem = "llm_report_graph"
    else:
        report_stem = "llm_report"
    report_path = REPORT_DIR / f"{report_stem}.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    (REPORT_DIR / f"{report_stem}.md").write_text(_report_markdown(report), encoding="utf-8")
    keys = ("mode", "generation_path", "model", "temperature", "prompt_version", "case_count", "consistency_rate")
    print(json.dumps({k: report[k] for k in keys}, ensure_ascii=False, indent=2))
    print(f"报告已生成：{report_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
