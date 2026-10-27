"""Six separately named synthetic state/revision diagnostics, not ChinaTravel originals."""

import copy
from pathlib import Path

from scripts.run_p4_acceptance import save
from scripts.sinan_nl_cases import OPTIONS


def prepare_diagnostics(root):
    path = Path(root) / "diagnostic-design.json"
    if path.exists():
        raise FileExistsError(path)
    rows = []
    for number in range(6):
        a, b, c = (f"匿名馆{number}{suffix}" for suffix in ("甲", "乙", "丙"))
        pool = [
            {
                "id": f"cp-{i}",
                "name": name,
                "duration_min": 60,
                "price": 10 if i < 2 else 0,
                "opening": [[540, 960]],
                "latitude": 30 + i * 0.001,
                "longitude": 120 + i * 0.001,
            }
            for i, name in enumerate((a, b, c))
        ]
        req = {
            "day_windows": [{"day_no": 1, "not_before": "09:00", "finish_by": "16:00"}],
            "budget_policy": {"mode": "hard_cap", "include_intercity_transport": False},
            "max_attractions_per_day": 3,
            "required_places": [],
            "place_relations": [],
        }
        budget, expected = 100, "FEASIBLE"

        def relation(first, second, name, strength="hard"):
            return {
                "constraint_id": name,
                "kind": "before",
                "place_names": [first, second],
                "day_no": 1,
                "strength": strength,
            }

        if number == 0:
            req["place_relations"] = [relation(a, b, "forward"), relation(b, a, "backward")]
            req["required_places"] = [{"constraint_id": "keep-c", "name": c}]
            clause = f"第1天必须先去{a}结束后再去{b}，但也必须先去{b}结束后再去{a}，两项先后都是硬要求；{c}全程也必须去"
            expected = "INFEASIBLE"
        elif number == 1:
            req["required_places"] = [{"constraint_id": "must-a", "name": a}]
            budget = 0
            clause = f"{a}全程必须去"
            expected = "INFEASIBLE"
        elif number in (2, 3):
            req["required_places"] = [{"constraint_id": "must-a", "name": a}]
            pool[0]["opening" if number == 2 else "price"] = None
            clause = f"{a}全程必须去，缺少的营业或价格事实不能自行假定"
            expected = "UNKNOWN"
        elif number == 4:
            req["required_places"] = [{"constraint_id": "must-c", "name": c}]
            req["excluded_places"] = [a, b]
            budget = 0
            clause = f"{c}一定去，{a}和{b}都不要安排"
        else:
            req["place_relations"] = [relation(a, b, "forward"), relation(b, a, "backward", "soft")]
            clause = (
                f"第1天先{a}结束再{b}是硬要求，两处都要去；反过来先{b}后{a}只是可协商的软偏好，不能覆盖前面的硬要求"
            )
        nl = (
            "1个人从2026年11月2日在杭州玩1天，仅安排景点，食宿另行处理。每天9点后开始、16点前结束，"
            f"最多3个景点，同一地点不重复。所列门票总额硬上限{budget}元人民币，不含城际或市内交通、食宿。{clause}。"
        )
        rows.append(
            {
                "id": f"diag-{number:02d}",
                "split": "diagnostic",
                "family": "synthetic-state-diagnostic",
                "nl": nl,
                "facts": {
                    "candidates": pool,
                    "routes": [[x["id"], y["id"], 15] for x in pool for y in pool if x != y],
                    "options": OPTIONS,
                    "route_basis": "synthetic directed minutes",
                },
                "gold": {
                    "city": "杭州",
                    "days": 1,
                    "persons": 1,
                    "budget": budget,
                    "start_date": "2026-11-02",
                    "requirements": copy.deepcopy(req),
                },
                "expected": expected,
            }
        )
    save(
        path,
        {
            "name": "Sinan synthetic state diagnostics v1",
            "rows": rows,
            "revision_script": "diag-00: explicitly soften reverse order only; retain forward and must-c",
        },
    )
    return rows
