"""P5 场景控制器持有目标/事件，模型只能表达当前一轮，不得到未来事件。"""

import re

FAMILIES = {
    "booking_lock": {"split": "development", "days": 1, "duration": 60, "max_places": 4, "fixed": True},
    "cross_day": {"split": "evaluation", "days": 2, "duration": 90, "max_places": 3, "fixed": True},
    "closure_negotiation": {"split": "evaluation", "days": 1, "duration": 60, "max_places": 4, "fixed": False},
    "negation_boundary": {"split": "evaluation", "days": 1, "duration": 60, "max_places": 4, "fixed": True},
}


def operations(family, variant):
    delay = (15, 20, 30)[variant]
    if family == "booking_lock":
        return [
            {"kind": "delay", "target": "地点1", "delay_minutes": delay, "expected": "apply"},
            {
                "kind": "appointment",
                "target": "地点2",
                "day_no": 1,
                "start_time": "14:00",
                "end_time": "15:00",
                "expected": "apply",
            },
            {"kind": "close", "target": "地点2", "expected": "reject"},
        ]
    if family == "cross_day":
        return [
            {
                "kind": "appointment",
                "target": "地点2",
                "day_no": 1,
                "start_time": "12:00",
                "end_time": "13:30",
                "expected": "apply",
            },
            {"kind": "move", "target": "地点5", "day_no": 1, "start_time": "15:00", "expected": "reject"},
            {"kind": "prefer", "target": "地点5", "expected": "noop_or_apply"},
        ]
    if family == "closure_negotiation":
        return [
            {"kind": "close", "target": "地点1", "expected": "reject"},
            {"kind": "finish_by", "day_no": 1, "end_time": "15:00", "expected": "apply"},
            {"kind": "close", "target": "地点1", "expected": "apply"},
        ]
    return [
        {"kind": "negative_cancel", "target": "地点1", "expected": "clarify"},
        {"kind": "delay", "target": "地点1", "delay_minutes": delay, "expected": "apply"},
        {"kind": "move", "target": "地点1", "day_no": 2, "expected": "reject"},
    ]


def intake_message(family):
    profile = FAMILIES[family]
    windows = "；".join(f"第{day}天09:00以后开始、18:00以前结束" for day in range(1, profile["days"] + 1))
    return (
        f"2026年11月3日去Paris玩{profile['days']}天，1个人，不住宿。活动总预算1000欧元是硬上限，"
        f"每天最多{profile['max_places']}个景点；{windows}。其他安排在确认表单中核对。"
    )


def semantic_errors(message, operation):
    """独立于产品 parser 的受控语义检查，不把不支持的礼貌表达误判成模拟器违规。"""
    errors = []
    names = set(re.findall(r"地点\d+", message))
    target = operation.get("target")
    if names and names != {target}:
        errors.append("模拟器更换或增加地点")
    kind = operation["kind"]
    if kind == "negative_cancel":
        if not re.search(r"不要|别|不取消|不关闭", message) or not re.search(r"取消|关闭", message):
            errors.append("模拟器未保留否定")
        return errors
    if re.search(r"不要|别|不取消|不关闭|不去|不移", message):
        errors.append("模拟器擅加否定")
    if kind == "delay":
        match = re.search(r"(?:推迟|延后|延误|顺延|往后(?:挪|推|延))\s*(\d+)\s*分钟", message)
        if not match or int(match[1]) != operation["delay_minutes"]:
            errors.append("模拟器改变延误分钟或操作")
    if kind in {"appointment", "move", "finish_by"}:
        for field in ("start_time", "end_time"):
            if operation.get(field) and operation[field].lstrip("0") not in message and operation[field] not in message:
                errors.append("模拟器遗漏或更改时刻")
        if operation.get("day_no") and not re.search(rf"第\s*{operation['day_no']}\s*天", message):
            errors.append("模拟器遗漏或更改天数")
    if kind == "appointment" and not re.search(r"预约|约在|预订", message):
        errors.append("模拟器未表达预约")
    if kind == "move" and not re.search(r"移|挪|安排到|放到|改到|去", message):
        errors.append("模拟器未表达移动")
    if kind == "finish_by" and not re.search(r"结束|返程", message):
        errors.append("模拟器未表达截止")
    if kind == "close" and not re.search(r"关闭|闭馆|取消|不开放|关掉", message):
        errors.append("模拟器未表达关闭")
    if kind == "prefer" and not re.search(r"优先|更想|偏好", message):
        errors.append("模拟器未表达偏好")
    return errors
