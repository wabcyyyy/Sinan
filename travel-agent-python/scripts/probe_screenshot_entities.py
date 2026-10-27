"""P3 仅一次免费京都站点名查询；不含图片、用户订单、模型、路线或部署。"""

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.agent.data.places import geocode_place_rows
from app.agent.grounding.screenshot_entities import screenshot_entity_choices
from app.agent.runtime.run_limits import RunLimits, use_task_limits


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--online", action="store_true", required=True)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    with use_task_limits(RunLimits(deadline_seconds=45, max_llm_calls=0, max_retrievals=1)):
        rows = geocode_place_rows("京都駅", "京都", namedetails=True, country_code="JP")
    choices = screenshot_entity_choices(rows or [], "JP")
    payload = {
        "recorded_utc": datetime.now(UTC).isoformat(),
        "source": "Nominatim / OpenStreetMap",
        "query": {"name": "京都駅", "city": "京都", "country_code": "JP"},
        "logical_lookups": 1,
        "status": "unavailable" if rows is None else "found" if rows else "not_found",
        "rows": rows,
        "adapter_choices": [c.model_dump(mode="json", by_alias=True) for c in choices or []],
        "model_calls": 0,
        "route_calls": 0,
        "attribution": "© OpenStreetMap contributors, ODbL; explicit named lookup, no autocomplete or bulk extraction",
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                "output": str(args.output),
                "status": payload["status"],
                "choices": [{"id": c.entity_id, "name": c.name, "type": c.entity_type} for c in choices or []],
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
