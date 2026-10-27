"""P5约束顺序/新软偏好补验：4条手写校准轨迹，真实多轮意图模型，不发工具请求。"""

import argparse
import json
import time
from pathlib import Path
from unittest.mock import patch

from app.agent.editing import clarify
from app.common.model_registry import binding
from app.schemas.planning import ConstraintPlanningOptions
from app.schemas.trip import ClarifyRequest
from app.schemas.trip_requirements import IntakeState, TripRequirements
from scripts.run_p4_acceptance import save
from scripts.sinan_night_budget import NightBudget, NightClient

CASES = {
    "paris": {
        "country": "FR",
        "zone": "Europe/Paris",
        "currency": "EUR",
        "budget": 1000,
        "base": "2026年11月3日去巴黎玩2天，1个人，不住宿，活动总预算1000欧元。",
        "hard": "第1天必须去卢浮宫，绝对不去Louvre-Lens；第1天09:00以后开始、16:00以前结束，1000欧元活动预算是硬上限。",
        "soft": "第1天优先先去Musée d'Orsay再去卢浮宫，这只是可协商的软偏好，不能覆盖原有硬要求。",
        "required": ("卢浮宫", "muséedulouvre"),
        "excluded": "louvre-lens",
        "soft_name": "orsay",
        "window": ("09:00", "16:00"),
    },
    "kyoto": {
        "country": "JP",
        "zone": "Asia/Tokyo",
        "currency": "JPY",
        "budget": 10000,
        "base": "2026年11月3日去京都玩2天，1个人，不住宿，活动总预算10000日元。",
        "hard": "第1天必须去清水寺，绝对不去银阁寺；第1天10:00以后开始、17:00以前结束，10000日元活动预算是硬上限。",
        "soft": "第1天优先先去八坂神社再去清水寺，这只是可协商的软偏好，不能覆盖原有硬要求。",
        "required": ("清水寺", "kiyomizu-dera"),
        "excluded": "银阁寺",
        "soft_name": "八坂神社",
        "window": ("10:00", "17:00"),
    },
    "lisbon": {
        "country": "PT",
        "zone": "Europe/Lisbon",
        "currency": "EUR",
        "budget": 100,
        "base": "2026年11月3日去里斯本玩2天，1个人，不住宿，活动总预算100欧元。",
        "hard": (
            "第1天必须去Mosteiro dos Jerónimos，绝对不去Oceanário de Lisboa；"
            "第1天09:00以后开始、16:00以前结束，100欧元活动预算是硬上限。"
        ),
        "soft": "第1天如果时间允许，想先去Torre de Belém再到Mosteiro dos Jerónimos；前一个可以不去，原有硬条件不变。",
        "required": ("mosteirodosjerónimos", "热罗尼莫斯修道院", "杰罗尼莫斯修道院"),
        "excluded": ("oceanáriodelisboa", "里斯本海洋馆"),
        "soft_name": ("torredebelém", "贝伦塔"),
        "window": ("09:00", "16:00"),
    },
    "bangkok": {
        "country": "TH",
        "zone": "Asia/Bangkok",
        "currency": "THB",
        "budget": 10000,
        "base": "2026年11月3日去曼谷玩2天，1个人，不住宿，活动总预算10000泰铢。",
        "hard": "第1天必须去Wat Pho，绝对不去ICONSIAM；第1天10:00以后开始、17:00以前结束，10000泰铢活动预算是硬上限。",
        "soft": "第1天想先去Grand Palace再去Wat Pho，有空就去前一个，挤不下可跳过；原有硬要求不变。",
        "required": ("watpho", "卧佛寺"),
        "excluded": ("iconsiam", "暹罗天地"),
        "soft_name": ("grandpalace", "大皇宫"),
        "window": ("10:00", "17:00"),
    },
}


def norm(text):
    return "".join(text.casefold().split())


def check(state, spec, hard_seen, soft_seen):
    req = state.requirements
    checks = {
        "slots_retained": state.days == 2
        and state.persons == 1
        and state.budget == spec["budget"]
        and state.start_date == "2026-11-03"
        and state.stay_nights == 0,
    }
    if hard_seen:
        checks.update(
            required_retained=any(matches(p.name, spec["required"]) and p.day_no == 1 for p in req.required_places),
            exclusion_retained=any(matches(name, spec["excluded"]) for name in req.excluded_places),
            day_window_retained=any(
                w.day_no == 1 and (w.not_before, w.finish_by) == spec["window"] for w in req.day_windows
            ),
            hard_budget_retained=bool(req.budget_policy and req.budget_policy.mode == "hard_cap"),
        )
    if soft_seen:
        checks["soft_does_not_add_hard_required"] = not any(
            matches(p.name, spec["soft_name"]) for p in req.required_places
        )
        checks["soft_does_not_become_hard_order"] = not any(
            r.strength == "hard" and any(matches(name, spec["soft_name"]) for name in r.place_names)
            for r in req.place_relations
        )
    return checks


def matches(name, choices):
    return any(norm(term) in norm(name) for term in ([choices] if isinstance(choices, str) else choices))


def run(root, label="v1", cities=("paris", "kyoto")):
    suffix = "" if label == "v1" else f"-{label}"
    target = root / f"intake-orders{suffix}.json"
    if target.exists():
        raise ValueError("校准轨迹已完成，保留原输出，不重发")
    save(
        root / f"intake-orders-design{suffix}.json",
        {
            "cases": {name: CASES[name] for name in cities},
            "orders": [["base", "hard", "soft"], ["base", "soft", "hard"]],
            "method": "hand-authored calibration messages, real multi-turn clarify; not a blinded language holdout",
        },
    )
    client = NightClient(NightBudget(root / "budget.json"), root / "models", binding("main"))
    report = {
        "method": "real multi-turn clarify model with its prior state; hand-authored user calibration",
        "trajectories": [],
    }
    started = time.monotonic()
    for name in cities:
        spec = CASES[name]
        for order_id, order in enumerate((("base", "hard", "soft"), ("base", "soft", "hard"))):
            state = IntakeState(
                requirements=TripRequirements(
                    constraint_planning=ConstraintPlanningOptions(
                        country_code=spec["country"], time_zone=spec["zone"], currency=spec["currency"]
                    )
                )
            )
            trace = {"city": name, "order": order, "turns": []}
            report["trajectories"].append(trace)
            hard_seen = soft_seen = False
            for turn, kind in enumerate(order):
                if time.monotonic() - started > 65:
                    raise TimeoutError("本批65秒预算到，不再启动调用")
                client.identifier = f"p5-order-{name}-{order_id}-{turn}{suffix}"
                with patch.object(clarify, "get_llm_client", return_value=client):
                    response = clarify.run_clarify(ClarifyRequest(state=state, message=spec[kind]))
                state = response.state
                hard_seen |= kind == "hard"
                soft_seen |= kind == "soft"
                checks = check(state, spec, hard_seen, soft_seen)
                trace["turns"].append(
                    {
                        "message": spec[kind],
                        "response": response.model_dump(mode="json"),
                        "checks": checks,
                        "passed": all(checks.values()) and not response.degraded,
                    }
                )
                save(root / f"intake-orders-partial{suffix}.json", report)
    report["total_peak_upper_cny"] = str(client.ledger.total())
    report["elapsed_s"] = round(time.monotonic() - started, 3)
    save(target, report)
    print(
        json.dumps(
            {
                "passed_trajectories": sum(all(t["passed"] for t in r["turns"]) for r in report["trajectories"]),
                "elapsed_s": report["elapsed_s"],
                "cost_upper": report["total_peak_upper_cny"],
            }
        )
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--approved", action="store_true")
    parser.add_argument("--label", default="v1")
    parser.add_argument("--cities", nargs="+", choices=CASES, default=("paris", "kyoto"))
    args = parser.parse_args()
    if not args.approved:
        raise SystemExit("需要既定本夜匿名输入/累计30元授权")
    run(args.root, args.label, args.cities)
