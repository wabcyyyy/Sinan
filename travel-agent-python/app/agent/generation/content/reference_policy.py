"""参考资料池的两条策略：用途判定（T5b）与排程前资格（T5a）。

从 `reference_pool` 拆出（那个文件受 >400 行门禁，且它的职责是编号/渲染/落地，
不是判定）。本模块只回答两个问题：

1. **这一项是干什么用的**（`item_purpose`）：池内行的 `category` 是**检索分类**
   （OTM 的 foods/interesting_places、Nominatim/Overpass 的 OSM 主键
   amenity/shop/tourism），不是这一项在行程里的用途。用途只能由条目自己申报，
   唯一允许的修正方向是升为用餐——饮食硬约束闸按用途取项，用途被检索类别降级
   等于换一条检索域就静默关掉闸（实测缺陷，T5b）。
2. **这一行今天有没有资格被端给模型**（`apply_eligibility`）：饮食要求与当天锚点
   已知时，不合格的行根本不进资料块；此前饮食行只是在检索层排得靠前
   （`tools/impl.search_foods`），实体最终仍由模型挑（T5a）。

判据一律取自 `rules`：类型归一（`generation_core.normalize_item_type`）、饮食闸
是否阻断（`day_policy.diet_gate_blocking`，与交付闸同一份）、餐饮 amenity 集合
（`category_evidence.DINING_AMENITIES`）、半径（`day_policy.MEAL_BRANCH_MAX_DISTANCE_M`，
与落地后的就近校正同一份）。这里不立第二口径。

依赖：core.poi_identity、rules、grounding_labels、schemas；无上层依赖。
"""

from collections.abc import Mapping, Sequence
from typing import Any

from app.agent.core.poi_identity import haversine_m
from app.agent.generation.rules.category_evidence import DINING_AMENITIES
from app.agent.generation.rules.day_policy import MEAL_BRANCH_MAX_DISTANCE_M, diet_gate_blocking
from app.agent.generation.rules.generation_core import normalize_item_type
from app.agent.grounding.grounding_labels import has_valid_coords
from app.schemas.trip_requirements import TripRequirements


def is_dining_row(row: Mapping[str, Any] | None) -> bool:
    """这一行代表的是"吃饭"这件事吗（与它从哪个检索域回来无关）。

    两条证据通道：所在检索域是餐饮池，或外部取回的类别证据 `amenity` 落在餐饮
    集合里（Overpass/Nominatim 的素食餐厅常从景点域回来）。
    """
    if not isinstance(row, Mapping):
        return False
    for key in ("_reference_category", "category"):
        if str(row.get(key) or "").strip().lower() == "food":
            return True
    tags = row.get("category_tags")
    return isinstance(tags, Mapping) and str(tags.get("amenity") or "").strip().lower() in DINING_AMENITIES


def item_purpose(declared: object, row: Mapping[str, Any] | None) -> str:
    """行程项用途的唯一判定：检索类别不得改写申报用途，只允许升为用餐。

    - 申报 food 的项经过景点检索域（或任何域）回来仍然是 food——饮食闸照样查它；
      反过来 OSM 主键（amenity/shop）绝不会被写成 item_type（那越出契约值域）。
    - 申报 attraction 但落到的实体是用餐实体时升为 food：模型把餐厅写成景点
      不能绕过饮食检查。

    两个方向合起来是一条纪律：**只加约束，不减约束**。
    """
    purpose = normalize_item_type(declared)
    if purpose == "food" or is_dining_row(row):
        return "food"
    return purpose


def _detour_m(row: Mapping[str, Any], anchors: Sequence[tuple[float, float]]) -> float | None:
    """这行到最近锚点的距离；行上没有效坐标时返回 None（没有参照就不判）。"""
    if not has_valid_coords(row):
        return None
    lat, lng = float(row["latitude"]), float(row["longitude"])
    return min(haversine_m(lat, lng, anchor_lat, anchor_lng) for anchor_lat, anchor_lng in anchors)


def row_eligible(row: Mapping[str, Any], requirements: TripRequirements | None, anchors: Sequence) -> bool:
    """这一行有没有资格进资料块（判据全部来自 `rules`，本模块只做组合）。"""
    diet = str(getattr(requirements, "dietary_requirement", "") or "")
    if diet and diet_gate_blocking(requirements) and diet not in (row.get("dietary_options") or ()):
        return False
    if anchors:
        detour = _detour_m(row, anchors)
        if detour is not None and detour > MEAL_BRANCH_MAX_DISTANCE_M:
            return False
    return True


def apply_eligibility(
    rows: list[dict], requirements: TripRequirements | None, anchors: Sequence[tuple[float, float]]
) -> tuple[list[dict], dict[str, int]]:
    """排程前定资格：把"饮食行排序优先、模型仍可选到远处/无证那家"升级为
    "不合格的行根本不进资料块"。

    只在**确有合格行**时才摘除：全部不合格（Overpass 关掉、城市覆盖不足、当天还
    没有锚点）时原样注入并如实计数——把餐饮域整个清空只会把"检索没覆盖到"伪装成
    "这个城市没有餐厅"。硬约束该拦的仍由交付闸（`rules.constraint_checks`）拦，
    这里既不放宽也不自行清空。
    """
    dining_idx = [index for index, row in enumerate(rows) if is_dining_row(row)]
    if not dining_idx:
        return rows, {}
    qualified = {index for index in dining_idx if row_eligible(rows[index], requirements, anchors)}
    if not qualified or len(qualified) == len(dining_idx):
        return rows, {"dining_unqualified": len(dining_idx) - len(qualified)}
    dropped = set(dining_idx) - qualified
    return [row for index, row in enumerate(rows) if index not in dropped], {
        "dining_eligible": len(qualified),
        "dining_dropped": len(dropped),
    }


def day_anchors(context: dict | None, chosen_hotel: str | None) -> list[tuple[float, float]]:
    """排程前就已知的当天锚点：只有已选定酒店的坐标。

    当天景点是模型排出来的，排程前不存在，那种远处分店由落地后的就近校正
    （`landing.relocate_far_meals`）负责，两处读同一个半径常量。
    `context["used_points"]` 装的是**别的天**已成功点位（见
    `itinerary_generation._other_completed_points`，它明确排除当日），拿它当今天的
    参照会把"今天在另一个片区"误判成"这家店太远"。没有锚点就不筛。
    """
    name = str(chosen_hotel or "").strip()
    if not name:
        return []
    for row in (context or {}).get("hotels") or []:
        if isinstance(row, dict) and str(row.get("name") or "").strip() == name and has_valid_coords(row):
            return [(float(row["latitude"]), float(row["longitude"]))]
    return []
