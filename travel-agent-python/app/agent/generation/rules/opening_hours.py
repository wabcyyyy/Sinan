"""开放规则的排程一致性检查；网页原文与模型声明不等于实时营业保证。"""

import re
from datetime import date, timedelta

from pydantic import ValidationError

from app.agent.generation.rules.opening_rules import CLOSED, parse_opening_rules
from app.agent.generation.rules.transfer_time import parse_time
from app.agent.grounding.grounding_evidence import evidence_row
from app.schemas.opening_hours import OpeningHours
from app.schemas.trip import FactEvidence


def _date(plan: dict, start_date: str | date | None) -> date | None:
    try:
        if plan.get("travel_date"):
            return date.fromisoformat(str(plan["travel_date"])[:10])
        if start_date:
            return date.fromisoformat(str(start_date)[:10]) + timedelta(days=int(plan["day_no"]) - 1)
    except (ValueError, TypeError, KeyError):
        pass
    return None


def _unknown(reason: str) -> dict:
    return {"status": "unknown", "reason": reason}


def _check_hours(hours: OpeningHours, item: dict, travel_date: date | None) -> dict:
    if hours.status != "known":
        return _unknown(hours.reason or "开放资料未知")
    if (hours.valid_from or hours.valid_to) and (
        travel_date is None
        or (hours.valid_from and travel_date < hours.valid_from)
        or (hours.valid_to and travel_date > hours.valid_to)
    ):
        return _unknown("来源规则不在已确认的适用日期范围内")
    start, end = parse_time(item.get("start_time")), parse_time(item.get("end_time"))
    if start is None or end is None or start == end:
        return _unknown("缺少或无法确定到访时段")
    overnight = end < start
    if overnight:
        end += 1440
    # 有星期限定的跨夜时段需要前后两天规则；当前语法不推断归属。
    if any(r.weekdays is not None and (overnight or any(x.end < x.start for x in r.intervals)) for r in hours.rules):
        return _unknown("跨夜规则的星期归属待核实")
    applicable = []
    for rule in hours.rules:
        if rule.weekdays is not None:
            if travel_date is None:
                return _unknown("缺少出行日期，无法核对开放日")
            if travel_date.weekday() not in rule.weekdays:
                continue
        if rule.exceptions:
            return _unknown("日期或例外条件未核实")
        applicable.append(rule)
    windows = []
    for rule in applicable:
        intervals = [
            (parse_time(x.start) or 0, 1440 if x.end == "24:00" else parse_time(x.end) or 0) for x in rule.intervals
        ]
        intervals = [(a, b + 1440 if b < a else b) for a, b in intervals]
        if rule.closed:
            if not intervals or any(start < b and end > a for a, b in intervals):
                return {"status": "violation", "reason": f"闭馆日冲突：{rule.source_text}"}
        else:
            windows.extend(intervals)
        if rule.last_admission and start >= (parse_time(rule.last_admission) or 0):
            return {"status": "violation", "reason": f"开放时间不符：{rule.last_admission} 停止入场"}
    if not windows:
        return _unknown("当日没有已确认的开放窗口")
    if not any(a <= start + shift and end + shift <= b for a, b in windows for shift in (0, 1440)):
        return {"status": "violation", "reason": "开放时间不符：到访未完全落在所述时段内"}
    return {"status": "pass", "reason": "到访与所述开放规则一致，出发前仍须核实"}


def _sourced_check(item: dict, travel_date: date | None) -> dict | None:
    raw = (item.get("fact_evidence") or {}).get("open_time")
    if not raw:
        return None
    try:
        fact = raw if isinstance(raw, FactEvidence) else FactEvidence.model_validate(raw)
    except ValidationError:
        return _unknown("开放字段来源结构不合法")
    if fact.opening_hours is None:
        return None
    snapshot = evidence_row({"name": item.get("poi_name"), "source": fact.provider, "evidence_id": fact.source_ref})
    if (
        not snapshot
        or fact.value_kind != "observed"
        or any(
            (
                fact.source_url != snapshot.get("source_url"),
                fact.retrieved_at != snapshot.get("source_fetched_at"),
                fact.excerpt != snapshot.get("open_time"),
                fact.opening_hours.model_dump(mode="json") != snapshot.get("opening_hours"),
            )
        )
    ):
        return _unknown(fact.opening_hours.reason or "开放来源票据缺失、过期或与原文不一致")
    return _check_hours(fact.opening_hours, item, travel_date)


def opening_checks(plan: dict, start_date: str | date | None = None) -> list[dict]:
    """pass 仅为排程与所述规则一致，未知不冒充已开放。"""
    checks = []
    for item in plan.get("items") or []:
        if item.get("item_type") not in ("attraction", "food", "activity", "shopping"):
            continue
        check = _sourced_check(item, _date(plan, start_date))
        if check is None:
            hours, remark = str(item.get("open_time") or ""), str(item.get("remark") or "")
            text = "。".join("开放时间：" + part for part in re.split(r"[;；。\n]", hours) if part)
            if CLOSED.search(remark) or "开放时间" in remark or "营业时间" in remark:
                text += "。" + remark
            check = _check_hours(parse_opening_rules(text), item, _date(plan, start_date))
        checks.append({"poi_name": str(item.get("poi_name") or ""), **check})
    return checks
