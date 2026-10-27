"""P4 相同冻结候选、相同查询上限的对照；不调用网络或模型。"""

import argparse
import json
import time
from pathlib import Path

from app.agent.generation.content.evidence_gaps import decision_signature
from app.agent.generation.orchestration.active_evidence import drive_evidence
from tests.active_evidence_cases import environment, observation, solve


def evaluate():
    cases = json.loads(
        (Path(__file__).resolve().parents[1] / "tests/fixtures/constraint_planning/p4_hidden_truth.json").read_text(
            encoding="utf-8"
        )
    )["cases"]
    rows = []
    for case in cases:
        truth = case["hidden_truth"]
        # 独立整数规则判官：固定 A09:00–11:00、B11:30–13:30，转场最多30分钟，预算100。
        # 不复用 CP 编译器或求解器作真值，且真值从不传入 gap/probe。
        oracle = (
            "FEASIBLE"
            if truth["closed"] is None and truth["forward_route"] <= 30 and 10 + truth["price_b"] <= 100
            else "INFEASIBLE"
        )
        for strategy, seeds in (("fixed", [0]), ("random", [0, 1, 2]), ("confidence", [0]), ("bottleneck", [0])):
            for seed in seeds:
                request, prepared, problems, truth = environment(case)
                assert request.requirements_struct is not None
                assert request.requirements_struct.constraint_planning is not None
                assert request.requirements_struct.constraint_planning.active_evidence is not None
                queried = []

                def observe(gap, current, candidates, req, options, *, queried=queried, truth=truth):
                    queried.append(gap.gap_id)
                    return observation(gap, candidates, truth)

                started = time.monotonic()
                results, report = drive_evidence(
                    problems,
                    prepared,
                    request,
                    options=request.requirements_struct.constraint_planning.active_evidence,
                    solve=solve,
                    observe=observe,
                    strategy=strategy,
                    seed=seed,
                )
                signature = decision_signature(results)
                rows.append(
                    {
                        "case": case["id"],
                        "split": case["split"],
                        "strategy": strategy,
                        "seed": seed,
                        "correct_feasibility": signature[0] == oracle,
                        "decision": signature,
                        "truth_decision": oracle,
                        "valid_delivery": oracle == "FEASIBLE" and signature[0] == "FEASIBLE",
                        "false_feasible": oracle == "INFEASIBLE" and signature[0] == "FEASIBLE",
                        "changed_queries": sum(q.decision_changed for q in report.queries),
                        "queries": len(queried),
                        "spent_units": report.spent_units,
                        "budget_units": report.budget_units,
                        "seconds": round(time.monotonic() - started, 6),
                        "report": report.model_dump(mode="json"),
                    }
                )
    aggregates = []
    for split in ("development", "holdout"):
        for strategy in ("fixed", "random", "confidence", "bottleneck"):
            selected = [r for r in rows if r["split"] == split and r["strategy"] == strategy]
            queries = sum(r["queries"] for r in selected)
            aggregates.append(
                {
                    "split": split,
                    "strategy": strategy,
                    "runs": len(selected),
                    "correct": sum(r["correct_feasibility"] for r in selected),
                    "queries": queries,
                    "valid_deliveries": sum(r["valid_delivery"] for r in selected),
                    "false_feasible": sum(r["false_feasible"] for r in selected),
                    "changed_queries": sum(r["changed_queries"] for r in selected),
                    "changed_query_rate": sum(r["changed_queries"] for r in selected) / queries if queries else None,
                    "spent_units": sum(r["spent_units"] for r in selected),
                    "seconds": round(sum(r["seconds"] for r in selected), 6),
                }
            )
    return {
        "method": "synthetic hidden truth; common seed pool; max 2 queries and 2 units per run; no model/network",
        "limitations": [
            "自制5开发+4留出，非独立旅行题库；策略冻结后首轮观察",
            "固定臂仅在同一候选池按营业/费用/路线域补查，不代表整套旧三域Agent的端到端成本",
            "缺失字段置信度同为未知，confidence臂按ID打破平局；random仅3个种子",
            "实际决策变化按数值可行性或地点选择/顺序计，不把单纯时间偏移算成改变",
            "没有真实模型、网页可用性或线上净成本/质量结论",
        ],
        "aggregates": aggregates,
        "rows": rows,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = evaluate()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result["aggregates"], ensure_ascii=False))
