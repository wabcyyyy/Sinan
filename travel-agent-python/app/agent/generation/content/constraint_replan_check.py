"""直接检查最终安排与量化扰动，不读取 CP 变量或编译约束。"""

from datetime import UTC, datetime, timedelta
from itertools import combinations, pairwise

from pydantic import ValidationError

from app.schemas.replanning import ReplanChange, ReplanMetrics
from app.schemas.trip import FactEvidence


def minute(value):
    return int(value[:2]) * 60 + int(value[3:5])


def item_positions(plans):
    return {p["id"]: (d["day_no"], i, p) for d in plans for i, p in enumerate(d["items"])}


def schedule_metrics(before, after, matrix, protected):
    old, new = item_positions(before), item_positions(after)
    retained = old.keys() & new.keys()
    shifts = [abs(minute(new[k][2]["start_time"]) - minute(old[k][2]["start_time"])) for k in retained]
    changed = [
        k
        for k in old
        if k not in new
        or old[k][0] != new[k][0]
        or any(old[k][2][f][:5] != new[k][2][f][:5] for f in ("start_time", "end_time"))
    ]
    inversions = sum(
        old[a][0] == old[b][0] and new[a][0] == new[b][0] and (old[a][1] - old[b][1]) * (new[a][1] - new[b][1]) < 0
        for a, b in combinations(retained, 2)
    )
    travel = sum(
        matrix.get((f"item:{a['id']}", f"item:{b['id']}"), {}).get("duration_min", 0)
        for day in after
        for a, b in pairwise(day["items"])
    )
    metrics = ReplanMetrics(
        removed_items=len(old.keys() - new.keys()),
        cross_day_moves=sum(old[k][0] != new[k][0] for k in retained),
        changed_items=len(changed),
        order_changes=inversions,
        start_shift_minutes=sum(shifts),
        travel_minutes=travel,
        mean_start_shift_minutes=round(sum(shifts) / len(shifts), 3) if shifts else 0,
        locked_violations=sum(k in changed for k in protected),
    )
    return metrics, sorted(changed)


def _cost_update_matches(original, current, update):
    """P4 仅允许服务端草稿逐项声明的观测更新；原有无证据改价保护继续生效。"""
    if not update or not original.get("source_entity_id") or update.get("entityId") != original["source_entity_id"]:
        return False
    if any(
        update.get(old) != original.get(field) or update.get(new) != current.get(field)
        for field, old, new in (("cost", "oldAmount", "amount"), ("cost_currency", "oldCurrency", "currency"))
    ):
        return False
    try:
        fact = FactEvidence.model_validate(update["evidence"])
        actual = FactEvidence.model_validate(current.get("fact_evidence", {}).get("cost", {}))
        observed = datetime.fromisoformat((fact.retrieved_at or "").replace("Z", "+00:00"))
        now = datetime.now(UTC)
        if observed.tzinfo is None or observed > now + timedelta(minutes=5):
            return False
        if fact.expires_at:
            expiry = datetime.fromisoformat(fact.expires_at.replace("Z", "+00:00"))
            if expiry.tzinfo is None or expiry <= now:
                return False
        return (
            fact == actual
            and bool(fact.source_url)
            and fact.value_kind == "observed"
            and fact.verification_status in {"partially_verified", "verified"}
            and fact.freshness_status != "stale"
        )
    except (KeyError, TypeError, ValueError, ValidationError):
        return False


def validate_replan_schedule(before, after, change, completed, matrix, removable=(), *, fact_updates=()):
    event = change if isinstance(change, ReplanChange) else ReplanChange.model_validate(change)
    old, new = item_positions(before), item_positions(after)
    rows = [p for d in after for p in d["items"]]
    errors = []
    updates = {row["itemId"]: row for row in fact_updates}
    allowed_removals = set(removable) | ({event.item_id} if event.kind == "close" else set())
    if not old.keys() - new.keys() <= allowed_removals:
        errors.append("移除了未经用户允许舍弃的安排")
    if len(new) != len(rows) or not new.keys() <= old.keys():
        errors.append("条目 ID 重复或不是原行程的条目")
    for key in old.keys() & new.keys():
        original, current = old[key][2], new[key][2]
        if any(current.get(f) != original.get(f) for f in ("poi_name", "item_type")):
            errors.append(f"条目 {key} 的身份被改写")
        if any(current.get(f) != original.get(f) for f in ("cost", "cost_currency")) and not _cost_update_matches(
            original, current, updates.get(key)
        ):
            errors.append(f"条目 {key} 的费用缺少匹配的有效证据更新")
        expected = (
            minute(event.end_time) - minute(event.start_time)
            if event.kind in {"move", "appointment"} and event.start_time and event.end_time and key == event.item_id
            else minute(original["end_time"]) - minute(original["start_time"])
        )
        if minute(current["end_time"]) - minute(current["start_time"]) != expected:
            errors.append(f"条目 {key} 的游玩时长被擅改")
    if any(
        key not in new
        or old[key][0] != new[key][0]
        or any(old[key][2][f][:5] != new[key][2][f][:5] for f in ("start_time", "end_time"))
        for key in completed
    ):
        errors.append("已执行或锁定条目发生变化")
    _check_operation(old, new, event, errors)
    for day in after:
        for a, b in pairwise(day["items"]):
            duration = matrix.get((f"item:{a['id']}", f"item:{b['id']}"), {}).get("duration_min", 0)
            if minute(b["start_time"]) < minute(a["end_time"]) + duration:
                errors.append(f"第 {day['day_no']} 天转场时间不足")
    return errors


def _check_operation(old, new, event, errors):
    current = new.get(event.item_id)
    if event.kind == "close":
        if current:
            errors.append("关闭的活动仍在行程内")
        return
    if event.kind == "finish_by":
        if any(d == event.day_no and minute(p["end_time"]) > minute(event.end_time) for d, _, p in new.values()):
            errors.append("未满足新的截止时间")
        return
    if event.kind == "prefer":
        return  # 软偏好不是可以覆盖旧硬约束的命令。
    if current is None:
        errors.append("用户要求移动/延后的条目被删除")
        return
    day, index, item = current
    if event.day_no is not None and day != event.day_no:
        errors.append("移动/预约的日期不符合操作")
    if event.start_time and item["start_time"][:5] != event.start_time:
        errors.append("指定开始时间未保留")
    if (
        event.kind == "delay"
        and minute(item["start_time"]) < minute(old[event.item_id][2]["start_time"]) + event.delay_minutes
    ):
        errors.append("延误要求未保留")
    if event.anchor_item_id:
        anchor = new.get(event.anchor_item_id)
        if anchor is None or anchor[0] != day or ((index < anchor[1]) != (event.placement == "before")):
            errors.append("指定的相对顺序未保留")
