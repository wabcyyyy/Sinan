"""Pre-model, deterministic ChinaTravel POI adaptation; gold never enters runtime prompts."""

import csv
import math
import re
from pathlib import Path

from scripts.run_p4_acceptance import save

CITIES = dict(
    zip(
        ("北京", "成都", "重庆", "广州", "杭州", "南京", "上海", "深圳", "苏州", "武汉"),
        (
            "beijing",
            "chengdu",
            "chongqing",
            "guangzhou",
            "hangzhou",
            "nanjing",
            "shanghai",
            "shenzhen",
            "suzhou",
            "wuhan",
        ),
        strict=True,
    )
)
OPTIONS = {"country_code": "CN", "time_zone": "Asia/Shanghai", "currency": "CNY"}
FAMILIES = ("must_exclude", "before", "choice", "day_assignment", "soft_order", "combined", "cap", "choice_order")


def minutes(value):
    a, b = value.split(":")
    return int(a) * 60 + int(b)


def hhmm(value):
    return f"{value // 60:02d}:{value % 60:02d}"


def _pool(root, city):
    path = root / "upstream-data/sandbox/database/attractions" / CITIES[city] / "attractions.csv"
    candidates = []
    for row in csv.DictReader(path.open(encoding="utf8")):
        try:
            duration = round(float(row["recommendmintime"]) * 60)
            start, end = minutes(row["opentime"]), minutes(row["endtime"])
            price = float(row["price"])
            if not (15 <= duration <= 180 and 0 <= start < end <= 1440 and price >= 0):
                continue
        except (ValueError, KeyError):
            continue
        candidates.append(
            {
                "id": f"cp-{len(candidates)}",
                "name": row["name"],
                "duration_min": duration,
                "price": price,
                "currency": "CNY",
                "opening": [[start, end]],
                "latitude": float(row["lat"]),
                "longitude": float(row["lon"]),
                "source_id": row["id"],
                "source_row": row,
            }
        )
        if len(candidates) == 10:
            break
    if len(candidates) < 6:
        raise ValueError(f"insufficient fixed pool for {city}")
    routes = []
    for i, first in enumerate(candidates):
        for j, second in enumerate(candidates):
            if i != j:
                # Explicit synthetic directed transfer assumptions, not geocoding/road-network truth.
                distance = math.hypot(
                    (first["latitude"] - second["latitude"]) * 111,
                    (first["longitude"] - second["longitude"]) * 96,
                )
                routes.append([first["id"], second["id"], min(90, max(5, round(distance * 2))) + (i + 2 * j) % 4])
    return {"candidates": candidates, "routes": routes, "route_basis": "synthetic directed minutes", "options": OPTIONS}


def _case(root, source, index):
    city, days, persons = source["target_city"], int(source["days"]), int(source["people_number"])
    facts = _pool(root, city)
    names = [row["name"] for row in facts["candidates"]]
    match = re.search(r"预算(\d+)", source["nature_language"])
    if match is None:
        raise ValueError("selected source has no numeric budget")
    budget = int(match[1])
    req = {
        "day_windows": [{"day_no": d, "not_before": "09:00", "finish_by": "16:00"} for d in range(1, days + 1)],
        "budget_policy": {"mode": "hard_cap", "include_intercity_transport": False},
        "max_attractions_per_day": 3,
        "required_places": [],
        "excluded_places": [],
        "place_relations": [],
    }
    family = FAMILIES[index % len(FAMILIES)]
    hard = index % len(FAMILIES) != 4
    clauses = []

    def must(name, day=None):
        req["required_places"].append(
            {"constraint_id": f"must-{len(req['required_places'])}", "name": name, "day_no": day}
        )

    def relation(kind, day, a, b, *, strength="hard"):
        req["place_relations"].append(
            {
                "constraint_id": f"rel-{len(req['place_relations'])}",
                "kind": kind,
                "place_names": [a, b],
                "day_no": day,
                "strength": strength,
            }
        )

    if family == "must_exclude":
        must(names[0])
        req["excluded_places"] = [names[3]]
        clauses.append(f"{names[0]}一定要去，{names[3]}不要安排")
    elif family == "before":
        relation("before", 1, names[0], names[1])
        clauses.append(f"第1天必须先去{names[0]}，游玩结束后再去{names[1]}，这两处都不能漏")
    elif family == "choice":
        relation("one_of", days, names[1], names[2])
        req["excluded_places"] = [names[4]]
        clauses.append(f"第{days}天{names[1]}和{names[2]}至少安排一处，可以都去；全程不去{names[4]}")
    elif family == "day_assignment":
        must(names[0], days)
        must(names[1], 1)
        clauses.append(f"把{names[0]}放在第{days}天且必须去，{names[1]}必须放第1天")
    elif family == "soft_order":
        must(names[2])
        relation("before", 1, names[0], names[1], strength="soft")
        clauses.append(
            f"{names[2]}是全程必去。第1天如果方便，偏好先{names[0]}后{names[1]}，后两处均非必去，排不下可以跳过"
        )
    elif family == "combined":
        must(names[0])
        relation("one_of", days, names[1], names[2])
        req["excluded_places"] = [names[3]]
        clauses.append(f"无论哪天都必须去{names[0]}；第{days}天{names[1]}、{names[2]}至少选一个；避开{names[3]}")
    elif family == "cap":
        req["max_attractions_per_day"] = 2
        must(names[0])
        must(names[1], days)
        clauses.append(f"慢节奏在这次仅指每日景点数上限2，不另设pace；{names[0]}全程必去，{names[1]}必须第{days}天去")
    else:
        relation("before", 1, names[0], names[1])
        relation("one_of", days, names[2], names[3])
        clauses.append(f"第1天先{names[0]}再{names[1]}是硬要求，两处必须都去；第{days}天{names[2]}、{names[3]}至少一个")
    cap = req["max_attractions_per_day"]
    base = (
        f"请为{persons}人安排{city}{days}日游，从2026年11月2日开始，各日只安排景点，住宿和餐饮已另行处理。"
        f"所列景点门票总额不得超过{budget}元人民币，不含城际、市内交通及食宿。每天9点后开始且16点前结束，"
        f"每天景点最多{cap}个，同一景点全程不能重复。"
    )
    nl = base + "；".join(clauses) + "。仅使用给定候选及冻结事实，不能缩短游玩时长。"
    gold = {
        "city": city,
        "days": days,
        "persons": persons,
        "budget": budget,
        "start_date": "2026-11-02",
        "requirements": req,
    }
    return {
        "id": f"ct-poi-{index:02d}",
        "split": "dev" if index < 6 else "confirm",
        "family": family,
        "source_uid": source["uid"],
        "source_original": source,
        "adaptation": {
            "removed": ["origin/intercity", "meals", "accommodation", "transport modes/ticket counts"],
            "changed": ["whole-trip budget -> listed activity admission budget"],
            "added": ["fixed date", "explicit daily windows", "POI pool", "directed synthetic transfers", family],
            "hard_relation": hard,
        },
        "nl": nl,
        "facts": facts,
        "gold": gold,
    }


def prepare(root):
    root = Path(root)
    if (root / "design.json").exists():
        raise ValueError("design already frozen")
    easy = list(csv.DictReader((root / "upstream-data/easy.csv").open(encoding="utf8")))
    medium = list(csv.DictReader((root / "upstream-data/medium.csv").open(encoding="utf8")))
    groups = [
        easy[:10],
        [row for row in medium if row["days"] == "2"][:10],
        [row for row in medium if row["days"] == "3"][:10],
    ]
    sources = [groups[j][i] for i in range(10) for j in range(3)]
    rows = [_case(root, source, index) for index, source in enumerate(sources)]
    if len({r["source_uid"] for r in rows}) != 30:
        raise ValueError("base task leakage")
    save(
        root / "design.json",
        {
            "name": "ChinaTravel-Sinan POI adaptation v1",
            "code_license": "MIT",
            "data_license": "CC-BY-4.0",
            "rows": rows,
            "max_model_calls_per_arm": 3,
            "B_retries": 2,
            "solver_seconds": 2.0,
        },
    )
    return {"dev": 6, "confirm": 24, "days": [r["gold"]["days"] for r in rows]}


def public_case(row):
    """Only this view is passed to an arm. Gold and upstream oracle annotations stay outside."""
    return {key: row[key] for key in ("id", "nl", "facts")}
