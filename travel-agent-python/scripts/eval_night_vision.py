"""更新预约提示的真实复测，以及新建匿名歧义/模糊图；复用累计30元账本。"""

import argparse
import json
import time
from pathlib import Path
from unittest.mock import patch

from app.agent.editing import screenshot
from app.common.model_registry import binding
from app.services.image_intent import _to_data_uri
from scripts.run_p4_acceptance import save
from scripts.screenshot_vision_checks import check_extraction
from scripts.sinan_night_budget import NightBudget, NightClient


def boundary_checks(name, extracted):
    fields = [field for entry in extracted.entries for field in entry.fields]
    questions = extracted.questions + [q for entry in extracted.entries for q in entry.questions]
    checks = {"asks_for_missing_information": bool(questions)}
    if name == "ambiguous":
        checks.update(
            no_guessed_context=not any(
                f.key in {"city", "countryCode", "date", "endDate", "timeZone", "currency"} for f in fields
            ),
            name_not_expanded=all(f.value == "Louvre" for f in fields if f.key in {"name", "localName"}),
            amount_if_present_correct=all(str(f.value) in {"25", "25.0", "25.00"} for f in fields if f.key == "amount"),
            not_mandatory=all(entry.polarity == "recommended" for entry in extracted.entries),
        )
    else:
        checks["no_unreadable_booking_facts"] = not any(
            f.key
            in {
                "name",
                "localName",
                "city",
                "countryCode",
                "date",
                "endDate",
                "startTime",
                "endTime",
                "timeZone",
                "currency",
                "amount",
            }
            for f in fields
        )
    return {"passed": all(checks.values()), "checks": checks}


def run(root, cases=("booking", "ambiguous", "blurred"), label="v1"):
    root = Path(root)
    output = root / ("vision-boundaries.json" if label == "v1" else f"vision-boundaries-{label}.json")
    if output.exists():
        raise ValueError("保留本次真实输出，禁止覆盖或重复调用")
    fixtures = Path("tests/fixtures/screenshots")
    client = NightClient(NightBudget(root / "budget.json"), root / "models", binding("vision"))
    report = {"mode": "real vision; self-made anonymous screenshots; no tools, no retries", "results": []}
    started = time.monotonic()
    for name in cases:
        if time.monotonic() - started > 42:
            raise TimeoutError("本批视觉42秒预算，不再新增调用")
        path = fixtures / ("booking.png" if name == "booking" else f"night-boundaries/{name}.png")
        client.identifier = f"p3-night-{name}-{label}"
        with patch.object(screenshot, "get_llm_client", return_value=client):
            extracted, extractor = screenshot.extract_screenshot(
                _to_data_uri(path.read_bytes()), "guide" if name == "ambiguous" else "booking"
            )
        value = extracted.model_dump(mode="json", by_alias=True)
        checked = (
            check_extraction(name, value, json.loads((fixtures / "booking.json").read_text("utf-8")))
            if name == "booking"
            else boundary_checks(name, extracted)
        )
        report["results"].append(
            {
                "case": name,
                "image": str(path),
                "extractor": extractor,
                "extraction": value,
                "independent_checks": checked,
            }
        )
        report["total_peak_upper_cny"] = str(client.ledger.total())
        save(output, report)
    print(
        json.dumps(
            {
                "checks": {r["case"]: r["independent_checks"]["passed"] for r in report["results"]},
                "total_peak_upper_cny": report["total_peak_upper_cny"],
            }
        )
    )


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--root", type=Path, required=True)
    p.add_argument("--approved", action="store_true")
    p.add_argument(
        "--cases", nargs="+", choices=("booking", "ambiguous", "blurred"), default=("booking", "ambiguous", "blurred")
    )
    p.add_argument("--label", default="v1")
    a = p.parse_args()
    if not a.approved:
        raise SystemExit("需要本夜匿名图/累计30元授权")
    run(a.root, a.cases, a.label)
