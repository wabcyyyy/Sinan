"""点位同一性判定：名称归一、球面距离与跨天去重。

「这两个名字/这两个坐标是不是同一个地方」是生成链路里被问得最多的一件事：存在性
解析器（`existence`）用它挡「解析到了另一个点」，证据票（`grounding_evidence`）用
它比对签发坐标，整段生成的边判去重（`PoiSeenRegistry`，PR-4 起两条链共用）判同日与
跨天重复。判定口径只留一份实现：各写一份归一时，分歧会以「重复点位没删掉」或「把真实
点位当重复删掉」两种形式出现在产出里，而后者正好砸在 A1 的真实性上。

`haversine_m` 与 `app.agent.core.geo.haversine_meters` 不是一条语义，故不复用：本函数把
0/0 与非数值坐标判为 `inf`（缺失哨兵），调用方据此退化成纯名称判定；geo 版没有这个
约定，且不做 `asin` 入参钳制。

依赖：stdlib 与 common.config 的名称阈值；不 import 其他 agent 域，生成与解析共用。
"""

from __future__ import annotations

import re
from math import asin, cos, radians, sin, sqrt
from typing import Any

from app.common.config import settings

# 归一化去重键：剥括号注记（"圣家堂（Sagrada Família）"→"圣家堂"）、
# 去空白与常见分隔标点、小写。模型对同一地点常输出名称变体（中英混注、
# 带括号别名），精确字符串比对会漏判跨天重复。
_BRACKET_ANNOTATION_RE = re.compile(r"[（(【\[〔].*?[）)】\]〕]", re.S)
_NAME_SEPARATOR_RE = re.compile(r"[\s·・、,，。．.\-—_]+")

_EARTH_RADIUS_M = 6371000.0


def strip_name_annotation(name: Any) -> str:
    """剥掉地点名里的括号注记（分店/别名/原文注音），保留其余文本。

    单一真源：去重键与存在性解析的查询串都必须用同一种写法——「楼外楼（孤山路
    总店）」与「楼外楼」是同一家店，实测有 24/192 个点位只因这串注记而查不到。
    """
    return _BRACKET_ANNOTATION_RE.sub("", str(name or "")).strip()


def norm_ws_key(name) -> str:
    """宽松名称键：只去空白 + 小写。

    跨模块 API（`suggestions` 的分组、`memory.working` 的对话内去重用它）。
    刻意**不**与 `norm_poi_key` 合并：后者还剥括号注释与分隔符并 casefold，
    换过去会改变判重结果——那是行为决策，不是整理。
    """
    return "".join(str(name or "").lower().split())


def norm_poi_key(name) -> str:
    """POI 名称归一化键：同名判定（同日/跨天去重）与 used 比对共用。"""
    s = strip_name_annotation(name)
    s = _NAME_SEPARATOR_RE.sub("", s)
    return s.lower().casefold()


def haversine_m(lat1, lng1, lat2, lng2) -> float:
    """两点球面距离（米）；坐标无效返回 inf（0/0 是缺失哨兵）。"""

    def _v(v):
        try:
            f = float(v)
        except (TypeError, ValueError):
            return None
        return f if abs(f) > 1e-6 else None

    a, b, c, d = _v(lat1), _v(lng1), _v(lat2), _v(lng2)
    if a is None or b is None or c is None or d is None:
        return float("inf")
    p1, p2 = radians(a), radians(c)
    dp = radians(c - a)
    dl = radians(d - b)
    h = sin(dp / 2) ** 2 + cos(p1) * cos(p2) * sin(dl / 2) ** 2
    return 2 * _EARTH_RADIUS_M * asin(min(1.0, sqrt(h)))


class PoiSeenRegistry:
    """跨天/同日重复点位登记表：归一化同名 + 同类型近距离坐标双通道。

    名称变体（"圣家堂" vs "圣家堂大教堂"）光靠字符串归一抓不住；落地
    （引用背书/高德/Google）补上真实坐标后，同类型点位相距 <80m 即视为
    同一地点。坐标缺失时退化为纯名称判定。
    酒店不参与判重：全程同一家酒店跨天重复是摊铺语义（N-1 晚口径）。
    """

    def __init__(self, max_proximity_m: float = 80.0) -> None:
        self.max_proximity_m = max_proximity_m
        self._keys: set[str] = set()
        self._coords: list[tuple[str, float, float]] = []

    def is_duplicate(self, name, item_type, latitude=None, longitude=None) -> bool:
        if str(item_type or "") == "hotel":
            # 酒店全程同一家是摊铺语义：跨天同名合法，不参与判重
            return False
        key = norm_poi_key(name)
        if key and key in self._keys:
            return True
        if latitude is None or longitude is None:
            # 坐标缺失就只走名称通道。`haversine_m` 本身对 None 返回 inf，
            # 所以这里是把意图写明，不是补一个崩溃（注释此前说反了）。
            return False
        want = str(item_type or "")
        dist = min(
            (haversine_m(latitude, longitude, la, ln) for t, la, ln in self._coords if t == want),
            default=float("inf"),
        )
        return dist < self.max_proximity_m

    def register(
        self, name: str, item_type: str | None, latitude: float | None = None, longitude: float | None = None
    ) -> None:
        if str(item_type or "") == "hotel":
            return
        key = norm_poi_key(name)
        if key:
            self._keys.add(key)
        if latitude is None or longitude is None:
            return
        try:
            lat, lng = float(latitude), float(longitude)
        except (TypeError, ValueError):
            return
        if abs(lat) > 1e-6 and abs(lng) > 1e-6:
            self._coords.append((str(item_type or ""), lat, lng))


_MIN_SHARED_CHARS = 4


def same_entity(query: str, candidate: str) -> bool:
    """同一实体判定：外部检索对任意输入都会尽力返回，不设门槛就会把
    「不存在的景点」锚定到无关坐标（典型事故：西安「方所书店」→ 安徽某防治所）。

    规则：归一（去空白/标点/大小写）后相等或互为包含 → 同一实体；否则要求
    字符重叠率 ≥ `entity_name_similarity_min` **且** 共享字符 ≥ 4——只有比率
    不够，短名（"老街"vs"老城"）比率高却不是同一处。
    """

    def norm(value: str) -> str:
        return "".join(ch for ch in strip_name_annotation(value).lower() if ch.isalnum())

    q, c = norm(query), norm(candidate)
    if not q or not c:
        return False
    if q == c or q in c or c in q:
        return True
    overlap = len(set(q) & set(c))
    union = len(set(q) | set(c))
    return union > 0 and overlap / union >= settings.entity_name_similarity_min and overlap >= _MIN_SHARED_CHARS


def matches_any(query: str, spellings: list[str] | tuple[str, ...] | None) -> bool:
    """对 provider 返回的全部名称写法（本地名 + `name:xx` 别名）逐一判等。

    跨脚本匹配只能靠别名：`淺草寺` 的 `name:ja` 是 `浅草寺`、`奧賽博物館` 的
    `name:zh-Hans` 是 `奥赛博物馆`——简繁/中英写法差异占实测未判定的 56%。
    """
    return any(same_entity(query, text) for text in spellings or [])


def replace_name_mentions(value: Any, aliases: dict[str, str]) -> Any:
    """只替换已被实体解析确认的名称；不做字形猜测或自行翻译。"""
    if not aliases:
        return value
    if isinstance(value, str):
        pattern = re.compile("|".join(re.escape(name) for name in sorted(aliases, key=len, reverse=True)))
        return pattern.sub(lambda hit: aliases[hit.group()], value)
    if isinstance(value, list):
        return [replace_name_mentions(item, aliases) for item in value]
    if isinstance(value, dict):
        return {key: replace_name_mentions(item, aliases) for key, item in value.items()}
    return value


def matches_city_name(city: str, row: dict) -> bool:
    # 城市名只接受完整名称/别名（市/县后缀可省略）；POI 的模糊包含不能用于目的地。
    def key(name: str) -> str:
        return "".join(ch for ch in name.casefold() if ch.isalnum()).removesuffix("市").removesuffix("县")

    spellings = [row.get("name") or "", *(row.get("aliases") or [])]
    return any(key(city) == key(str(name)) for name in spellings if name)
