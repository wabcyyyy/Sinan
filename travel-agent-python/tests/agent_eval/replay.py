"""失败案例回放：不用真实 LLM，复现安全边界如何拦截错误计划。

用法：uv run python tests/agent_eval/replay.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from app.agent.generation.content.reference_pool import ReferencePool
from app.agent.generation.content.reflect import validate_plans
from tests.agent_eval.dataset_schema import load_dataset
from tests.agent_eval.mock_llm import catalog

CASES_PATH = Path(__file__).with_name("replay_cases.json")


def _run(case: dict) -> dict:
    kind = case["kind"]
    if kind == "authority":
        data = catalog("杭州")
        pool = ReferencePool({"candidates": data["attractions"], "foods": data["foods"], "hotels": data["hotels"]})
        item = {"item_type": "attraction", "poi_name": "不存在的景点"}
        grounded = pool.ground(item)
        # LLM-only 口径：编造名称不允许获得知识库背书（source/权威字段不落地）。
        detected = grounded is False and "source" not in item
        return {
            "id": case["id"],
            "detected": detected,
            "issue": "非白名单名称未被知识库背书" if detected else " unexpectedly grounded",
        }
    elif kind == "route":
        issues, _ = validate_plans(
            [
                {
                    "day_no": 1,
                    "items": [
                        {
                            "item_type": "attraction",
                            "poi_name": "A",
                            "start_time": "09:00",
                            "end_time": "10:00",
                            "latitude": 30.0,
                            "longitude": 120.0,
                        },
                        {
                            "item_type": "attraction",
                            "poi_name": "B",
                            "start_time": "10:30",
                            "end_time": "12:00",
                            "latitude": 30.2,
                            "longitude": 120.0,
                        },
                    ],
                }
            ]
        )
        return {"id": case["id"], "detected": any("路线时间不足" in issue for issue in issues), "issue": issues}
    elif kind == "opening":
        issues, _ = validate_plans(
            [
                {
                    "day_no": 1,
                    "items": [
                        {
                            "item_type": "attraction",
                            "poi_name": "闭馆景点",
                            "start_time": "16:00",
                            "end_time": "18:00",
                            "open_time": "09:00-17:00",
                        }
                    ],
                }
            ]
        )
        return {"id": case["id"], "detected": any("开放时间不符" in issue for issue in issues), "issue": issues}
    raise ValueError(f"unknown replay kind: {kind}")


def main() -> int:
    cases, _ = load_dataset(CASES_PATH)
    results = [_run(case) for case in cases]
    print(json.dumps({"case_count": len(results), "results": results}, ensure_ascii=False, indent=2))
    return 0 if all(result["detected"] for result in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
