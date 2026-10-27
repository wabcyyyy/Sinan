"""P2 免费受控句式：只解释现有条目的一项变化；歧义/否定不猜、不调模型。"""

import re

from app.schemas.replanning import ReplanChange, ReplanParseVO
from app.schemas.trip_requirements import MAX_TRIP_DAYS

_DAY = rf"([1-{MAX_TRIP_DAYS}])"
_TIME = r"([0-2]?\d:[0-5]\d)"


def parse_replan_change(message, items, *, item_id=None, day_no=1):
    text = message.strip().strip("。！!")
    if re.search(r"不要|别|不取消|不关闭|取消关闭|不移|不要取消", text):
        return ReplanParseVO(questions=["这句话含否定，请用表单确认具体修改；未自动执行。"])
    candidates = (
        [p for p in items if p["id"] == item_id]
        if item_id
        else [
            p
            for p in items
            if p["poi_name"] in text or any(alias in text for alias in p.get("aliases", []) if len(alias) > 1)
        ]
    )
    selected = candidates[0] if len(candidates) == 1 else None
    match = re.fullmatch(rf"(?:第{_DAY}天)?(?:提前到|改为|改到)?{_TIME}(?:前)?(?:结束|返程)", text)
    if match:
        return _parsed(kind="finish_by", day_no=int(match[1]) if match[1] else day_no, end_time=_clock(match[2]))
    if selected is None:
        return ReplanParseVO(questions=["请点选一个现有条目或写出唯一地点名；同名条目需要条目 ID。"])
    for other in items:
        if other["id"] != selected["id"] and other["poi_name"] != selected["poi_name"] and other["poi_name"] in text:
            return ReplanParseVO(questions=["一句话只处理一个条目的变化，请分次比较。"])
    text = re.sub(r"^(?:请(?:帮我)?|帮我|我想|麻烦(?:你)?)(?:\s*)", "", text, count=1)
    return _parse_existing(text, selected, day_no)


def _parse_existing(text, selected, day_no):
    name = re.escape(selected["poi_name"])
    if re.fullmatch(rf"(?:关闭|闭馆|取消|关掉)(?:[「『\"]?{name}[」』\"]?)(?:的活动)?", text):
        return _parsed(kind="close", item_id=selected["id"])
    appointment = _appointment_expression(text, name)
    if appointment:
        day, start, end = appointment
        return _parsed(
            kind="appointment",
            item_id=selected["id"],
            day_no=day or day_no,
            start_time=_clock(start),
            end_time=_clock(end),
        )
    prefix = rf"(?:把|将)?(?:[「『\"]?{name}[」』\"]?)?(?:的(?:活动|预约))?\s*"
    rest = re.sub("^" + prefix, "", text, count=1)
    match = re.fullmatch(rf"(?:移到|挪到|安排到)第{_DAY}天(?:的)?(?:" + _TIME + r")?", rest)
    if match:
        return _parsed(
            kind="move", item_id=selected["id"], day_no=int(match[1]), start_time=_clock(match[2]) if match[2] else None
        )
    match = re.fullmatch(r"(?:推迟|延后|延误)([1-9]\d{0,2})分钟", rest)
    if match and int(match[1]) <= 480:
        return _parsed(kind="delay", item_id=selected["id"], delay_minutes=int(match[1]))
    match = re.fullmatch(r"(?:改到|改成|改为|安排在)" + _TIME, rest)
    if match:
        return _parsed(kind="move", item_id=selected["id"], day_no=selected["day_no"], start_time=_clock(match[1]))
    if re.fullmatch(r"(?:已)?(?:关闭|闭馆|不开放|取消|关掉)", rest):
        return _parsed(kind="close", item_id=selected["id"])
    match = re.fullmatch(rf"(?:预约|约在)(?:第{_DAY}天)?" + _TIME + r"(?:到|至|[-–])" + _TIME, rest)
    if match:
        return _parsed(
            kind="appointment",
            item_id=selected["id"],
            day_no=int(match[1]) if match[1] else day_no,
            start_time=_clock(match[2]),
            end_time=_clock(match[3]),
        )
    if re.fullmatch(r"(?:优先保留|更想去)" + re.escape(selected["poi_name"]), text):
        return _parsed(kind="prefer", item_id=selected["id"])
    return ReplanParseVO(
        questions=[
            "当前支持：推迟30分钟、移到第2天、改到15:00、关闭、预约14:00-15:00、第1天16:00结束。"
            "可改用表单，不自动猜测。"
        ]
    )


def _appointment_expression(text, name):
    """只收完整同日预约表达；重复截止必须与预约结束一致，不能吞掉第二个要求。"""
    day = rf"第{_DAY}天"
    clock = _TIME
    patterns = (
        rf"(?:把|将)?{name}的预约改到{day}{clock}[，,]第{_DAY}天{clock}前结束",
        rf"预约{day}{clock}(?:到|至|[-–]){clock}的{name}(?:[，,]第{_DAY}天{clock}前结束)?",
        rf"预约{name}[，,]{day}{clock}(?:到|至|[-–]){clock}",
    )
    for index, pattern in enumerate(patterns):
        match = re.fullmatch(pattern, text)
        if match is None:
            continue
        values = match.groups()
        if index == 0:
            d1, start, d2, end = values
            return (int(d1), start, end) if d1 == d2 else None
        d1, start, end = values[:3]
        if index == 1 and values[3] is not None and (values[3] != d1 or _clock(values[4]) != _clock(end)):
            return None
        return int(d1), start, end
    return None


def _clock(value):
    hour, minute = value.split(":")
    return f"{int(hour):02d}:{minute}"


def _parsed(**kwargs):
    try:
        return ReplanParseVO(change=ReplanChange(**kwargs))
    except ValueError:
        return ReplanParseVO(questions=["日期或时段无效，请用 00:00–23:59 的完整时段确认。"])
