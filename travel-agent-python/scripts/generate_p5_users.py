"""分基础故事生成真实模型意图/用户表达；每批10次以内，缓存复用不重发。"""

import argparse
import json
import sys
import time
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.p5_scenarios import FAMILIES, intake_message, operations, semantic_errors
from scripts.run_p4_acceptance import save
from scripts.sinan_night_budget import NightBudget, NightClient

SIM_SYSTEM = (
    "你是受控旅行用户模拟器，只负责把本轮给定的一个操作用简短中文表达，不增加地点、时刻、"
    "预算、删除权限或其他操作，不假装已生效，不执行任何业务工具。你不知道未来事件，不编后续计划。"
    '只输出JSON：{"message":"一句用户话"}。日期用第N天，时刻保留HH:MM，分钟用阿拉伯数字。'
    "有选中的条目时可以省略名称。预约完整时段；结束需写第N天HH:MM前结束。"
)


def generate(family, root, fixture, label="v1"):
    from app.agent.editing import clarify
    from app.common.model_registry import binding
    from app.schemas.planning import ConstraintPlanningOptions
    from app.schemas.trip import ClarifyRequest
    from app.schemas.trip_requirements import IntakeState, TripRequirements

    root = Path(root)
    fixture = Path(fixture)
    if fixture.exists():
        raise ValueError("该故事录制已完成，禁止覆盖原结果或重复生成")
    fixture.parent.mkdir(parents=True, exist_ok=True)
    root.mkdir(parents=True, exist_ok=True)
    ledger = NightBudget(root / "budget.json")
    client = NightClient(ledger, root / "models", binding("main"))
    profile = FAMILIES[family]
    result = {
        "family": family,
        "profile": profile,
        "method": "real clarify and constrained real user expression; frozen controller, no future events sent",
        "recording_label": label,
        "trajectories": [],
    }
    save(
        root / f"{family}-{label}-controller.json",
        {"profile": profile, "trajectories": [operations(family, v) for v in range(3)]},
    )
    client.identifier = f"p5-{family}-intake"
    state = IntakeState(
        requirements=TripRequirements(
            constraint_planning=ConstraintPlanningOptions(country_code="FR", time_zone="Europe/Paris", currency="EUR")
        )
    )
    with patch.object(clarify, "get_llm_client", return_value=client):
        response = clarify.run_clarify(ClarifyRequest(state=state, message=intake_message(family)))
    result["intake_message"] = intake_message(family)
    result["intake"] = response.model_dump(mode="json")
    collected = response.state
    result["intake_checks"] = {
        "not_degraded": not response.degraded,
        "destination": collected.city in {"Paris", "巴黎", "巴黎市"},
        "slots": collected.days == profile["days"]
        and collected.persons == 1
        and collected.budget == 1000
        and collected.start_date == "2026-11-03"
        and collected.stay_nights == 0,
        "hard_budget": bool(
            collected.requirements.budget_policy and collected.requirements.budget_policy.mode == "hard_cap"
        ),
        "daily_max": collected.requirements.max_attractions_per_day == profile["max_places"],
        "windows": all(
            any(
                w.day_no == day and w.kind == "full" and w.not_before == "09:00" and w.finish_by == "18:00"
                for w in collected.requirements.day_windows
            )
            for day in range(1, profile["days"] + 1)
        ),
    }
    styles = [
        "简短直接，可使用推迟/移到/预约/关闭/优先保留等词",
        "自然礼貌，但不能加其他意图",
        "明确点名活动，用简短同义词表达",
    ]
    started = time.monotonic()
    for variant, style in enumerate(styles):
        trajectory = {"id": f"{family}-{variant}", "style": style, "turns": []}
        for turn, operation in enumerate(operations(family, variant), 1):
            if time.monotonic() - started > 48:
                save(root / f"{family}-{label}-partial.json", result)
                raise TimeoutError("本批表达生成48秒预算耗尽；仅可续未发出的ID")
            client.identifier = f"p5-{family}-{variant}-{turn}" + (f"-{label}" if label != "v1" else "")
            this_turn = {k: v for k, v in operation.items() if k != "expected"}
            if operation["kind"] == "negative_cancel" and label != "v1":
                this_turn["meaning"] = "保持该活动，不要取消或关闭；表达否定取消，而不是执行取消"
            raw = client.complete(
                json.dumps(
                    {"this_turn": this_turn, "style": style},
                    ensure_ascii=False,
                ),
                system_prompt=SIM_SYSTEM,
                max_tokens=192,
                temperature=0.3,
            )
            try:
                data = json.loads(raw)
                message = data["message"]
                errors = semantic_errors(message, operation)
            except (ValueError, KeyError, TypeError):
                message, errors = str(raw), ["模拟器JSON不合法"]
            trajectory["turns"].append(
                {
                    "operation": operation,
                    "message": message,
                    "simulator_errors": errors,
                    "request_id": client.identifier,
                }
            )
        result["trajectories"].append(trajectory)
        save(root / f"{family}-{label}-partial.json", result)
    save(fixture, result)
    print(
        json.dumps(
            {
                "family": family,
                "intake": result["intake_checks"],
                "valid_user_turns": sum(
                    not t["simulator_errors"] for tr in result["trajectories"] for t in tr["turns"]
                ),
                "total_peak_upper_cny": str(ledger.total()),
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--family", choices=FAMILIES, required=True)
    p.add_argument("--root", type=Path, required=True)
    p.add_argument("--fixture", type=Path, required=True)
    p.add_argument("--approved", action="store_true")
    p.add_argument("--label", default="v1")
    a = p.parse_args()
    if not a.approved:
        raise SystemExit("需要本夜累计预算授权")
    generate(a.family, a.root, a.fixture, a.label)
