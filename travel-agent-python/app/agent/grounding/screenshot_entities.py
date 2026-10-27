"""显式查询一次地点，保留跨语言别名和多个馆区，不用视觉坐标代替实体定位。"""

import unicodedata

from app.agent.data.places import geocode_place_rows
from app.schemas.screenshot import ScreenshotEntity


def resolve_screenshot_place(
    name: str, city: str, country_code: str, kind: str = "place"
) -> list[ScreenshotEntity] | None:
    rows = geocode_place_rows(name, city, namedetails=True, country_code=country_code)
    if rows is None:
        return None
    choices = screenshot_entity_choices(rows, country_code)

    def normalize(value):
        return "".join(c for c in unicodedata.normalize("NFKC", value).casefold() if c.isalnum())

    query, locality = normalize(name), normalize(city)
    for choice in choices:
        if query == normalize(choice.name):
            choice.match_score += 60
            choice.match_reasons.append("名称一致")
        elif query in {normalize(a) for a in choice.aliases}:
            choice.match_score += 50
            choice.match_reasons.append("跨语言别名一致")
        if locality and locality in normalize(choice.address):
            choice.match_score += 20
            choice.match_reasons.append("地址包含查询城市")
        if kind == "transport":
            matched = choice.entity_type in {
                "station",
                "halt",
                "stop_position",
                "bus_station",
                "ferry_terminal",
                "aerodrome",
                "airport",
            }
            choice.match_score += 20 if matched else -50
            choice.match_reasons.append("交通设施类别匹配" if matched else "类别与交通资料不一致，请核对")
        if not choice.match_reasons:
            choice.match_reasons.append("仅名称检索召回，具体馆区待确认")
    return sorted(choices, key=lambda c: (-c.match_score, c.entity_id))[:5]


def screenshot_entity_choices(rows: list[dict], country_code: str) -> list[ScreenshotEntity]:
    choices = []
    seen = set()
    for row in rows:
        entity = row.get("place_id")
        if (
            not entity
            or entity in seen
            or row.get("country_code") != country_code
            or not row.get("source_url")
            or not row.get("name")
        ):
            continue
        lat, lon = row.get("latitude"), row.get("longitude")
        if lat is None or lon is None or not -90 <= lat <= 90 or not -180 <= lon <= 180 or (lat == 0 and lon == 0):
            continue
        seen.add(entity)
        choices.append(
            ScreenshotEntity(
                entity_id=entity,
                name=row["name"],
                aliases=(row.get("aliases") or [])[:40],
                address=row.get("display_name") or row["name"],
                country_code=country_code,
                latitude=lat,
                longitude=lon,
                source_url=row["source_url"],
                retrieved_at=row.get("source_fetched_at"),
                category=row.get("category") or "",
                entity_type=row.get("type") or "",
            )
        )
    return choices
