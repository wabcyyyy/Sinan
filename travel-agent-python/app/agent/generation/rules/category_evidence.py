"""类别证据的资格与三态判定；未知类别不能推翻保守名称判据。"""

from app.schemas.trip import FactEvidence

#: OSM `amenity` 里表示"用餐"的取值。用途判定（content/reference_pool.item_purpose）
#: 与"排除类别的正向冲突证据"（下面的 `_DISTINCT_USES`）读同一份餐饮口径：
#: 一个实体算不算吃饭的地方，只能有一个答案（T5b：类型被改写会静默关掉饮食闸）。
DINING_AMENITIES = {"restaurant", "cafe", "fast_food", "food_court"}

# 明确的独立用途；泛泛的 tourism=attraction 或缺少 religion 均不够。
_DISTINCT_USES = {
    "leisure": {"park", "garden", "pitch", "sports_centre"},
    "shop": {"gift", "books", "supermarket", "convenience"},
    "amenity": DINING_AMENITIES,
}
_TEMPLE_RELIGIONS = {"buddhist", "shinto"}
_OTHER_WORSHIP_RELIGIONS = {"christian", "muslim", "jewish"}


def _bound_observation(raw: object, identity: object) -> bool:
    """只用已验证的观测事实，且须与已验证地点指向同一来源实体。"""
    try:
        fact = FactEvidence.model_validate(raw)
        place = FactEvidence.model_validate(identity)
    except (TypeError, ValueError):
        return False
    return (
        all(
            evidence.verification_status == "verified"
            and evidence.value_kind == "observed"
            and evidence.freshness_status != "stale"
            and bool((evidence.provider or "").strip() and (evidence.source_url or "").strip())
            for evidence in (fact, place)
        )
        and fact.provider == place.provider
        and fact.source_url == place.source_url
    )


def item_category_evidence(items: list[dict]) -> list[tuple[str, dict[str, str]]]:
    pairs: list[tuple[str, dict[str, str]]] = []
    for item in items:
        name = str(item.get("poi_name") or item.get("name") or "")
        raw_facts = item.get("fact_evidence") or item.get("factEvidence") or {}
        facts = raw_facts if isinstance(raw_facts, dict) else {}
        tags: dict[str, str] = {}
        for key, value in facts.items():
            text = str(key)
            if not text.startswith("category:") or not _bound_observation(value, facts.get("identity")):
                continue
            category_key, separator, category_value = text[len("category:") :].partition("=")
            if separator and category_key and category_value:
                tags[category_key] = category_value
        pairs.append((name, tags))
    return pairs


def category_membership(category: str, tags: dict[str, str]) -> bool | None:
    """True=命中、False=有明确不同用途、None=证据不足；正向冲突证据优先阻断。"""
    if category == "temple":
        if (
            tags.get("religion") in _TEMPLE_RELIGIONS
            or tags.get("historic") in {"temple", "shrine"}
            or tags.get("building") in {"temple", "shrine"}
        ):
            return True
        if tags.get("amenity") == "place_of_worship":
            return False if tags.get("religion") in _OTHER_WORSHIP_RELIGIONS else None
    elif category == "museum":
        if tags.get("tourism") == "museum" or tags.get("amenity") == "museum":
            return True
    else:
        return None
    if any(tags.get(key) in values for key, values in _DISTINCT_USES.items()):
        return False
    return None
