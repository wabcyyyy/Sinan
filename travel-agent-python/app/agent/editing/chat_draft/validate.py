"""行程草稿的安全校验与基础工具（纯函数，无业务、无 LLM）。

职责：
- 时间冲突检测（_plan_conflict）、实质变更签名（_substantive_plan_signature）；
- 模型 JSON 的解析与修复（_parse_decision_json / DecisionJsonError）；
- 时钟字符串与分钟数互转（_clock_minutes / _format_clock）；
- 回复文案兜底（_decision_reply / _default_plan_update_reply）。

实现要点：
- 全部为无副作用的确定性函数，只做“检查/转换”，绝不修改行程；
- 上层（decide / plan_edit）在落地前后调用这里做校验，是安全边界的一部分。

依赖：app.agent.core.json_utils（解析唯一实现）；re、itertools。
"""

import logging
import re
from itertools import pairwise

from app.agent.core.json_utils import LlmJsonError, parse_llm_json
from app.schemas.trip import (
    ChatTurnRequest,
)

logger = logging.getLogger(__name__)


class DecisionJsonError(ValueError):
    """模型决策不是可安全执行的 JSON；避免把解析器英文异常暴露给用户。"""


def _parse_decision_json(raw: str) -> dict:
    """解析失败（含缺大括号/非 dict）一律映射 DecisionJsonError：decide 层
    统一走「修复重试 → 文本降级」，不再区分失败形态。"""
    try:
        return parse_llm_json(raw)
    except LlmJsonError as exc:
        raise DecisionJsonError("模型返回的结构化结果不完整") from exc


def _clock_minutes(value: object) -> int | None:
    match = re.fullmatch(r"\s*(\d{1,2}):(\d{2})(?::\d{2})?\s*", str(value or ""))
    if not match:
        return None
    hour, minute = int(match.group(1)), int(match.group(2))
    return hour * 60 + minute if 0 <= hour <= 23 and 0 <= minute <= 59 else None


def _format_clock(minutes: int) -> str | None:
    if not 0 <= minutes < 24 * 60:
        return None
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def _all_plan_conflicts(plans: list[dict]) -> list[tuple[int, str, str]]:
    """全部日内时间冲突（day_no, 先起项, 后起项）；酒店入住点不视为占用型活动。"""
    conflicts: list[tuple[int, str, str]] = []
    for plan in plans:
        day_no = int(plan.get("day_no") or 0)
        intervals = []
        for item in plan.get("items") or []:
            if item.get("item_type") == "transport":
                continue
            start = _clock_minutes(item.get("start_time"))
            end = _clock_minutes(item.get("end_time"))
            if end is None and start is not None and isinstance(item.get("duration_min"), (int, float)):
                end = start + int(item["duration_min"])
            if start is None or end is None or end <= start:
                continue
            intervals.append((start, end, str(item.get("poi_name") or "未命名安排")))
        intervals.sort()
        for previous, current in pairwise(intervals):
            if current[0] < previous[1]:
                conflicts.append((day_no, previous[2], current[2]))
    return conflicts


def _plan_conflict(plans: list[dict]) -> tuple[int, str, str] | None:
    """返回首个日内时间冲突（decide 拒绝分支用）；无冲突返回 None。"""
    conflicts = _all_plan_conflicts(plans)
    return conflicts[0] if conflicts else None


def _untouched_conflicted_days(plans: list[dict], baseline: list[dict]) -> set[int]:
    """「冲突全为遗留」的天集合：该天基线本就有冲突，且编辑后所有存留条目的
    (id, start, end) 与基线原样一致（纯删除——没有改任何时间、没有新增条目）。

    活体实证（2026-10-06 残留①复验 R4）：基线有全占酒店条目时，pairwise 相邻配对
    使 (酒店,甲) 在基线冲突集而 (酒店,乙) 不在；模型删掉甲后 (酒店,乙) 成为新相邻对，
    按三元组精确匹配会被误判 novel 而拒绝。按天判定兜住这类纯删除场景；条目缺 id
    （新加）或 id/时间对不上（改过时间）都不算 untouched，保守回落到对级判定。"""
    base_days = {day for day, _, _ in _all_plan_conflicts(baseline)}
    if not base_days:
        return set()
    base_items: dict[tuple[int, str], tuple[str, str]] = {}
    for plan in baseline:
        day_no = int(plan.get("day_no") or 0)
        for item in plan.get("items") or []:
            if item.get("id") is not None:
                base_items[(day_no, str(item["id"]))] = (
                    str(item.get("start_time") or ""),
                    str(item.get("end_time") or ""),
                )
    out: set[int] = set()
    for plan in plans:
        day_no = int(plan.get("day_no") or 0)
        if day_no not in base_days:
            continue
        untouched = all(
            item.get("id") is not None
            and base_items.get((day_no, str(item["id"])))
            == (
                str(item.get("start_time") or ""),
                str(item.get("end_time") or ""),
            )
            for item in plan.get("items") or []
        )
        if untouched:
            out.add(day_no)
    return out


def _plan_item_signature(item: dict) -> tuple[str, ...]:
    """逐字段条目签名（M4 §9.1）：身份/日期/时间/费用/类型/坐标——不只比名称。"""
    return (
        str(item.get("item_type") or ""),
        str(item.get("poi_name") or ""),
        str(item.get("start_time") or ""),
        str(item.get("end_time") or ""),
        str(item.get("duration_min") or ""),
        str(item.get("cost") or ""),
        str(item.get("latitude") or ""),
        str(item.get("longitude") or ""),
    )


def _scope_violations(decision: dict, baseline_plans: list[dict], draft_plans: list[dict]) -> list[str]:
    """作用域硬校验（M4，spec §9.1）。

    - affected_days 给定时：清单之外的天逐字段必须与基线一致（含时间/费用/坐标），
      "只改第1天"则第2/3天任何字段变化都是违例；
    - preserved 清单：要求保留的条目必须在草稿中且逐字段未变。
    违例是硬约束——调用方不得用宽泛 fallback 重写后放行。
    """
    issues: list[str] = []

    def by_day(plans: list[dict]) -> dict[int, list[tuple[str, ...]]]:
        result: dict[int, list[tuple[str, ...]]] = {}
        for plan in plans:
            day_no = int(plan.get("day_no") or 0)
            result[day_no] = sorted(
                _plan_item_signature(it) for it in (plan.get("items") or []) if isinstance(it, dict)
            )
        return result

    affected_raw = decision.get("affected_days")
    if isinstance(affected_raw, list):
        try:
            affected = {int(x) for x in affected_raw}
        except (TypeError, ValueError):
            affected = set()
        base_by_day, draft_by_day = by_day(baseline_plans), by_day(draft_plans)
        for day_no, base_sigs in base_by_day.items():
            if day_no in affected:
                continue
            if draft_by_day.get(day_no, []) != base_sigs:
                issues.append(
                    f"第 {day_no} 天不在本次授权修改范围内，但其内容发生了变化；请只修改第 {sorted(affected)} 天"
                )
    preserved = decision.get("preserved")
    if isinstance(preserved, list):
        draft_index: dict[tuple[int, str], tuple[str, ...]] = {}
        for plan in draft_plans:
            day_no = int(plan.get("day_no") or 0)
            for it in plan.get("items") or []:
                if isinstance(it, dict):
                    draft_index[(day_no, str(it.get("poi_name") or ""))] = _plan_item_signature(it)
        base_index: dict[tuple[int, str], tuple[str, ...]] = {}
        for plan in baseline_plans:
            day_no = int(plan.get("day_no") or 0)
            for it in plan.get("items") or []:
                if isinstance(it, dict):
                    base_index[(day_no, str(it.get("poi_name") or ""))] = _plan_item_signature(it)
        for entry in preserved:
            if not isinstance(entry, dict):
                continue
            try:
                day_no = int(entry.get("day_no") or 0)
            except (TypeError, ValueError):
                continue
            name = str(entry.get("poi_name") or "")
            key = (day_no, name)
            if key not in draft_index:
                issues.append(f"要求保留的「{name}」（第 {day_no} 天）在草稿中丢失")
            elif key in base_index and base_index[key] != draft_index[key]:
                issues.append(f"要求保留的「{name}」（第 {day_no} 天）的时间/费用等字段发生了变化")
    return issues


def _substantive_plan_signature(plans: list[dict]) -> list[tuple]:
    return [
        (
            int(plan.get("day_no") or 0),
            item_index,
            str(item.get("id") or ""),
            str(item.get("item_type") or ""),
            str(item.get("poi_name") or ""),
            str(item.get("start_time") or ""),
            str(item.get("end_time") or ""),
            str(item.get("duration_min") or ""),
        )
        for plan in plans
        for item_index, item in enumerate(plan.get("items") or [])
    ]


def _decision_reply(value: object, fallback: str) -> str:
    reply = str(value or "").strip()
    compact = re.sub(r"[\s`*_#：:]", "", reply).lower()
    if not reply or compact in {"中文markdown", "markdown", "中文", "reply", "回复"}:
        return fallback
    return reply


def _reschedule_moved_item(plans_by_day: dict[int, dict], item: dict, day_no: int) -> bool:
    """移动到新日期后若原时间冲突，寻找 07:00-22:00 的首个合理空档。"""
    duration = item.get("duration_min")
    if not isinstance(duration, (int, float)) or duration <= 0:
        return False
    duration = int(duration)
    occupied = []
    for other in plans_by_day[day_no].get("items") or []:
        if other is item or other.get("item_type") == "transport":
            continue
        start = _clock_minutes(other.get("start_time"))
        end = _clock_minutes(other.get("end_time"))
        if end is None and start is not None and isinstance(other.get("duration_min"), (int, float)):
            end = start + int(other["duration_min"])
        if start is not None and end is not None and end > start:
            occupied.append((start, end))
    occupied.sort()

    def available(start: int) -> bool:
        end = start + duration
        return end <= 22 * 60 and all(end <= busy_start or start >= busy_end for busy_start, busy_end in occupied)

    original = _clock_minutes(item.get("start_time"))
    if original is not None and available(original):
        return True
    for candidate in range(7 * 60, 22 * 60 - duration + 1, 15):
        if available(candidate):
            item["start_time"] = _format_clock(candidate)
            item["end_time"] = _format_clock(candidate + duration)
            return True
    return False


def _default_plan_update_reply(req: ChatTurnRequest, plans: list[dict]) -> str:
    before_count = sum(len(plan.get("items") or []) for plan in req.plans)
    after_count = sum(len(plan.get("items") or []) for plan in plans)
    lines = [f"### 已生成 {len(plans)} 天宽松版草稿", ""]
    if after_count < before_count:
        lines.append(f"已精简 **{before_count - after_count}** 个相对次要或重复的安排，并重新分配剩余项目。")
    else:
        lines.append("已重新分配每天的安排，降低单日行程密度。")
    lines.extend(["", "请先查看每天安排，确认后再应用；住宿没有被自动修改。"])
    return "\n".join(lines)
