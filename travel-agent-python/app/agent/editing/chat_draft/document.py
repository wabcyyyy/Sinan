"""行程 JSON 的投影（project）与补水（hydrate）。

职责：
- _trip_plan_document / _decision_plan_document：把业务侧 req.plans 投影成
  给模型的精简 JSON（仅保留身份/时间/语义字段，价格坐标等权威字段留业务侧）；
- _hydrate_decision_plans：把模型返回的精简计划合并回完整权威字段；
- _extract_document_plans：校验并提取模型给出的整份 plan_document。

实现要点：
- “投影/补水”双层结构让模型无需复述上千个无变化字段，也防止模型篡改权威数据；
- 保护字段（城市/人数/预算等）由业务侧维护，模型只能通过 item id 引用既有项、
  或新增不带 id 的项目，酒店项被显式禁止新增；
- _extract_document_plans 会校验元数据未变、天数合法、day_no 连续，非法则返回 None。

依赖：intent（读取目标天数）；对外被 plan_edit / hotel / decide 调用。
"""

import logging
from copy import deepcopy

from app.schemas.trip import (
    MAX_TRIP_DAYS,
    ChatTurnRequest,
)

from .intent import _requested_day_count

logger = logging.getLogger(__name__)


def _trip_plan_document(req: ChatTurnRequest) -> dict:
    """每轮都把关系型行程投影成一份完整 JSON，作为 LLM 唯一可编辑的计划状态。"""
    document = {
        "schema_version": 1,
        "trip": {
            "city": req.city,
            "days": req.days,
            "persons": req.persons,
            "budget": req.budget,
            "start_date": req.start_date,
            "end_date": req.end_date,
            "preferences": req.preferences,
            "hotel_tier": req.hotel_tier,
        },
        "days": req.plans,
        "pending_action": None,
    }
    # 实际花费（C3.4）放顶层、不进受保护的 trip 元数据块：模型只需读取它来理解
    # "超支/剩余预算"，不允许也不会被要求在 plan_document 里回显（_extract_document_plans
    # 的元数据一致性校验因此不受影响）。
    if req.spent_total is not None:
        document["spent"] = {
            "total": req.spent_total,
            "by_category": req.spent_by_category,
            "other_currencies": req.spent_other_currencies,
        }
    return document


def _decision_plan_document(req: ChatTurnRequest) -> dict:
    """给模型的精简计划。

    对话修改只需要项目身份、时间和语义信息。价格、坐标等权威字段继续留在
    业务侧，避免一次简单删减也要求模型原样复述数千个无变化字段。
    """
    document = _trip_plan_document(req)
    document["days"] = [
        {
            "day_no": plan.get("day_no"),
            "note": plan.get("note"),
            "items": [
                {
                    key: item.get(key)
                    for key in (
                        "id",
                        "item_type",
                        "poi_name",
                        "address",
                        "start_time",
                        "end_time",
                        "duration_min",
                        "tag",
                        "remark",
                    )
                    if item.get(key) is not None
                }
                for item in (plan.get("items") or [])
            ],
        }
        for plan in req.plans
    ]
    return document


def _hydrate_decision_plans(compact_plans: list[dict], req: ChatTurnRequest) -> list[dict] | None:
    """把模型返回的精简计划合并回业务侧的完整权威字段。"""
    existing_by_id = {
        int(item["id"]): item
        for plan in req.plans
        for item in (plan.get("items") or [])
        if isinstance(item.get("id"), int)
    }
    seen_ids: set[int] = set()
    hydrated: list[dict] = []
    allowed_keys = {
        "id",
        "item_type",
        "poi_name",
        "address",
        "start_time",
        "end_time",
        "duration_min",
        "tag",
        "remark",
    }
    for compact_plan in compact_plans:
        if not isinstance(compact_plan, dict) or not isinstance(compact_plan.get("items"), list):
            return None
        full_plan = deepcopy(compact_plan)
        full_items = []
        for compact_item in compact_plan["items"]:
            if not isinstance(compact_item, dict):
                return None
            item_id = compact_item.get("id")
            if item_id is not None:
                if not isinstance(item_id, int) or item_id not in existing_by_id or item_id in seen_ids:
                    return None
                seen_ids.add(item_id)
                original = existing_by_id[item_id]
                # 模型不能借普通编辑直接改变酒店，也不能把旧项目 id 套给另一个 POI。
                if original.get("item_type") == "hotel" and any(
                    compact_item.get(key) != original.get(key) for key in allowed_keys if key in compact_item
                ):
                    return None
                if compact_item.get("poi_name") != original.get("poi_name"):
                    return None
                merged = deepcopy(original)
                merged.update({key: value for key, value in compact_item.items() if key in allowed_keys})
                full_items.append(merged)
            else:
                if compact_item.get("item_type") == "hotel" or not compact_item.get("poi_name"):
                    return None
                full_items.append({key: value for key, value in compact_item.items() if key in allowed_keys})
        full_plan["items"] = full_items
        hydrated.append(full_plan)
    return hydrated


def _extract_document_plans(data: dict, req: ChatTurnRequest) -> list[dict] | None:
    document = data.get("plan_document")
    if not isinstance(document, dict) or document.get("schema_version") != 1:
        return None
    trip = document.get("trip")
    if not isinstance(trip, dict):
        return None
    # 行程元数据由业务系统维护，模型不得借修改内容之名改变预算、人数或城市。
    expected = _trip_plan_document(req)["trip"]
    requested_days = _requested_day_count(req.message, req.days)
    target_days = requested_days or req.days
    if target_days < 1 or target_days > MAX_TRIP_DAYS:
        return None
    protected_keys = set(expected) - {"days", "end_date"}
    if any(trip.get(key) != expected[key] for key in protected_keys):
        return None
    if trip.get("days") not in {req.days, target_days}:
        return None
    plans = document.get("days")
    if not isinstance(plans, list) or len(plans) != target_days:
        return None
    valid_days = {
        int(day_no) for plan in plans if isinstance(plan, dict) and isinstance(day_no := plan.get("day_no"), int)
    }
    if valid_days != set(range(1, target_days + 1)):
        return None
    return _hydrate_decision_plans(plans, req)
