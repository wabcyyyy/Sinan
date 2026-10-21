"""三组真实消融：直接检索+一次生成/相同修复 vs 当前研究；--run 才外呼。"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import logging
import multiprocessing
import os
import subprocess
import sys
import threading
import time
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
SEARCH_PRICE_SOURCE = "https://api-docs.deepseek.com/zh-cn/guides/search/"
VARIANTS = ("direct_once", "direct_repair", "current")


def variant_order(index, selected=VARIANTS):
    return tuple(v for v in VARIANTS[index % 3 :] + VARIANTS[: index % 3] if v in selected)


class SingleGenerationOnly(RuntimeError):
    """基线不允许追加行程生成。"""


def no_second_generation(*args, **kwargs):
    raise SingleGenerationOnly("直接检索基线只允许一次行程生成，未通过的日期如实保留未完成")


def strict_json(raw, client, **kwargs):
    from app.agent.core.json_utils import parse_llm_json

    data = parse_llm_json(raw)
    if kwargs.get("validate"):
        kwargs["validate"](data)
    return data


def direct_research(task):
    from app.agent.research.agent_state import UnifiedAgentState
    from app.agent.research.evidence import EvidencePack
    from app.agent.research.factory import _run_search

    result = _run_search(UnifiedAgentState(task=task))
    items = result["items"]
    return EvidencePack(
        domain=task.domain,
        items=items,
        rounds=1,
        confidence=0,
        gaps=["确定性直接检索；未进行模型充分性评估"],
        degraded=not items,
    )


@contextlib.contextmanager
def capture_calls():
    from app.agent.runtime.observability import current_scene
    from app.common import trial_billing

    original = trial_billing.billable_call
    rows: list[dict] = []
    lock = threading.Lock()

    @contextlib.contextmanager
    def observed(base_url, payload, *, user_funded=False):
        row = {"model": payload["model"], "scene": current_scene(), "started": time.time(), "usage": None}
        with lock:
            rows.append(row)
        with original(base_url, payload, user_funded=user_funded) as charge:

            class Observer:
                def settle(self, usage):
                    row["usage"] = usage
                    if charge is not None:
                        charge.settle(usage)

            try:
                yield Observer()
            except Exception as exc:
                row["error_type"] = type(exc).__name__
                raise
            finally:
                row["duration_ms"] = round((time.time() - row["started"]) * 1000, 2)

    with patch.object(trial_billing, "billable_call", observed):
        yield rows


def evaluate_quality(response, case, catalog, trace):
    from app.agent.generation.rules.constraint_checks import check_constraints
    from app.agent.generation.rules.day_policy import day_policy_for
    from app.agent.generation.rules.opening_hours import opening_checks
    from app.schemas.trip_requirements import TripRequirements
    from tests.agent_eval.metrics import evaluate_depth, evaluate_narrative, evaluate_response

    requirements = TripRequirements.model_validate(case.get("requirements_struct") or {})
    checks = []
    opening = []
    for plan in response.daily_plans:
        opening.extend(
            {"day_no": plan.day_no, **check} for check in opening_checks(plan.model_dump(), case.get("start_date"))
        )
        report = check_constraints(
            plan.model_dump(), requirements, plan.day_no, day_policy_for(requirements, plan.day_no)
        )
        checks.extend(check.model_dump(mode="json") for check in report.checks)
    counts = Counter(check["status"] for check in checks)
    denominator = sum(counts[status] for status in ("pass", "violation", "unknown"))
    total = response.budget_estimate.get("合计")
    if total is None:
        total = sum(response.budget_estimate.values())
    hard_cap = (case.get("requirements_struct") or {}).get("budgetPolicy", {}).get("mode") == "hard_cap"
    return {
        "delivery_status": response.status,
        "constraint_counts": dict(counts),
        "constraint_pass_rate": counts["pass"] / denominator
        if denominator and any(p.items for p in response.daily_plans)
        else None,
        "checks": checks,
        "opening_checks": opening,
        "opening_counts": dict(Counter(check["status"] for check in opening)),
        "hard_cap_pass": float(total) <= float(case["budget"]) if hard_cap else None,
        "itinerary_budget": total,
        "structural": evaluate_response(response, case, catalog, trace),
        "depth": evaluate_depth(response, case),
        "narrative": evaluate_narrative(response, case),
    }


def run_variant(case, variant, run_id):
    from app.agent.generation.content import trip_stream
    from app.agent.generation.orchestration import open_plans
    from app.agent.research import supervisor
    from app.services import itinerary_generation
    from tests.agent_eval.product_path import run_product_path

    generation_count = repair_count = 0
    original_stream = trip_stream.llm_open_trip_stream
    original_day = itinerary_generation._generate_day_with_trace

    def counted_stream(*args, **kwargs):
        nonlocal generation_count
        generation_count += 1
        yield from original_stream(*args, **kwargs)

    def counted_day(*args, **kwargs):
        nonlocal repair_count
        repair_count += 1
        return original_day(*args, **kwargs)

    with contextlib.ExitStack() as stack:
        stack.enter_context(patch.object(trip_stream, "llm_open_trip_stream", counted_stream))
        stack.enter_context(patch.object(itinerary_generation, "_generate_day_with_trace", counted_day))
        if variant in ("direct_once", "direct_repair"):
            stack.enter_context(patch.object(supervisor, "run_research", direct_research))
        if variant == "direct_once":
            stack.enter_context(patch.object(trip_stream, "parse_llm_json_with_repair", strict_json))
            stack.enter_context(patch.object(open_plans, "llm_open_day", no_second_generation))
            stack.enter_context(patch.object(itinerary_generation, "_generate_day_with_trace", no_second_generation))
        calls = stack.enter_context(capture_calls())
        with run_product_path(case, run_id) as (response, trace, catalog):
            quality = evaluate_quality(response, case, catalog, trace)
            final_plan = response.model_dump(mode="json")
    known = 0
    unknown = sum(
        not all((call["usage"] or {}).get(k) is not None for k in ("prompt_tokens", "completion_tokens"))
        for call in calls
    )
    searches = [event["metadata"] for event in trace["events"] if event["name"] == "web_search.request"]
    search_cost = sum(row.get("estimated_cny") or 0 for row in searches)
    search_unknown = sum(row.get("estimated_cny") is None for row in searches)
    total = round(known + search_cost, 8)
    return {
        "variant": variant,
        "run_id": run_id,
        "case": case,
        "duration_ms": trace["duration_ms"],
        "quality": quality,
        "final_plan": final_plan,
        "generation_streams": generation_count,
        "day_repair_attempts": repair_count,
        "calls": calls,
        "cost": {
            "http_attempts": len(calls),
            "usage_unknown_attempts": unknown,
            "search_http_attempts": len(searches),
            "search_billable_calls": sum(row.get("billable") is True for row in searches),
            "search_unknown_attempts": search_unknown,
            "model_known_estimated_cny": None,
            "model_pricing": "not estimated; Token usage is reported separately",
            "search_known_estimated_cny": round(search_cost, 8),
            "known_estimated_cny": total,
            "estimated_cny": None,
            "prompt_tokens": sum((call["usage"] or {}).get("prompt_tokens", 0) for call in calls),
            "completion_tokens": sum((call["usage"] or {}).get("completion_tokens", 0) for call in calls),
        },
        "trace": trace,
    }


def render_report(protocol, results):
    lines = [
        "# 三域研究真实配对消融",
        "",
        "每场景每方案一次，失败保留。每臂独立进程和临时数据库；供应商缓存与网络无法等同。未做人工盲评。",
        "direct_repair 与 current 使用相同修复；两者差异是研究规划/评估/补查。direct_once 禁用模型修复。",
        "模型价格未估算；Token 与独立搜索估算分开记录。未知用量不计零，不能把搜索小计当总费用。",
        "空交付不算约束通过；grounded 覆盖仅说明有可追溯证据，不等于事实逐条核实或整体质量。",
        "",
        "| 场景 | 方案 | 交付状态 | 秒 | 输入/输出 Token | 模型/搜索调用 | 估算元 | 约束通过率 | 待判定约束 |",
        "|---|---|---|---:|---:|---:|---:|---:|---:|",
    ]
    for result in results:
        quality, cost = result["quality"], result["cost"]
        rate = quality["constraint_pass_rate"]
        prefix = f"| {result['case']['id']} | {result['variant']} | {quality['delivery_status']} | "
        lines.append(
            prefix + f"{result['duration_ms'] / 1000:.2f} | "
            f"{cost['prompt_tokens']}/{cost['completion_tokens']} | "
            f"{cost['http_attempts']}/{cost['search_http_attempts']} | {cost['estimated_cny']} | "
            f"{rate:.1%} | {quality['constraint_counts'].get('unknown', 0)} |"
            if rate is not None
            else prefix + f"{result['duration_ms'] / 1000:.2f} | {cost['prompt_tokens']}/{cost['completion_tokens']} | "
            f"{cost['http_attempts']}/{cost['search_http_attempts']} | {cost['estimated_cny']} | 未验证 | - |"
        )
    lines += [
        "",
        f"搜索估算说明：{SEARCH_PRICE_SOURCE}；模型费用未估算",
        "",
        "## 固定协议",
        "",
        "```json",
        json.dumps(protocol, ensure_ascii=False, indent=2),
        "```",
    ]
    return "\n".join(lines) + "\n"


def _worker(case, variant, run_id, result_path):
    result = run_variant(case, variant, run_id)
    result_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--run", action="store_true")
    parser.add_argument("--output", required=True)
    parser.add_argument("--variants", nargs="+", choices=VARIANTS, default=VARIANTS)
    parser.add_argument("--case-id", nargs="+", help="只运行指定公开场景 ID")
    args = parser.parse_args()
    output = Path(args.output).resolve()
    output.mkdir(parents=True, exist_ok=False)
    for key, name in (
        ("CHECKPOINT_DB_PATH", "checkpoints.sqlite"),
        ("USAGE_DB_PATH", "usage.sqlite"),
        ("TRACE_STORAGE_PATH", "traces.sqlite"),
    ):
        os.environ[key] = str(output / name)
    logging.basicConfig(filename=output / "runtime.log", level=logging.WARNING, force=True, encoding="utf-8")
    from app.agent.data.web_search import search_channel_ready
    from app.common.config import settings
    from app.common.model_registry import binding
    from tests.agent_eval.prompt_digest import prompt_text_sha256

    source = Path(__file__).with_name("live_pilot_cases.json")
    raw_cases = json.loads(source.read_text(encoding="utf-8"))
    cases = [
        {**row["slots"], "id": row["id"], "intent": row["message"], "requirements_struct": row["requirements"]}
        for row in raw_cases
    ]
    if args.case_id:
        cases = [case for case in cases if case["id"] in args.case_id]
        if set(args.case_id) != {case["id"] for case in cases}:
            parser.error("未知场景 ID")
    protocol = {
        "created_at": datetime.now(UTC).isoformat(),
        "head": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
        "cases": cases,
        "case_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        "prompt_sha256": prompt_text_sha256(),
        "roles": {
            role: {"model": binding(role).model, "provider": binding(role).provider.name}
            for role in ("main", "fast", "search")
        },
        "limits": {
            key: getattr(settings, key)
            for key in (
                "agent_deadline_seconds",
                "itinerary_deadline_seconds",
                "research_deadline_seconds",
                "max_llm_calls",
                "max_token_budget",
            )
        },
        "search_channel_ready": search_channel_ready(),
        "thinking": settings.llm_reasoning_effort,
        "order": [
            [case["id"], variant] for index, case in enumerate(cases) for variant in variant_order(index, args.variants)
        ],
        "repair_reserve": "disabled; fixed reservation has no product-path benefit evidence",
        "pricing": {"model": "not estimated", "search_source": SEARCH_PRICE_SOURCE, "search": "per trace only"},
        "scope": (
            "core delivery only; same deterministic grounding/persistence/final rules; "
            "direct_once has no model planning/evaluation/JSON repair/day regeneration; "
            "direct_repair retains the same model repairs as current; "
            "each arm uses a fresh process/app cache; provider cache and external network are uncontrolled; "
            "one run per scenario per arm"
        ),
    }
    (output / "protocol.json").write_text(json.dumps(protocol, ensure_ascii=False, indent=2), encoding="utf-8")
    if not args.run:
        print(json.dumps(protocol, ensure_ascii=False, indent=2))
        return 0
    if any(not binding(role).provider.api_key for role in ("main", "fast")):
        raise RuntimeError("未配置模型凭据，不运行真实消融")
    results = []
    for index, case in enumerate(cases):
        variants = variant_order(index, args.variants)
        for variant in variants:
            print(f"START {case['id']} {variant} {datetime.now(UTC).isoformat()}", flush=True)
            result_path = output / f"{case['id']}-{variant}.json"
            process = multiprocessing.get_context("spawn").Process(
                target=_worker, args=(case, variant, f"ablation-{output.name}-{case['id']}-{variant}", result_path)
            )
            process.start()
            process.join()
            if process.exitcode != 0 or not result_path.exists():
                raise RuntimeError(f"{case['id']} {variant} worker failed; no successful result inferred")
            result = json.loads(result_path.read_text(encoding="utf-8"))
            results.append(result)
            (output / "report.md").write_text(render_report(protocol, results), encoding="utf-8")
            print(
                f"DONE {case['id']} {variant} status={result['quality']['delivery_status']} "
                f"seconds={result['duration_ms'] / 1000:.2f} calls={result['cost']['http_attempts']} "
                f"cny={result['cost']['estimated_cny']}",
                flush=True,
            )
    print(f"REPORT {output / 'report.md'}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
