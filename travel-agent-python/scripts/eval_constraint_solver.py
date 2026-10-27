"""P0 验收实验：独立穷举 5–8 候选，记录 12/16/20 候选有界求解。

自制冻结事实，无 LLM/在线工具；小样本不用于推算产品成功率或普遍最优性。
用法：uv run python scripts/eval_constraint_solver.py --output <JSON 路径>
"""

import argparse
import json
import platform
import sys
import time
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import ortools

from app.agent.generation.content.constraint_solver import solve_day
from tests.constraint_planner_cases import frozen_problem
from tests.constraint_planner_oracle import exhaustive_day, objective, schedule_violations


def run_case(size, seed, infeasible=False, *, oracle=False):
    problem = frozen_problem(size, seed, infeasible=infeasible)
    result = solve_day(problem, time_limit_s=3)
    errors = schedule_violations(problem, result.visits) if result.visits else []
    row = {
        "size": size,
        "seed": seed,
        "expected_feasible": not infeasible,
        "status": result.status,
        "solver_status": result.solver_status,
        "solve_s": result.elapsed_s,
        "objective": result.objective,
        "objective_bound": result.objective_bound,
        "optimized_tiers": result.optimized_tiers,
        "total_tiers": result.total_tiers,
        "independent_violations": errors,
        "conflict_ids": result.conflict_ids,
        "conflict_irreducible": result.conflict_irreducible,
        "visits": [asdict(visit) for visit in result.visits],
        "report": result.report.model_dump(mode="json", by_alias=True),
    }
    validation_errors = list(errors)
    if infeasible and result.status != "INFEASIBLE":
        validation_errors.append("已知不可行案例未证明不可行")
    if oracle:
        started = time.monotonic()
        visits, score, examined = exhaustive_day(problem)
        row.update(
            oracle_s=round(time.monotonic() - started, 6),
            permutations_examined=examined,
            oracle_objective=score,
            oracle_feasible=visits is not None,
        )
        if (visits is None) != (result.status == "INFEASIBLE"):
            validation_errors.append("与独立穷举的可行性不一致")
        if visits is not None and (result.status != "OPTIMAL" or tuple(result.objective) != score):
            validation_errors.append("未完成小规模最优目标核对")
    elif not infeasible and result.status == "INFEASIBLE":
        validation_errors.append("将已知可行案例误判为不可行")
    if result.visits and tuple(result.objective) != objective(problem, result.visits):
        validation_errors.append("最终安排与分层目标值不一致")
    row["validation_errors"] = validation_errors
    return row


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    started = time.monotonic()
    rows = [run_case(size, 100 + size, infeasible, oracle=True) for size in range(5, 9) for infeasible in (False, True)]
    rows.extend(run_case(size, 200 + size) for size in (12, 16, 20))
    payload = {
        "task": "SINAN-UPGRADE-P0",
        "created_utc": datetime.now(UTC).isoformat(),
        "ortools": ortools.__version__,
        "python": platform.python_version(),
        "platform": platform.platform(),
        "time_limit_s_per_case": 3,
        "source": "hand-authored synthetic frozen facts; no model/online calls",
        "elapsed_s": round(time.monotonic() - started, 6),
        "cases": rows,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                "output": str(args.output),
                "cases": len(rows),
                "elapsed_s": payload["elapsed_s"],
                "statuses": [row["status"] for row in rows],
            },
            ensure_ascii=False,
        )
    )
    return int(any(row["validation_errors"] for row in rows))


if __name__ == "__main__":
    raise SystemExit(main())
