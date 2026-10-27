"""Explicit frozen ChinaTravel provider. No network fallback, oracle access or implicit calendar date."""

import json
import sys
from contextlib import contextmanager
from contextvars import ContextVar
from functools import lru_cache
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
WORK = ROOT / "docs/benchmarks/sinan-chinatravel-original-20261026/official"
_usage: ContextVar[dict[str, int] | None] = ContextVar("chinatravel_planning_query_usage", default=None)


@contextmanager
def measure_calls():
    counts = {"planning_goto_calls_excluding_official_checker": 0}
    token = _usage.set(counts)
    try:
        yield counts
    finally:
        _usage.reset(token)


@lru_cache(maxsize=1)
def world():
    if not (WORK / "chinatravel/environment/database").is_dir():
        raise ValueError("frozen official sandbox is not prepared")
    sys.path.insert(0, str(WORK))
    from chinatravel.environment.world_env import WorldEnv

    return WorldEnv(lang="zh")


def enabled(requirements):
    return requirements is not None and requirements.fact_source == "chinatravel"


def records(frame):
    if isinstance(frame, str):
        raise ValueError(frame)
    return json.loads(frame.to_json(orient="records", force_ascii=False))


def research(request):
    env = world()
    if not request.origin_city:
        raise ValueError("official travel planning needs a parsed origin_city")
    context = {
        "fact_source": "chinatravel",
        "relative_days": True,
        "origin_city": request.origin_city,
        "target_city": request.city,
    }
    for key, tool in (("candidates", env.attractions), ("foods", env.restaurants), ("hotels", env.accommodations)):
        # Frozen source order is shared by all arms; no feasible itinerary is selected here.
        rows = records(tool.select(request.city, "name", lambda _x: True))
        context[key] = rows
    context["intercity"] = {}
    for label, first, second in (
        ("outbound", request.origin_city, request.city),
        ("return", request.city, request.origin_city),
    ):
        context["intercity"][label] = {
            mode: records(env.intercitytransport.select(first, second, mode)) for mode in ("train", "airplane")
        }
    context["flight_quotes"] = []
    context["weather"] = []
    return context


def common_issues(plan):
    """Only schema/common facts; query Gold DSL never enters this module."""
    world()
    import jsonschema
    from chinatravel.symbol_verification import commonsense_constraint as checks

    issues = []
    schema = json.loads((WORK / "chinatravel/evaluation/output_schema.json").read_text("utf8"))
    validator = jsonschema.Draft7Validator(schema)
    issues.extend(f"schema {list(error.path)}: {error.message}" for error in validator.iter_errors(plan))
    if any(key not in plan for key in ("start_city", "target_city", "people_number", "itinerary")):
        return issues
    public = {k: plan[k] for k in ("start_city", "target_city", "people_number")}
    for name in (
        "Is_activity_grounded",
        "Is_intercity_transport_correct",
        "Is_attractions_correct",
        "Is_hotels_correct",
        "Is_restaurants_correct",
        "Is_transport_correct",
        "Is_time_correct",
        "Is_space_correct",
    ):
        try:
            table, errors = getattr(checks, name)(public, plan)
            if table.to_numpy().sum() != 0:
                issues.extend(f"{name}: {error}" for error in errors or ["constraint failed"])
        except Exception as exc:
            issues.append(f"{name}: {type(exc).__name__}: {exc}")
    return issues


def route(city, first, second, departure, mode):
    counts = _usage.get()
    if counts is not None:
        counts["planning_goto_calls_excluding_official_checker"] += 1
    result = world().transportation.goto(city, first, second, departure, mode)
    if not isinstance(result, list):
        raise ValueError(f"official goto failed: {result}")
    return result
