"""Eight new self-made anonymous fixtures, bounded vision transport and field-level evaluation.

Uses only the existing upgraded SINAN-NL ledger. Missing-field questions are reported separately from accuracy.
"""

import base64
import hashlib
import json
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from PIL import Image, ImageDraw, ImageFilter, ImageFont

from app.agent.editing import screenshot
from app.common.model_registry import binding
from app.services.image_intent import _to_data_uri
from scripts.run_p4_acceptance import save
from scripts.sinan_night_budget import NightClient
from scripts.sinan_nl_budget import NIGHT_UPGRADE, NLBudget

FIELDS = ("name", "date", "endDate", "timeZone", "amount", "currency")
CASES = [
    (
        "zero-paris",
        "booking",
        [
            "Reservation: Musée du Louvre",
            "Paris / France FR",
            "Start: 2026-11-04 14:00",
            "End: 2026-11-04 15:30",
            "Local time zone: Europe/Paris",
            "Ticket: EUR 0.00",
        ],
        {
            "name": "Musée du Louvre",
            "date": "2026-11-04",
            "endDate": "2026-11-04",
            "timeZone": "Europe/Paris",
            "amount": "0",
            "currency": "EUR",
        },
    ),
    (
        "kyoto-guide",
        "guide",
        [
            "おすすめ 清水寺 / Kiyomizu-dera",
            "京都 / 日本 JP",
            "参考候補です。必須ではありません。",
        ],
        {"name": "清水寺"},
    ),
    (
        "lisbon-missing-date",
        "booking",
        [
            "Reservation: Museu Nacional do Azulejo",
            "Lisboa / Portugal PT",
            "Visit: 10:30 - 12:00",
            "Local time zone: Europe/Lisbon",
            "Ticket: EUR 12.00",
            "Date: not provided",
        ],
        {"name": "Museu Nacional do Azulejo", "timeZone": "Europe/Lisbon", "amount": "12", "currency": "EUR"},
    ),
    (
        "louvre-ambiguous",
        "guide",
        [
            "Louvre",
            "Branch and city: not selected",
            "Suggested time: 14:00",
            "Price: $25.00",
            "This is a candidate, not a mandatory visit.",
        ],
        {"name": "Louvre", "amount": "25"},
    ),
    (
        "bangkok-foreign",
        "booking",
        [
            "Reservation: Wat Pho",
            "Bangkok / Thailand TH",
            "Start: 2026-11-06 09:00",
            "End: 2026-11-06 10:30",
            "Local time zone: Asia/Bangkok",
            "Ticket: THB 300.00",
        ],
        {
            "name": "Wat Pho",
            "date": "2026-11-06",
            "endDate": "2026-11-06",
            "timeZone": "Asia/Bangkok",
            "amount": "300",
            "currency": "THB",
        },
    ),
    (
        "overnight-station",
        "transport",
        [
            "Departure station: 京都駅 / Kyoto Station",
            "Arrival station: 東京駅 / Tokyo Station",
            "Departure: 2026-11-06 23:10",
            "Arrival: 2026-11-07 06:20",
            "Local time zone: Asia/Tokyo",
            "Fare: JPY 1500",
            "Synthetic route, not a real timetable.",
        ],
        {
            "name": "京都駅",
            "date": "2026-11-06",
            "endDate": "2026-11-07",
            "timeZone": "Asia/Tokyo",
            "amount": "1500",
            "currency": "JPY",
        },
    ),
    (
        "unreadable-fields",
        "booking",
        [
            "Museum reservation",
            "Venue: [unreadable]",
            "Start: 2026-11-07 10:00",
            "End: 2026-11-07 11:00",
            "Local time zone: Asia/Tokyo",
            "Currency: JPY",
            "Amount: [unreadable]",
        ],
        {"date": "2026-11-07", "endDate": "2026-11-07", "timeZone": "Asia/Tokyo", "currency": "JPY"},
    ),
    (
        "same-city-branch",
        "guide",
        [
            "Tokyo National Museum",
            "Tokyo / Japan JP",
            "Building options: Honkan / Toyokan",
            "Building: not selected",
            "Candidate only. Ask which building before confirming.",
        ],
        {"name": "Tokyo National Museum"},
    ),
]


def prepare(root):
    root.mkdir(parents=True, exist_ok=False)
    font = ImageFont.truetype("C:/Windows/Fonts/msyh.ttc", 26)
    rows = []
    for identifier, kind, lines, gold in CASES:
        image = Image.new("RGB", (1100, 760), "white")
        draw = ImageDraw.Draw(image)
        draw.text((25, 15), "SINAN SELF-MADE ANONYMOUS TEST / NOT A REAL ORDER", font=font, fill="black")
        for index, line in enumerate(lines):
            draw.text((35, 100 + index * 75), line, font=font, fill="black")
        if identifier == "unreadable-fields":
            for box in ((125, 170, 1050, 235), (145, 545, 1050, 610)):
                image.paste(image.crop(box).filter(ImageFilter.GaussianBlur(9)), box[:2])
        path = root / f"{identifier}.png"
        image.save(path)
        rows.append(
            {
                "id": identifier,
                "kind": kind,
                "image": str(path.resolve()),
                "gold": gold,
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "wire_sha256": hashlib.sha256(
                    base64.b64decode(_to_data_uri(path.read_bytes()).split(",", 1)[1])
                ).hexdigest(),
                "lines": lines,
            }
        )
    save(root / "manifest.json", {"scope": "eight self-made anonymous images, no private inputs", "rows": rows})
    return rows


class VisionBudget(NLBudget):
    def __init__(self, path, manifest, *, approved=False):
        super().__init__(path, approved=approved)
        if self.state.get("approval_upgrade") != NIGHT_UPGRADE or self.state["cap_cny"] != "30":
            raise ValueError("eight-image scope requires the explicit cumulative night upgrade")
        self.allowed = {row["wire_sha256"] for row in json.loads(Path(manifest).read_text("utf8"))["rows"]}
        if len(self.allowed) != 8:
            raise ValueError("only the eight frozen anonymous images are authorized")

    def reserve(self, identifier, payload):
        messages = payload.get("messages", [])
        if len(messages) != 1 or messages[0].get("role") != "user":
            raise ValueError("single-image extraction only")
        blocks = messages[0].get("content", [])
        images = [b for b in blocks if b.get("type") == "image_url"]
        texts = [b.get("text", "") for b in blocks if b.get("type") == "text"]
        if len(blocks) != 2 or len(images) != 1 or len(texts) != 1:
            raise ValueError("single image and extraction instruction only")
        url = images[0].get("image_url", {}).get("url", "")
        prefix = "data:image/jpeg;base64,"
        if not url.startswith(prefix) or len(url) > 400000:
            raise ValueError("reencoded JPEG payload scope exceeded")
        content = base64.b64decode(url[len(prefix) :], validate=True)
        if hashlib.sha256(content).hexdigest() not in self.allowed:
            raise ValueError("image is not one of the frozen anonymous fixtures")
        # Same full-context upper bound as text; image tokens are covered by the model's 1M context cap.
        super().reserve(identifier, {**payload, "messages": [{"role": "user", "content": texts[0]}]})


def evaluate(extracted, gold):
    fields = [field for entry in extracted.entries for field in entry.fields]
    values = {key: [str(f.value) for f in fields if f.key == key] for key in FIELDS}
    result = {}
    for key in FIELDS:
        observed = values[key]
        expected = gold.get(key)
        if expected is None:
            result[key] = {"reference": None, "values": observed, "safe_omission": not observed}
        else:

            def norm(value, key=key):
                if key == "amount":
                    return str(Decimal(value).normalize())
                return value

            result[key] = {
                "reference": expected,
                "values": observed,
                "recognized": bool(observed) and all(norm(v) == norm(expected) for v in observed),
            }
    return {
        "fields": result,
        "questions": extracted.questions + [q for e in extracted.entries for q in e.questions],
        "entity_confirmation_required": True,
        "human_review": "not_performed",
    }


def execute(root, ledger_path, start, stop, *, label="first"):
    if not 0 <= start < stop <= 8 or stop - start > 2:
        raise ValueError("at most two vision calls per90-second batch")
    manifest = root / "manifest.json"
    rows = json.loads(manifest.read_text("utf8"))["rows"]
    ledger = VisionBudget(ledger_path, manifest, approved=True)
    client = NightClient(ledger, root / "models", binding("vision"))
    if label not in {"first", "departure-rule"} or (label == "departure-rule" and (start, stop) != (5, 6)):
        raise ValueError("only one justified transport regression, retain all first outputs")
    output = root / ("results" if label == "first" else "regression")
    output.mkdir(exist_ok=True)
    results = []
    for row in rows[start:stop]:
        path = output / f"{row['id']}.json"
        if path.exists():
            results.append(json.loads(path.read_text("utf8")))
            continue
        client.identifier = "night-vision-" + row["id"] + ("" if label == "first" else "-" + label)
        try:
            with patch.object(screenshot, "get_llm_client", return_value=client):
                extracted, provider = screenshot.extract_screenshot(
                    _to_data_uri(Path(row["image"]).read_bytes()), row["kind"]
                )
            result = {
                "id": row["id"],
                "status": "extracted",
                "extraction": extracted.model_dump(mode="json", by_alias=True),
                "provider": provider,
                "evaluation": evaluate(extracted, row["gold"]),
            }
        except ValueError as error:
            # A retained, billed raw response can fail the product schema. Never discard or silently fix it.
            if not (root / "models" / f"{client.identifier}-response.json").exists():
                raise
            result = {
                "id": row["id"],
                "status": "invalid_extraction",
                "error": str(error),
                "provider": "vision:" + client.model,
                "extraction": None,
                "evaluation": {
                    "fields": {
                        key: {"reference": value, "values": [], "recognized": False}
                        for key, value in row["gold"].items()
                    },
                    "human_review": "not_performed",
                    "questions": [],
                    "entity_confirmation_required": True,
                },
            }
        save(path, result)
        results.append(result)
    return results
