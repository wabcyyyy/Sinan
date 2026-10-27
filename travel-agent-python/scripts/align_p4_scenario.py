"""只读复用 P4 真实输出，新增匹配普通日门禁的合成变体；全程禁止模型和 HTTP。"""

import argparse
import json
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.schemas.trip_requirements import IntakeState
from scripts.p4_acceptance_results import frozen_reference, policy_comparison
from scripts.run_p4_acceptance import save


@contextmanager
def local_only():
    with (
        patch("httpx.Client.send", side_effect=RuntimeError("本轮只允许本地冻结事实，不允许 HTTP")),
        patch("httpx.AsyncClient.send", side_effect=RuntimeError("本轮只允许本地冻结事实，不允许 HTTP")),
        patch("app.common.llm_client.LLMClient.complete", side_effect=RuntimeError("本轮不允许新增模型调用")),
    ):
        yield


def research_scope(raw, business):
    """记录原结论及其适用范围；不重新判模型、不把合成假设升级为真实证据。"""
    if raw.get("sufficient") is not business.get("sufficient"):
        raise ValueError("原始模型与业务记录的充分性结论不一致，停止归因")
    return {
        "recorded_model_sufficient": raw.get("sufficient"),
        "recorded_model_reason": raw.get("reason"),
        "recorded_business_evaluation": business,
        "real_research_gate_passed": business.get("sufficient") is True,
        "frozen_scope": {
            "world": "synthetic conditional experiment",
            "facts": "A/B identity, duration, opening and prices are declared fixture assumptions; no real POI claim",
            "question": "under the declared frozen facts, does P4 update and choose a constraint-valid plan?",
            "not_evaluated": (
                "real POI existence, real price truth, live source applicability or model evidence improvement"
            ),
            "gate_override": False,
        },
        "interpretation": (
            "原模型按真实资料口径返回不足，保留 false 与原理由。冻结规划只作条件实验；"
            "其通过不能覆盖研究门禁，也不能冒充模型对合成条件世界重新评估通过。"
        ),
    }


def align(source, output):
    source, output = Path(source).resolve(), Path(output).resolve()
    if output == source or source in output.parents:
        raise ValueError("新实验输出必须位于原始结果目录之外")
    old = json.loads((source / "result.json").read_text("utf-8"))
    raw_response = json.loads((source / "M2-research-evaluation-response.json").read_text("utf-8"))
    raw = json.loads(raw_response["choices"][0]["message"]["content"])
    state = IntakeState.model_validate(old["clarify"]["state"])
    old_reference = frozen_reference(state, 180)
    if old_reference["day_kind"] != "full" or old_reference["min_active_minutes"] != 240:
        raise ValueError("普通日基线与本次归因前提不符；不得自动调整门禁或伪称相同场景")
    output.mkdir(parents=True, exist_ok=False)
    design = {
        "case_id": "P4-Lisboa-full-day-240-v1",
        "source_run": str(source),
        "design_status": "post-failure development correction; not a holdout",
        "changed_fixture_field": {"candidate_duration_min": {"from": 180, "to": 240}},
        "unchanged": (
            "recorded M1 state, full day type, 09–13, max1, hard EUR100, excluded C, one_of, original M2 false"
        ),
        "query_limits": {"max_queries": 2, "budget_units": 2, "time_limit_seconds": 4, "seed": 0},
        "network_requests": 0,
        "model_calls": 0,
        "paid_cny": "0",
    }
    save(output / "scenario.json", design)
    started = time.monotonic()
    with local_only():
        corrected = policy_comparison(state, old["research"], duration_min=240)
    original_rows = old["policies"]["rows"]
    result = {
        "design": design,
        "research_scope": research_scope(raw, old["research"]),
        "original_failure": {
            "source": str(source / "result.json"),
            "reference": old_reference,
            "rows": [
                {"strategy": r["strategy"], "passed": r["passed"], "decision": r["decision"]} for r in original_rows
            ],
            "note": "original artifacts retained read-only; original four arms not rerun",
        },
        "corrected_conditional_experiment": corrected,
        "summary": {
            "conditional_planning_passed": sum(r["conditional_planning_passed"] for r in corrected["rows"]),
            "arms": len(corrected["rows"]),
            "real_research_gate_passed": old["research"].get("sufficient") is True,
            "full_real_acceptance_claimed": False,
            "elapsed_seconds": round(time.monotonic() - started, 3),
            "new_model_calls": 0,
            "new_http_requests": 0,
            "new_paid_cny": "0",
        },
    }
    save(output / "alignment.json", result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    print(json.dumps(align(args.source, args.output)["summary"], ensure_ascii=False))


if __name__ == "__main__":
    main()
