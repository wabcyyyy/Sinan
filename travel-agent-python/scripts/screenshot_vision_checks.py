"""对自制匿名图片的手写参考做确定性核对，不调用评审模型。"""

import unicodedata


def normalized(value):
    return "".join(unicodedata.normalize("NFKC", str(value)).casefold().split())


def check_extraction(name, extracted, reference):
    aliases = {
        "guide": ("Musée du Louvre", "卢浮宫", "Musée du Louvre / 卢浮宫"),
        "booking": ("Musée du Louvre", "卢浮宫", "Musée du Louvre / 卢浮宫"),
        "guide_kyoto": ("清水寺", "Kiyomizu-dera", "清水寺 / Kiyomizu-dera"),
        "transport": ("京都駅", "Kyoto Station", "京都駅 / Kyoto Station"),
    }[name]
    entries = extracted.get("entries", [])
    primary = next(
        (
            e
            for e in entries
            if any(
                f["key"] in ("name", "localName") and normalized(f["value"]) in {normalized(a) for a in aliases}
                for f in e["fields"]
            )
        ),
        None,
    )
    checks = [{"check": "primary_record", "passed": primary is not None}]
    if primary is not None:
        expected = reference["entries"][0]
        values = {f["key"]: f["value"] for f in primary["fields"]}
        for field in expected["fields"]:
            if field["key"] in ("name", "note"):
                continue
            checks.append(
                {
                    "check": f"field:{field['key']}",
                    "passed": normalized(values.get(field["key"], "")) == normalized(field["value"]),
                }
            )
        checks.append({"check": "kind", "passed": primary["kind"] == expected["kind"]})
    if name == "guide":
        checks.append(
            {
                "check": "negative_separate_record",
                "passed": any(
                    e["polarity"] == "excluded" and any(normalized(f["value"]) == "louvre-lens" for f in e["fields"])
                    for e in entries
                ),
            }
        )
    lines = [f for e in reference["entries"] for f in e["fields"]]
    for i, entry in enumerate(entries):
        for field in entry["fields"]:
            quoted = normalized(field["quote"])
            matches = [line for line in lines if quoted and quoted in normalized(line["quote"])]
            checks.append({"check": f"quote:{i}:{field['key']}", "passed": bool(matches)})
            region = field["region"]
            cx, cy = region["x"] + region["width"] / 2, region["y"] + region["height"] / 2
            on_line = any(
                line["region"]["x"] - 0.02 <= cx <= line["region"]["x"] + line["region"]["width"] + 0.02
                and line["region"]["y"] - 0.02 <= cy <= line["region"]["y"] + line["region"]["height"] + 0.02
                for line in matches
            )
            checks.append({"check": f"region_row:{i}:{field['key']}", "passed": on_line})
    return {
        "passed": all(c["passed"] for c in checks),
        "checks": checks,
        "scope": "hand-authored field values, verbatim quotes and region centers on reference rows; not tight-box IoU",
    }
