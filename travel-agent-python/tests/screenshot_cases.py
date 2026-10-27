"""自制匿名截图的开发参考；抽取回放不是模型准确率结果。"""

import json
from pathlib import Path

from app.schemas.planning import ConstraintPlanningOptions
from app.schemas.screenshot import ScreenshotExtraction
from app.schemas.trip_requirements import TripRequirements

ROOT = Path(__file__).parent / "fixtures/screenshots"


def extraction(kind="guide"):
    return ScreenshotExtraction.model_validate(json.loads((ROOT / f"{kind}.json").read_text(encoding="utf-8")))


def requirements():
    return TripRequirements(
        constraint_planning=ConstraintPlanningOptions(
            country_code="FR",
            time_zone="Europe/Paris",
            currency="EUR",
        )
    )


def paris_rows():
    payload = Path(__file__).parent / "fixtures/constraint_planning/paris_observation_20261024.json"
    return [json.loads(payload.read_text(encoding="utf-8"))["row"]]


def resolved_entity():
    from app.schemas.screenshot import ScreenshotEntity

    row = paris_rows()[0]
    return ScreenshotEntity(
        entity_id=row["place_id"],
        name=row["name"],
        aliases=row["aliases"],
        address=row["display_name"],
        country_code="FR",
        latitude=row["latitude"],
        longitude=row["longitude"],
        source_url=row["source_url"],
        retrieved_at=row["source_fetched_at"],
        category=row["category"],
        entity_type=row["type"],
    )


def station_entity():
    from app.schemas.screenshot import ScreenshotEntity

    observed = json.loads((ROOT / "kyoto_station_observation_20261024.json").read_text(encoding="utf-8-sig"))
    # 明确选一个返回的站点，不声称是统一的京都站或某运营商的唯一入口。
    return ScreenshotEntity.model_validate(observed["adapter_choices"][0])


def kyoto_entity():
    from app.agent.grounding.screenshot_entities import screenshot_entity_choices

    observed = json.loads((ROOT / "kyoto_place_observation_20261024.json").read_text(encoding="utf-8-sig"))
    return screenshot_entity_choices(observed["observed"]["rows"], "JP")[0]
