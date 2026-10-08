"""相邻点位转场时间的判据与生成后确定性微调（单一真源）。

转场/时间线判据从这里出：``estimate_transfer_minutes`` 与时间解析 helper
（parse_time / parse_open_window / item_start / item_end）原在
content/reflect.py，2026-10-08 下沉至此——reflect（content 层，校验）与本模块
（rules 层，生成后微调）共用同一判据，而 rules 是生成域最底层
（rules → content → output → orchestration），判据必须落在两者的公共下层，
否则微调规则就得复制第二份常量。调用方已全部就地迁移到新位置，reflect 不再转发。

跨模块 API：parse_time / parse_open_window / item_start / item_end /
estimate_transfer_minutes / normalize_item_clocks / fix_transfer_gaps。

- 判据（步行/公交最短转场时间）：相邻 attraction/food 留白 +
  ROUTE_ESTIMATE_TOLERANCE_MIN 容差 < estimate_transfer_minutes 即转场不足，
  与 validate_plans、离线评测指标、业务终检三方同一口径；
- 修复：fix_transfer_gaps 生成后就地微调——先收缩前一条目游览时长（保底
  MIN_VISIT_MINUTES），余量整段顺延后续条目（受各条目 open_time 关窗与当天
  23:59 约束）；只把留白变大，不产生新的重叠/越界/跨天错乱；修不掉的原样
  留给校验反馈循环，不在这里硬掰。
"""

import re
from typing import Any

from app.agent.core.geo import haversine_meters
from app.agent.data.route_service import (
    ROUTE_ESTIMATE_TOLERANCE_MIN,
    estimate_duration_minutes,
)

# 微调保底游览时长：收缩前一条目时不低于 30 分钟——再短就从"逛得紧凑"变成
# "路过打卡"，宁可顺延后续条目或把修不掉的留给校验反馈循环。
MIN_VISIT_MINUTES = 30
# 当天时间线写回上限取 23:59 而非 24:00："24:00" 会被 parse_time 拒收，
# 判官将退回 start+duration 推算口径，写出去的时刻反而对不上时间线。
DAY_LAST_MINUTE = 24 * 60 - 1

_TIME_RE = re.compile(r"^(\d{1,2}):(\d{2})$")


def parse_time(value: str | None) -> int | None:
    if not value:
        return None
    m = _TIME_RE.match(value.strip())
    if not m:
        return None
    hour, minute = int(m.group(1)), int(m.group(2))
    # "99:99" 之类非法时间必须拒绝，否则会被解析成 6039 分钟蒙混过冲突检测。
    if hour > 23 or minute > 59:
        return None
    return hour * 60 + minute


def parse_open_window(open_time: str | None) -> tuple[int, int] | None:
    if not open_time:
        return None
    ranges = re.findall(r"(\d{1,2}):(\d{2})\s*[-~至]\s*(\d{1,2}):(\d{2})", open_time)
    if not ranges:
        return None
    start = int(ranges[0][0]) * 60 + int(ranges[0][1])
    end = int(ranges[0][2]) * 60 + int(ranges[0][3])
    # 跨午夜闭馆（如 18:00-02:00）：结束时间归一化到次日，避免误报"不符"。
    if end <= start:
        end += 24 * 60
    return start, end


def item_start(item: dict) -> int:
    return parse_time(item.get("start_time")) or 0


def item_end(item: dict) -> int:
    end = parse_time(item.get("end_time"))
    if end is not None:
        return end
    start = item_start(item)
    duration = item.get("duration_min")
    return start + int(duration or 120)


def estimate_transfer_minutes(first: dict, second: dict, mode: str = "walking") -> int | None:
    """按 POI 坐标估算保守换乘时间；缺坐标/0/0 哨兵返回 None（unknown，不当 0）。

    M3（spec §8.1）：速度/缓冲统一走 route_service.estimate_duration_minutes
    的 mode-aware 单一实现——本模块不再自持第二套估算；步行档 4.5km/h 规划
    默认值（此前误用 25km/h 车速，估算结果一律标 estimated）。
    """
    values: list[float] = []
    for raw in (first.get("latitude"), first.get("longitude"), second.get("latitude"), second.get("longitude")):
        if raw is None:
            return None
        try:
            value = float(raw)
        except (TypeError, ValueError):
            return None
        # 0/0 是 RAG 缺失哨兵，不能当真实坐标算路程
        if abs(value) <= 1e-6:
            return None
        values.append(value)
    distance_m = haversine_meters(*values)
    return estimate_duration_minutes(distance_m, mode)


def normalize_item_clocks(item: dict) -> None:
    """单条目时间窗归一：24 点制钟点回绕 + duration 跟随时间窗（就地改写）。

    24:00 写法回绕为 00:00（落库/前端钟点口径）；知识库 duration_min 是
    “典型游览时长”，可能与已排时间窗不一致（如西湖库内 480 分钟、行程只排
    150 分钟）——以时间窗为准。原为 day_stream.generate_day_once 的内联块，
    2026-10-08 上收至此：与转场判据同属时间线规则，行为原样搬移。
    """
    for key in ("start_time", "end_time"):
        value = item.get(key)
        if isinstance(value, str):
            item[key] = value.replace("24:", "00:")
    st = parse_time(item.get("start_time"))
    en = parse_time(item.get("end_time"))
    if st is not None and en is not None and en > st:
        item["duration_min"] = en - st


def fix_transfer_gaps(items: list[Any] | None) -> int:
    """生成后自查相邻 attraction/food 的转场留白，不足就就地微调时间线。

    判据与 validate_plans / 业务终检 / 离线评测完全同口径：留白 +
    ROUTE_ESTIMATE_TOLERANCE_MIN 容差 < estimate_transfer_minutes 才算不足
    （判官放行的一律不动，避免无谓改动产物）。微调顺序：先收缩前一条目的
    end_time（游览保底 MIN_VISIT_MINUTES，其余条目时间不动），余量把后续
    条目**整段**顺延——段内间隔不变，才不会把足额的相邻对挤成新违规。
    重叠对（前条 end > 后条 start）是时间冲突规则的辖区，本规则不越权；
    时刻不可解析 / 缺坐标 / 0-0 哨兵的条目一律不猜不动。返回被修复（微调后
    达到判官口径）的间隔数，供调用方记遥测。
    """
    if not items:
        return 0
    timed = [
        item
        for item in items
        if isinstance(item, dict) and item.get("item_type") in ("attraction", "food") and _has_explicit_window(item)
    ]
    if len(timed) < 2:
        return 0
    timed.sort(key=item_start)
    fixed = 0
    for index in range(len(timed) - 1):
        prev, nxt = timed[index], timed[index + 1]
        prev_start, prev_end = _window_of(prev)
        next_start, _next_end = _window_of(nxt)
        if prev_start is None or prev_end is None or next_start is None:
            continue  # 序内条目入场时都可解析；防御脏改写，不猜
        if prev_end > next_start:
            continue  # 重叠不归本规则管，与 validate_plans 的 continue 对齐
        required = estimate_transfer_minutes(prev, nxt)
        if required is None:
            continue  # 缺坐标 / 0-0 哨兵：判官同样跳过，不猜路线
        deficit = required - ROUTE_ESTIMATE_TOLERANCE_MIN - (next_start - prev_end)
        if deficit <= 0:
            continue
        # 1) 收缩前一条目：只动 end_time，后续条目时间全部原地不动
        trim = min(deficit, max(prev_end - prev_start - MIN_VISIT_MINUTES, 0))
        if trim > 0:
            prev["end_time"] = _fmt_clock(prev_end - trim)
            # duration 跟随时间窗（与 day_stream 落地链"以时间窗为准"同一口径）
            prev["duration_min"] = prev_end - trim - prev_start
            prev_end -= trim
        # 2) 余量把后续条目整段顺延：受各条目 open_time 关窗与当天 23:59 约束
        shift = min(deficit - trim, _suffix_slack(timed[index + 1 :]))
        if shift > 0:
            for item in timed[index + 1 :]:
                start, end = _window_of(item)
                if start is None or end is None:
                    continue  # 同上：理论不可达，防御
                item["start_time"] = _fmt_clock(start + shift)
                item["end_time"] = _fmt_clock(end + shift)  # 平移不改时长，duration_min 不动
        if next_start + shift - prev_end + ROUTE_ESTIMATE_TOLERANCE_MIN >= required:
            fixed += 1
    return fixed


def _has_explicit_window(item: dict) -> bool:
    """只按显式可解析的时间窗微调；缺 end 的条目判官按 start+duration 推算，
    那条口径不是本规则能动的东西。"""
    start, end = _window_of(item)
    return start is not None and end is not None and end > start


def _window_of(item: dict) -> tuple[int | None, int | None]:
    return parse_time(item.get("start_time")), parse_time(item.get("end_time"))


def _suffix_slack(items: list[dict]) -> int:
    """整段顺延的上限：后续条目都不得越过自己的 open_time 关窗与当天 23:59。"""
    ends = [end for item in items if (end := _window_of(item)[1]) is not None]
    if not ends:
        return 0
    slack = DAY_LAST_MINUTE - max(ends)
    for item in items:
        end = _window_of(item)[1]
        window = parse_open_window(item.get("open_time"))
        if window is not None and end is not None:
            slack = min(slack, window[1] - end)
    return max(slack, 0)


def _fmt_clock(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"
