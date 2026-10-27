"""直接排序：保留时长、相邻间隔与区段边界，固定项不参与重排。"""

from datetime import time
from itertools import pairwise

from sqlalchemy import select

from app.common.envelope import ApiError
from app.db.models import ItineraryItem


def _minutes(value: time) -> int:
    return value.hour * 60 + value.minute


def move(session, item, day, days, request, requirements) -> None:
    target = next((d for d in days if d.day_no == request.dayNo), None)
    if target is None or not item.start_time or not item.end_time:
        raise ApiError(400, "请选择有效日期；无时段的安排请先编辑时间")
    if target.id != day.id:
        # 跨天快捷移动保留时段，由共用终检拒绝冲突，不偷偷挤动目标天。
        item.day_id = target.id
        session.flush()
        for changed_day in (day, target):
            rows = session.scalars(select(ItineraryItem).where(ItineraryItem.day_id == changed_day.id)).all()
            for index, row in enumerate(sorted(rows, key=lambda p: (p.start_time or time.max, p.sort_no or 0, p.id))):
                row.sort_no = index
        return
    rows = list(session.scalars(select(ItineraryItem).where(ItineraryItem.day_id == day.id)))
    rows.sort(key=lambda row: (row.start_time or time.max, row.sort_no or 0, row.id))
    start = rows.index(item)
    end = request.targetIndex
    if end is None or end >= len(rows) or end == start:
        raise ApiError(400, "请选择另一处有效位置")
    low, high = sorted((start, end))
    affected = rows[low : high + 1]
    fixed = {(f.day_no, f.name, f.item_type) for f in requirements.fixed_arrangements}
    if any((day.day_no, row.poi_name, row.item_type) in fixed for row in affected):
        raise ApiError(409, "这段顺序含固定安排，请先解除固定或选择其他位置")
    if any(not row.start_time or not row.end_time or row.end_time <= row.start_time for row in affected):
        raise ApiError(400, "这段安排缺少有效时段，请先编辑时间")
    durations = {row.id: _minutes(row.end_time) - _minutes(row.start_time) for row in affected}
    gaps = [max(0, _minutes(b.start_time) - _minutes(a.end_time)) for a, b in pairwise(affected)]
    cursor = _minutes(affected[0].start_time)
    rows.insert(end, rows.pop(start))
    for index, row in enumerate(rows[low : high + 1]):
        finish = cursor + durations[row.id]
        if finish >= 24 * 60:
            raise ApiError(400, "排序后跨越午夜，请改用编辑安排调整")
        row.start_time, row.end_time = time(cursor // 60, cursor % 60), time(finish // 60, finish % 60)
        cursor = finish + (gaps[index] if index < len(gaps) else 0)
    for index, row in enumerate(rows):
        row.sort_no = index
