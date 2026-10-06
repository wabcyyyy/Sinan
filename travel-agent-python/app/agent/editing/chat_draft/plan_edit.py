"""行程编辑的执行层（executor）：把模型提案落地成具体 JSON 草稿。

职责：
- _apply_decision_patches：解释模型的小修补丁（move/delete/update/add/set_day_note）；
- _apply_plan_update：按 mode 路由——rewrite_plan 走整份文档，plan_update 走补丁；
- _deterministic_reduce / _deterministic_extend：模型失败时的确定性兜底
  （精简/去重 / 用单日生成器补齐新增天）；
- _dedupe_plans：最终的跨天去重兜底。

实现要点：
- 这是“LLM 当规划者、代码当执行者”的核心：模型只产出结构化意图，
  真正改写内存 JSON 的永远是这里的确定性代码；
- 所有入口返回 list[dict] 或 None（结构不合法时），由 decide 层决定重试或兜底；
- 不改变任何权威/酒店字段，酒店差异由 decide 层统一校验。

依赖：intent（天数解析）、document（文档提取）、validate（时钟/冲突）。
"""

import logging
import re
from copy import deepcopy

from app.agent.generation.orchestration.day_stream import run_generate_day
from app.agent.generation.orchestration.plan_context import run_plan_context
from app.schemas.trip import (
    MAX_TRIP_DAYS,
    ChatTurnRequest,
    GenerateDayRequest,
)

from .document import _extract_document_plans
from .intent import _requested_day_count
from .validate import _clock_minutes, _format_clock, _reschedule_moved_item

logger = logging.getLogger(__name__)


def _apply_decision_patches(data: dict, req: ChatTurnRequest) -> list[dict] | None:
    """在业务侧应用模型返回的短补丁，避免要求模型复述整份计划。"""
    requested_days = _requested_day_count(req.message, req.days)
    try:
        # 模型给的 target_days 只做合法性探测（坏值放弃补丁）；用户原话解析的天数才是权威。
        _ = int(data.get("target_days")) if data.get("target_days") is not None else None
    except (TypeError, ValueError):
        return None
    # 用户原话解析出的目标天数优先；若模型给出的 target_days 不一致，仍以用户意图为准继续执行，
    # 而非整体判失败。超出目标天数的补丁会在下方被安全跳过（优雅降级）。
    target_days = requested_days if requested_days is not None else req.days
    if target_days < 1 or target_days > MAX_TRIP_DAYS:
        return None
    deletion_requested = target_days < req.days or bool(
        re.search(
            r"删除|删掉|去掉|移除|替换|换成|换掉|减少|精简|不重要|重复|宽松|轻松|别太赶|不要太赶|少一点",
            req.message,
        )
    )

    plans = deepcopy(req.plans)
    by_day = {int(plan.get("day_no")): plan for plan in plans if isinstance(plan.get("day_no"), int)}
    if set(by_day) != set(range(1, req.days + 1)):
        return None
    if target_days > req.days:
        for day_no in range(req.days + 1, target_days + 1):
            by_day[day_no] = {"day_no": day_no, "note": "宽松安排", "items": []}

    item_locations: dict[int, tuple[int, dict]] = {}
    moved_without_time_update: set[int] = set()
    for day_no, plan in by_day.items():
        for item in plan.get("items") or []:
            if isinstance(item.get("id"), int):
                item_locations[int(item["id"])] = (day_no, item)

    patches = data.get("patches")
    if not isinstance(patches, list):
        return None
    allowed_update_fields = {"start_time", "end_time", "duration_min", "tag", "remark"}
    for patch in patches:
        if not isinstance(patch, dict):
            return None
        operation = str(patch.get("op") or "")
        if operation in {"delete", "move", "update"}:
            item_id = patch.get("item_id")
            if isinstance(item_id, str) and item_id.isdigit():
                item_id = int(item_id)
            if not isinstance(item_id, int) or item_id not in item_locations:
                continue
            old_day_no, item = item_locations[item_id]
            # 酒店身份必须走候选卡片确认：delete/move 一律拒绝；update 只会改
            # allowed_update_fields 里的时间/备注展示字段（不含名称/价格/身份），
            # 是「把入住挪到晚上」这类时间调整的唯一合法通道（2026-09-30 评审）。
            if item.get("item_type") == "hotel" and operation != "update":
                continue
            if operation == "delete":
                if not deletion_requested:
                    continue
                remaining_non_hotel = sum(
                    candidate.get("item_type") != "hotel"
                    for plan in by_day.values()
                    for candidate in (plan.get("items") or [])
                )
                # 延长行程时最多删到“每天至少一个普通安排”，避免生成大量空白日。
                if target_days > req.days and remaining_non_hotel <= target_days:
                    continue
                by_day[old_day_no]["items"].remove(item)
                del item_locations[item_id]
            elif operation == "move":
                day_no = patch.get("day_no")
                if not isinstance(day_no, int) or day_no not in by_day or day_no > target_days:
                    continue
                by_day[old_day_no]["items"].remove(item)
                target_items = by_day[day_no]["items"]
                position = patch.get("position")
                index = max(0, min(position, len(target_items))) if isinstance(position, int) else len(target_items)
                target_items.insert(index, item)
                item_locations[item_id] = (day_no, item)
                moved_without_time_update.add(item_id)
            else:
                fields = patch.get("fields")
                if not isinstance(fields, dict):
                    return None
                clean_fields = {key: value for key, value in fields.items() if key in allowed_update_fields}
                item.update(clean_fields)
                if "start_time" in clean_fields:
                    moved_without_time_update.discard(item_id)
                # 仅修改开始时间时同步推导结束时间，避免保留旧 end_time 而出现倒序。
                if "start_time" in clean_fields and "end_time" not in clean_fields:
                    start = _clock_minutes(item.get("start_time"))
                    duration = item.get("duration_min")
                    if start is not None and isinstance(duration, (int, float)):
                        item["end_time"] = _format_clock(start + int(duration))
        elif operation == "add":
            day_no, item = patch.get("day_no"), patch.get("item")
            if not isinstance(day_no, int) or day_no not in by_day or day_no > target_days:
                continue
            if not isinstance(item, dict) or item.get("item_type") == "hotel" or not item.get("poi_name"):
                continue
            clean_item = {
                key: value
                for key, value in item.items()
                if key
                in {"item_type", "poi_name", "address", "start_time", "end_time", "duration_min", "tag", "remark"}
            }
            clean_item.setdefault("item_type", "attraction")
            by_day[day_no]["items"].append(clean_item)
        elif operation == "set_day_note":
            day_no, note = patch.get("day_no"), patch.get("note")
            if not isinstance(day_no, int) or day_no not in by_day or day_no > target_days or not isinstance(note, str):
                continue
            by_day[day_no]["note"] = note[:255]
        else:
            continue

    for item_id in moved_without_time_update:
        location = item_locations.get(item_id)
        if location and not _reschedule_moved_item(by_day, location[1], location[0]):
            # 找不到无冲突空档时保留原时间，交由后续冲突校验提示，而不是整段失败。
            logger.debug("reschedule failed for moved item %s; keeping original time", item_id)

    # 缩短行程时，被移除日期中的剩余项目直接丢弃（行程变短，多余安排不再保留）。
    for day_no in range(target_days + 1, req.days + 1):
        by_day.pop(day_no, None)
    if target_days > req.days:
        # 模型有时会创建了新日期却忘记分配项目；从最拥挤日期挪出普通项目填充空白日。
        # 酒店不参与自动移动，避免绕过住宿房型确认。
        for empty_day_no in range(1, target_days + 1):
            if by_day[empty_day_no].get("items"):
                continue
            donors = sorted(
                range(1, target_days + 1),
                key=lambda day_no: sum(
                    item.get("item_type") != "hotel" for item in (by_day[day_no].get("items") or [])
                ),
                reverse=True,
            )
            donor_day_no = next(
                (
                    day_no
                    for day_no in donors
                    if sum(item.get("item_type") != "hotel" for item in (by_day[day_no].get("items") or [])) > 1
                ),
                None,
            )
            if donor_day_no is None:
                break
            donor_items = by_day[donor_day_no]["items"]
            moving_index = next(
                index for index in range(len(donor_items) - 1, -1, -1) if donor_items[index].get("item_type") != "hotel"
            )
            by_day[empty_day_no]["items"].append(donor_items.pop(moving_index))
    result = [by_day[day_no] for day_no in range(1, target_days + 1)]
    # move/add 默认追加到末尾；按时间重新排序，保证时间线展示顺序与实际游览顺序一致。
    for plan in result:
        indexed = list(enumerate(plan.get("items") or []))
        indexed.sort(
            key=lambda row: (
                _clock_minutes(row[1].get("start_time")) is None,
                _clock_minutes(row[1].get("start_time")) or 0,
                row[0],
            )
        )
        plan["items"] = [item for _, item in indexed]
    return result


def _deterministic_reduce(req: ChatTurnRequest, target_days: int | None = None) -> list[dict]:
    """模型编辑失败时的确定性兜底：去重跨天重复景点，并按需缩短行程天数。

    仅删除/缩短，不改动价格、坐标、城市等权威字段；酒店随被移除的日期一起丢弃
    （缩短行程的副作用），由调用方在必要时走住宿确认流程。
    """
    plans = deepcopy(req.plans)
    seen: set[str] = set()
    for plan in plans:
        kept: list[dict] = []
        for item in plan.get("items") or []:
            name = item.get("poi_name")
            if item.get("item_type") in ("attraction", "food") and name:
                if name in seen:
                    continue
                seen.add(name)
            kept.append(item)
        plan["items"] = kept
    if target_days and 1 <= target_days < req.days:
        plans = [plan for plan in plans if isinstance(plan.get("day_no"), int) and plan["day_no"] <= target_days]
    for plan in plans:
        items = plan.get("items") or []
        items.sort(
            key=lambda it: (
                _clock_minutes(it.get("start_time")) is None,
                _clock_minutes(it.get("start_time")) or 0,
            )
        )
        plan["items"] = items
    return plans


def _apply_plan_update(decision: dict, req: ChatTurnRequest) -> list[dict] | None:
    """把模型的结构化提案落地成具体草稿；失败（结构不合法）返回 None。

    大改类请求（mode=rewrite_plan）会直接给出完整 plan_document，优先按整份文档落地；
    小修类请求走 patches 补丁。两者最终都经过同一套安全校验与补水（_hydrate_decision_plans）。
    """
    if str(decision.get("mode")) == "rewrite_plan" or isinstance(decision.get("plan_document"), dict):
        return _extract_document_plans(decision, req)
    if isinstance(decision.get("patches"), list):
        return _apply_decision_patches(decision, req)
    return _extract_document_plans(decision, req)


def _dedupe_plans(plans: list[dict]) -> list[dict]:
    """防御性去重：保证返回给前端的草稿里不存在跨天重复景点/餐饮。

    生成与编辑各环节的去重都在此最后兜底，避免任何遗漏把重复项漏给用户。
    酒店与权威字段一律不动。
    """
    seen: set[str] = set()
    out: list[dict] = []
    for plan in plans:
        kept: list[dict] = []
        for item in plan.get("items") or []:
            name = item.get("poi_name")
            if item.get("item_type") in ("attraction", "food") and name:
                if name in seen:
                    continue
                seen.add(name)
            kept.append(item)
        out.append({**plan, "items": kept})
    return out


def _deterministic_extend(req: ChatTurnRequest, target_days: int) -> list[dict]:
    """模型编辑失败时的加天数兜底：用单日生成器补齐新增日期，保证“加一天”也能产出完整草稿。

    新增日期含自身住宿，属于延长行程的预期副作用；仍只动行程内容、不改动既有权威字段。
    """
    plans = deepcopy(req.plans)
    used: set[str] = {it.get("poi_name") for plan in plans for it in plan.get("items") or [] if it.get("poi_name")}
    try:
        ctx = run_plan_context(req.city, req.preferences)
    except Exception as exc:
        logger.warning("deterministic extend context failed: %s", exc)
        ctx = {}
    for day_no in range(req.days + 1, target_days + 1):
        try:
            daily = run_generate_day(
                GenerateDayRequest(
                    city=req.city,
                    day_no=day_no,
                    persons=req.persons,
                    hotel_tier=req.hotel_tier,
                    preferences=req.preferences,
                    context=ctx,
                    used_names=list(used),
                    start_date=req.start_date,
                )
            )
        except Exception as exc:
            logger.warning("deterministic extend day %s failed: %s", day_no, exc)
            daily = None
        if daily and getattr(daily, "items", None):
            items = [it.model_dump() if hasattr(it, "model_dump") else dict(it) for it in daily.items]
            plans.append({"day_no": day_no, "note": daily.note, "items": items})
            for it in daily.items:
                name = getattr(it, "poi_name", None)
                if name:
                    used.add(name)
        else:
            plans.append({"day_no": day_no, "note": f"第{day_no}天（生成失败）", "items": []})
    return plans
