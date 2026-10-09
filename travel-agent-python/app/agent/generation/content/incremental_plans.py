"""整趟 JSON 增量解析（M5a，spec §10.1）：真实逐项候选预览的解析底座。

职责：
- 用 ijson push 接口（parse_coro + sendable_list）把 stream_chat_deltas 的文本
  增量推入解析器——跨 chunk 的字符串/转义/嵌套/对象闭合由 ijson 负责，
  本模块不手写数括号、不正则截 JSON，未闭合对象绝不会被当成完整候选；
- 逐 item 发布候选：item JSON 完整闭合 + 基础形状校验（poi_name 非空、时间格式、
  体积上限）后才产出 CandidateItem；day_no 后置时按产出顺序缓冲到确定归属；
- EOF：顶层未闭合 = TruncatedTripPlanError（半截输出，上层走逐日 fallback）；
  重复日/缺字段交给上层既有 validate_trip_output 以 days/seen_day_nos 判定。

候选身份（runId + dayNo + itemOrdinal）由上层分配；本模块保证 itemOrdinal 按
日内产出顺序稳定（0 起）。

依赖：ijson==3.5.1（纯 Python 可用，Windows/py3.12 验证；引依赖理由见 M5a commit：
正确处理跨 chunk 字符串/转义/嵌套/闭合，替代手写解析）。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any

import ijson

logger = logging.getLogger(__name__)

#: 单个候选条目的累计体积上限（spec §10.1：超限明确降级，不静默吞）
MAX_ITEM_JSON_CHARS = 16_000
_TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
_DAY_PREFIX = "daily_plans.item"
_ITEM_PREFIX = "daily_plans.item.items.item"
_SCALAR_EVENTS = ("string", "number", "boolean", "null")


class TruncatedTripPlanError(ValueError):
    """顶层 JSON 未闭合（半截输出）；调用方走逐日 fallback，不冒充成功。"""


@dataclass
class CandidateItem:
    """一个完整闭合且过基础形状校验的候选条目。"""

    day_no: int
    item_ordinal: int
    item: dict[str, Any] = field(default_factory=dict)


def _basic_item_shape(item: dict[str, Any]) -> str | None:
    """基础形状校验，返回问题描述（None = 通过）。schema 级校验仍归上层。"""
    name = item.get("poi_name")
    if not isinstance(name, str) or not name.strip():
        return "poi_name 缺失或非文本"
    for key in ("start_time", "end_time"):
        value = item.get(key)
        if value is not None and not (isinstance(value, str) and _TIME_RE.match(value.strip())):
            return f"{key} 不是 HH:MM 时间"
    return None


class _ValueFolder:
    """ijson 事件 → 嵌套值 的小型折叠器（词法是 ijson 的，这里只组装）。"""

    def __init__(self) -> None:
        self.stack: list[tuple[str, Any]] = []
        self.root: Any = None

    def feed(self, prefix: str, event: str, value: Any) -> bool:
        """吃一个事件；返回 True 表示当前根值已闭合（结果在 .root）。"""
        if event == "start_map":
            self.stack.append(("map", {}))
            return False
        if event == "start_array":
            self.stack.append(("array", []))
            return False
        if event == "map_key":
            self.stack.append(("key", value))
            return False
        if event == "end_map":
            done = len(self.stack) == 1
            obj = self._pop({})
            self._attach(obj)
            if done:
                self.root = obj
            return done
        if event == "end_array":
            done = len(self.stack) == 1
            arr = self._pop([])
            self._attach(arr)
            if done:
                self.root = arr
            return done
        if event in _SCALAR_EVENTS:
            done = not self.stack
            self._attach(value)
            if done:
                self.root = value
            return done
        return False

    def _pop(self, default: Any) -> Any:
        if not self.stack:
            return default
        kind, value = self.stack.pop()
        return value if kind in ("map", "array") else default

    def _attach(self, value: Any) -> None:
        if not self.stack:
            return
        kind, top = self.stack[-1]
        if kind == "key":
            self.stack.pop()
            kind2, container = self.stack[-1] if self.stack else ("map", {})
            if kind2 == "map" and isinstance(container, dict):
                container[str(top)] = value
            elif kind2 == "array" and isinstance(container, list):
                container.append(value)
        elif kind == "array" and isinstance(top, list):
            top.append(value)


@dataclass
class _DayAssembly:
    """一个 day 对象的装配状态。"""

    order: int
    day_no: int | None = None
    day: dict[str, Any] = field(default_factory=lambda: {"day_no": None, "items": []})
    last_key: str | None = None
    item_ordinal: int = 0
    #: day_no 未定（后置）期间完成的候选，随日闭合统一冲刷
    buffered: list[CandidateItem] = field(default_factory=list)


class TripPlanStreamParser:
    """整趟 JSON 的增量解析器：feed 文本块 → 产出完整闭合的候选条目。"""

    def __init__(self) -> None:
        self._events: ijson.sendable_list = ijson.sendable_list()
        self._coro = ijson.parse_coro(self._events)
        self._consumed = 0
        self._top_level_closed = False
        self._days: list[dict[str, Any]] = []
        self._day: _DayAssembly | None = None
        self._pending: list[CandidateItem] = []  # 已发布序、归属未定的候选
        self._seen_day_nos: set[int] = set()
        self._suggestions: list[dict[str, Any]] = []
        self._suggestions_folder: _ValueFolder | None = None
        self._item_folder: _ValueFolder | None = None
        self._item_chars = 0

    # ---- 对外接口 ----

    def feed(self, chunk: str | bytes) -> list[CandidateItem]:
        """推入一段文本增量，返回本段新完成的候选条目（归属已确定）。"""
        self._events.clear()
        self._consumed = 0
        if isinstance(chunk, str):
            chunk = chunk.encode("utf-8")
        self._coro.send(chunk)
        return self._drain_events()

    def finish(self) -> tuple[list[dict[str, Any]], list[dict[str, Any]], set[int]]:
        """EOF：返回 (days, suggestions, seen_day_nos)；顶层未闭合抛 TruncatedTripPlanError。"""
        self._events.clear()
        self._consumed = 0
        try:
            self._coro.close()
        except ijson.IncompleteJSONError as exc:
            raise TruncatedTripPlanError(f"顶层 JSON 未闭合：{exc}") from exc
        self._drain_events()
        if not self._top_level_closed:
            raise TruncatedTripPlanError("输出在顶层对象闭合前结束（半截候选不能当完整计划）")
        if self._pending:
            logger.warning("incremental parser dropped %d candidates without day_no", len(self._pending))
            self._pending = []
        return self._days, self._suggestions, self._seen_day_nos

    # ---- 事件折叠 ----

    def _drain_events(self) -> list[CandidateItem]:
        published: list[CandidateItem] = []
        while self._consumed < len(self._events):
            prefix, event, value = self._events[self._consumed]
            self._consumed += 1
            published.extend(self._handle_event(prefix, event, value))
        return published

    def _handle_event(self, prefix: str, event: str, value: Any) -> list[CandidateItem]:
        if prefix == "" and event == "end_map":
            self._top_level_closed = True
            return []
        if self._suggestions_folder is not None:
            if self._suggestions_folder.feed(prefix, event, value):
                self._suggestions = self._suggestions_folder.root or []
                self._suggestions_folder = None
            return []
        if prefix == "suggestions" and event == "start_array":
            self._suggestions_folder = _ValueFolder()
            self._suggestions_folder.feed(prefix, event, value)
            return []
        if self._item_folder is not None:
            published: list[CandidateItem] = []
            self._item_chars += len(str(value))
            if self._item_chars > MAX_ITEM_JSON_CHARS:
                raise ValueError(f"候选条目超过 {MAX_ITEM_JSON_CHARS} 字符上限（超限降级，不冒充候选）")
            if self._item_folder.feed(prefix, event, value):
                item = self._item_folder.root
                self._item_folder = None
                published.extend(self._publish_item(item))
            return published
        if prefix == _ITEM_PREFIX and event == "start_map":
            self._item_folder = _ValueFolder()
            self._item_chars = 0
            self._item_folder.feed(prefix, event, value)  # 根 map 入栈，end_map 时弹空才算闭合
            return []
        if self._day is not None:
            return self._handle_day_event(prefix, event, value)
        if prefix == _DAY_PREFIX and event == "start_map":
            self._day = _DayAssembly(order=len(self._days))
            return []
        return []

    def _handle_day_event(self, prefix: str, event: str, value: Any) -> list[CandidateItem]:
        assert self._day is not None
        if prefix == _DAY_PREFIX and event == "end_map":
            day = self._day.day
            if isinstance(day.get("day_no"), int):
                self._seen_day_nos.add(day["day_no"])
            self._days.append(day)
            flushed = self._flush_buffered()
            self._day = None
            return flushed
        if event == "map_key" and prefix == _DAY_PREFIX:
            self._day.last_key = value
            if value == "day_no":
                # day_no 标量随后到达；先占位确定归属
                pass
            return []
        if event in _SCALAR_EVENTS and prefix.startswith(_DAY_PREFIX + "."):
            # 标量值事件的 prefix 是 `<日prefix>.<键名>`（ijson 约定），据此取键
            key = prefix.rsplit(".", 1)[-1]
            if key == "day_no":
                try:
                    self._day.day_no = int(value)
                    self._day.day["day_no"] = self._day.day_no
                    self._seen_day_nos.add(self._day.day_no)
                except (TypeError, ValueError):
                    self._day.day_no = None
            else:
                self._day.day[key] = value
            return self._flush_buffered()
        if event == "start_map" and prefix == _DAY_PREFIX:
            return []
        if event in ("start_array", "end_array") and prefix == _DAY_PREFIX:
            return []
        # items 数组内层事件（prefix 更深）在 _handle_event 的 item 分支处理
        return []

    def _publish_item(self, item: Any) -> list[CandidateItem]:
        if not isinstance(item, dict):
            return []
        problem = _basic_item_shape(item)
        if problem:
            logger.warning("incremental candidate rejected: %s", problem)
            return []
        assert self._day is not None
        if isinstance(self._day.day.get("items"), list):
            self._day.day["items"].append(dict(item))
        candidate = CandidateItem(
            day_no=self._day.day_no if isinstance(self._day.day_no, int) else -1,
            item_ordinal=self._day.item_ordinal,
            item=dict(item),
        )
        self._day.item_ordinal += 1
        if candidate.day_no == -1:
            self._day.buffered.append(candidate)
            return []
        self._pending.append(candidate)
        return self._drain_pending()

    def _flush_buffered(self) -> list[CandidateItem]:
        """日闭合/day_no 到达：确定归属并冲刷 pending（含后置缓冲）。"""
        if self._day is None or not isinstance(self._day.day_no, int):
            return []
        self._pending.extend(self._day.buffered)
        self._day.buffered = []
        return self._drain_pending()

    def _drain_pending(self) -> list[CandidateItem]:
        published: list[CandidateItem] = []
        still: list[CandidateItem] = []
        for candidate in self._pending:
            if candidate.day_no == -1:
                if self._day is None or not isinstance(self._day.day_no, int):
                    still.append(candidate)
                    continue
                candidate.day_no = self._day.day_no
            published.append(candidate)
        self._pending = still
        return published
