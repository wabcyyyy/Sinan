"""业务路径体验指标报告（M7，spec 2026-10-08 §13.3，最小版）。

用法：
    uv run python tests/agent_eval/eval_experience.py

与 nightly（llm_eval 直调 agent 面）的差异与边界：
- 本报告从**业务服务入口**驱动（clarify 决策链 / services.itinerary_generation.generate /
  apply_plans / chat_edit / TripPlanStreamParser / official_pages），全部离线桩/纯函数，
  零真实 LLM、零真实网络——量的是"业务路径上硬约束与事务语义是否成立"，不是生成质量；
- nightly 直调路径的入参遗漏已在本次一并修复（`llm_eval._stream_request` 补
  preferences/requirements_struct、研究请求补 persons/budget/hotel_tier/intent/requirements/
  origin_city/requirements_struct，并在报告加 path_identity 标注）；nightly 本身未重写，
  真实 LLM 对比仍需另行授权；
- 指标口径（spec §13.3）：unknown 留在分母不剔除；超时/失败单列；
  自然度/舒适度人工匿名评价**不做**（需真人，报告留占位说明）。

每份报告绑定：commit、题集 hash、prompt 版本、调用路径、样本量（spec 的模型/缓存冷热/
来源覆盖维度在离线 mock 下无意义，如实标 offline-mock）。
"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.prompts.open_generation import OPEN_DAY_PROMPT_VERSION, OPEN_TRIP_PROMPT_VERSION
from tests.experience_cases import runner

REPORT_DIR = Path(__file__).with_name("report") / "experience"
#: 离线 mock 口径的模型标注（不冒充真实模型数字）
MODEL_LABEL = "offline-mock (no real LLM)"
CALL_PATH = (
    "business-service entries: editing.clarify / services.itinerary_generation.generate / "
    "itinerary_plan_apply.apply_plans / itinerary_chat.chat_edit(turn-id) / "
    "generation.content.TripPlanStreamParser / grounding.official_pages"
)
HUMAN_EVAL_PLACEHOLDER = (
    "自然度/舒适度（回应具体诉求、追问必要性、语气、取舍说明）需要真人匿名同题评价，"
    "本离线报告不做、不填机器代跑数字；上线后按 spec §13.3 以人工同题评分补齐。"
)


def git_commit() -> str:
    try:
        return subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=str(ROOT), capture_output=True, text=True, check=True
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown（git 不可用）"


def _sum_stats(result: dict, key: str) -> float:
    return float(sum(check.get("stats", {}).get(key, 0) for case in result["cases"] for check in case["checks"]))


def build_metrics(result: dict) -> dict:
    """§13.3 最少报告的离线可测四项 + 证据冲突数；unknown/超时如实单列不剔分母。"""
    applicable = _sum_stats(result, "constraint_applicable")
    passed = _sum_stats(result, "constraint_pass")
    generation_cases = _sum_stats(result, "generation_cases")
    first_pass = _sum_stats(result, "first_pass")
    timeouts = _sum_stats(result, "timeout_failures")
    failures = _sum_stats(result, "generation_failures")
    scope_cases = _sum_stats(result, "scope_cases")
    scope_preserved = _sum_stats(result, "scope_preserved")
    return {
        # 硬约束履约：pass / 适用约束数；violation、unknown 单列，unknown 留在分母
        "hard_constraint_fulfillment": {
            "applicable": applicable,
            "pass": passed,
            "violation": _sum_stats(result, "constraint_violation"),
            "unknown": _sum_stats(result, "constraint_unknown"),
            "rate": round(passed / applicable, 4) if applicable else None,
        },
        # 一次交付通过率：无 LLM 修复且核心硬门禁过 / 全部生成案例；超时、失败单列（在分母）
        "first_pass_delivery": {
            "generation_cases": generation_cases,
            "first_pass": first_pass,
            "timeout_failures": timeouts,
            "generation_failures": failures,
            "rate": round(first_pass / generation_cases, 4) if generation_cases else None,
        },
        # 编辑范围保持率：明确保护范围的案例中，未触及字段全部保持的比例
        "edit_scope_preservation": {
            "scoped_cases": scope_cases,
            "preserved": scope_preserved,
            "rate": round(scope_preserved / scope_cases, 4) if scope_cases else None,
        },
        # 重复执行数（E13 幂等面：同 turn 重放/并发争抢后实际双跑的次数）
        "duplicate_executions": _sum_stats(result, "duplicate_executions"),
        # 事实证据冲突数（E14：官方证据 vs 行程字段的冲突记录数）
        "fact_conflicts": _sum_stats(result, "fact_conflicts"),
    }


def build_report(result: dict) -> dict:
    return {
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "mode": "offline-business-path",
        "commit": git_commit(),
        "dataset": {
            "file": result["dataset"]["file"],
            "dataset_hash": result["dataset"]["dataset_hash"],
            "case_count": result["dataset"]["case_count"],
        },
        "prompt_version": {
            "open_day": OPEN_DAY_PROMPT_VERSION,
            "open_trip": OPEN_TRIP_PROMPT_VERSION,
            # 离线桩链路多数 check 不经过生成 prompt；版本照记，供与真实跑对比时对口径
            "note": "checks 主要走服务编排与确定性规则；prompt 版本绑定供真实 LLM 复跑时对齐",
        },
        "model": MODEL_LABEL,
        "call_path": CALL_PATH,
        "path_identity": (
            "business-service path (shell→research→per-day→persist→apply→idempotency)，"
            "LLM/网络全桩；与 nightly 直调路径（agent-face direct）分属两套口径，数值不可混读"
        ),
        "sample_size": {
            "cases": result["summary"]["cases"],
            "checks": result["summary"]["checks"],
            "checks_total": result["summary"]["checks_total"],
        },
        "metrics": build_metrics(result),
        "human_evaluation": HUMAN_EVAL_PLACEHOLDER,
        "nightly_gap_fixes": [
            "llm_eval._stream_request 补 preferences、requirements_struct（对齐 _plan_whole_trip）",
            "llm_eval._run_stream_once 研究请求补 persons/budget/hotel_tier/intent/requirements/"
            "origin_city/region_hint/requirements_struct（对齐 GenerateCommand.to_generate_request）",
            "llm_eval 报告加 path_identity 标注（agent-face direct，非业务落库链）",
        ],
        "cases": result["cases"],
    }


def write_report(report: dict) -> Path:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    path = REPORT_DIR / "experience_report.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n")
    return path


def main() -> int:
    result = runner.run_all()
    if result["summary"]["checks"][runner.FAIL] > 0:
        print("题集存在 fail，拒绝出报告（先修回归再谈指标）", file=sys.stderr)
        for case in result["cases"]:
            for check in case["checks"]:
                if check["status"] == runner.FAIL:
                    print(f"  {case['id']} {check['name']}: {check['detail'][:160]}", file=sys.stderr)
        return 1
    report = build_report(result)
    path = write_report(report)
    print(json.dumps(report["metrics"], ensure_ascii=False, indent=2))
    print(json.dumps(report["sample_size"], ensure_ascii=False))
    print(f"报告已生成：{path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
