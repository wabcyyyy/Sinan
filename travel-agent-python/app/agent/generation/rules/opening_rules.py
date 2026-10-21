"""小范围、可解释的中文开放规则提取；日期或条件不明时保留未知。"""

import re
import unicodedata
from datetime import date

from app.schemas.opening_hours import OpeningHours, OpeningInterval, OpeningRule

DAY = r"(?:周|星期)[一二三四五六日天]"
DAYS = re.compile(rf"({DAY})(?:\s*[-~至到]\s*({DAY}))?")
CLOCK = re.compile(r"(\d{1,2}):([0-5]\d)\s*[-~至到]\s*(\d{1,2}):([0-5]\d)")
CLOSED = re.compile(r"(?<!不)(?:闭馆|休馆|闭园|闭店)(?!问题)|不(?:对外)?开放|不营业|公休|休息")
_WEEK = {c: i for i, c in enumerate("一二三四五六日")}
_WEEK["天"] = 6
_DATE = r"(\d{4})年(\d{1,2})月(\d{1,2})日"


def weekdays(text: str) -> list[int] | None:
    text = re.sub(rf"({DAY})\s*([-~至到])\s*([一二三四五六日天])", r"\1\2周\3", text)
    days = set()
    for match in DAYS.finditer(text):
        first = _WEEK[match[1][-1]]
        last = _WEEK[match[2][-1]] if match[2] else first
        days.update((first + n) % 7 for n in range((last - first) % 7 + 1))
    if "周末" in text:
        days.update((5, 6))
    if "工作日" in text:
        days.update(range(5))
    return sorted(days) or None


def _validity(text: str) -> dict | None:
    """只认完整中文日期范围、自某日起、截至某日；其他日期不猜年份/归属。"""
    values = {}
    ranges = list(re.finditer(_DATE + r"\s*(?:至|到|-)\s*(?:(\d{4})年)?(\d{1,2})月(\d{1,2})日", text))
    if len(ranges) > 1:
        return None
    try:
        if ranges:
            y, m, d, y2, m2, d2 = ranges[0].groups()
            values = {"valid_from": date(int(y), int(m), int(d)), "valid_to": date(int(y2 or y), int(m2), int(d2))}
            text = text.replace(ranges[0][0], "")
        for key, pattern in (
            ("valid_from", r"自\s*" + _DATE + rf"(?:[（(]{DAY}[）)])?起"),
            ("valid_to", r"截至\s*" + _DATE),
        ):
            matches = re.findall(pattern, text)
            if len(set(matches)) > 1 or (matches and key in values):
                return None
            if matches:
                values[key] = date(*map(int, matches[0]))
            text = re.sub(pattern, "", text)
    except ValueError:
        return None
    if re.search(r"\d{2,4}年|\d{1,2}月\d{1,2}日|\d{4}[-/]\d{1,2}[-/]\d{1,2}", text):
        return None
    if values.get("valid_from") and values.get("valid_to") and values["valid_from"] > values["valid_to"]:
        return None
    return values


def _clauses(text: str) -> list[str]:
    clauses, pending = [], ""
    for part in re.split(r"[,，、]", text):
        complete = CLOSED.search(pending) or CLOCK.search(pending) or re.search(r"正常开放|全天开放", pending)
        if complete and re.match(rf"\s*(?:每)?{DAY}", part):
            clauses.append(pending)
            pending = part
        else:
            pending = f"{pending}、{part}" if pending else part
    return [*clauses, pending] if pending else clauses


def _rule(clause: str, original: str) -> OpeningRule | None:
    closed = bool(CLOSED.search(clause))
    windows = []
    for match in CLOCK.finditer(clause):
        h1, m1, h2, m2 = map(int, match.groups())
        if h1 > 23 or h2 > 24 or (h2 == 24 and m2) or (h1, m1) == (h2, m2):
            raise ValueError("开放时刻不合法或含义不明")
        windows.append(OpeningInterval(start=f"{h1:02d}:{m1:02d}", end=f"{h2:02d}:{m2:02d}"))
    if re.search(r"24小时|全天开放|全天营业", clause):
        windows.append(OpeningInterval(start="00:00", end="24:00"))
    days = weekdays(clause)
    admission = re.search(r"(\d{1,2}:[0-5]\d)\s*(?:停止|截止)(?:入馆|入园|入场|入寺)", clause)
    if closed and not days and not re.search(r"全天|暂停开放", clause):
        return None
    if not closed and not windows and not admission:
        return None
    if closed:
        windows = [OpeningInterval(start="00:00", end="12:00")] if "上午" in clause else []
        if "下午" in clause:
            windows = [OpeningInterval(start="12:00", end="24:00")]
    return OpeningRule(
        weekdays=days,
        closed=closed,
        intervals=windows,
        last_admission=admission[1].zfill(5) if admission else None,
        exceptions=[original] if re.search(r"节假日|如遇|若遇|可能|另行|例外|临时", clause) else [],
        source_text=original,
    )


def _labelled_rule(text: str) -> OpeningRule | None:
    opens = re.findall(r"开门\s*:\s*(\d{1,2}:[0-5]\d)", text)
    closes = re.findall(r"关门\s*:\s*(\d{1,2}:[0-5]\d)", text)
    admissions = re.findall(r"停止入(?:馆|园|场|寺)\s*:\s*(\d{1,2}:[0-5]\d)", text)
    if not opens and not closes:
        return None
    if len(opens) != 1 or len(closes) != 1 or len(admissions) > 1 or weekdays(text):
        raise ValueError("多组开关门标签或周条件无法确定")
    lines = [s.strip() for s in text.splitlines() if re.search(r"开门\s*:|关门\s*:|停止入(?:馆|园|场|寺)\s*:", s)]
    return OpeningRule(
        intervals=[OpeningInterval(start=opens[0].zfill(5), end=closes[0].zfill(5))],
        last_admission=admissions[0].zfill(5) if admissions else None,
        source_text="\n".join(lines),
    )


def parse_opening_rules(text: str) -> OpeningHours:
    normalized = unicodedata.normalize("NFKC", text).replace("–", "-").replace("—", "-")
    date_scope = "\n".join(
        part
        for part in re.split(r"[。；;\n]", normalized)
        if re.search(r"开放|营业|开馆|闭馆|自|截至|适用|有效|期间", part)
    )
    validity = _validity(date_scope)
    if validity is None:
        return OpeningHours(reason="来源日期范围不明确或不合法")
    if re.search(r"夏令|冬令|旺季|淡季|每年|季节|临时|曾经|往年|调整前|原开放|活动期间|即日起", normalized):
        return OpeningHours(reason="季节、历史或临时规则的适用日期未确认")
    rules = []
    try:
        labelled = _labelled_rule(normalized)
        if labelled:
            # 规则原文保留全角冒号，不用规范化文本冒充网页原句。
            labelled.source_text = "\n".join(
                s.strip() for s in text.splitlines() if re.search(r"开门|关门|停止入(?:馆|园|场|寺)", s)
            )
            rules.append(labelled)
        for sentence in re.split(r"[。；;\n]", text):
            if not re.search(r"开放|营业|开馆|闭馆|休馆|入馆|入园|闭园", sentence):
                continue
            if len(sentence) > 800:
                return OpeningHours(reason="开放段落过长，无法可靠定位规则")
            if (
                rules
                and re.search(r"节假日|例外|除外", sentence)
                and not weekdays(sentence)
                and not CLOCK.search(sentence)
            ):
                rules[-1].exceptions.append(sentence.strip())
                continue
            for clause in _clauses(unicodedata.normalize("NFKC", sentence).replace("–", "-").replace("—", "-")):
                rule = _rule(clause, sentence.strip())
                if rule:
                    rules.append(rule)
    except ValueError as exc:
        return OpeningHours(reason=str(exc))
    return (
        OpeningHours(status="known", rules=rules, **validity)
        if rules
        else OpeningHours(reason="正文没有可解析的开放规则")
    )
