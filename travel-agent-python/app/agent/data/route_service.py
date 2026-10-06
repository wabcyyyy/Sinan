"""路线服务适配层。

路线是旅行规划中的事实数据，不应由模型猜测。本模块提供一个很薄的、可
替换的路线服务：**默认只用坐标估算**（haversine × 道路系数 + 缓冲，结果带
``degraded=True``），不查任何外部路线 API——高德 v5 与 Google Routes 源已于
2026-09-15 随「去高德」移除。`fetcher` 注入保留给测试替身与将来的本地路线源。

对外只暴露领域契约，优化器和校验器不需要知道底层是估算、测试替身还是将来的
本地路网。进程内缓存用于避免同一轮规划重复查询同一条边。
"""

from __future__ import annotations

import math
import threading
import time
from collections.abc import Callable
from datetime import datetime

from app.agent.core.geo import haversine_meters
from app.agent.runtime.trace import record_event
from app.common.config import settings

ESTIMATE_SOURCE = "coordinate-estimate"
# 路线估算常量的唯一声明（G-1.3 ②）：原 reflect.py 重复副本已删除并改从本模块
# import——两处数值漂移会让「路线服务估算」与「reflect 校验」对同一路段给出
# 不一致的时长判断。ROAD_DISTANCE_FACTOR：直线距离 → 城市道路距离的折算系数；
# 25 km/h 为非实时路况下的城市均速；BUFFER 给安全余量，避免"理论刚好可达"。
ROUTE_SPEED_KMH = 25.0
ROAD_DISTANCE_FACTOR = 1.35
ROUTE_BUFFER_RATIO = 0.25
ROUTE_FIXED_BUFFER_MIN = 10
# 坐标估算源的判定容差（分钟，P2 逐日兜底空天 2026-10-05）：估算式含固定+比例
# 两层缓冲，下限即 ceil(5×1.25+10)=17 分钟——相邻点位只留 15 分钟必判不足，
# 与实际距离无关。validate_plans 改按「留白+容差 < 估算需求」判不足，避免
# 1-2 分钟级裕量差触发整日重生成（空天链路的共同前件）；真实路线源不走容差。
ROUTE_ESTIMATE_TOLERANCE_MIN = 5

RouteFetcher = Callable[[dict, dict, str, str | None], dict | None]

_route_cache: dict[tuple, tuple[float, dict]] = {}
_route_lock = threading.Lock()
# 缓存只在读时判 TTL，过期项永不清理会无界增长（每次路线查询写一条）。
# 写入时做一次轻量清扫，并设硬上限兜底。
_ROUTE_CACHE_MAX = 2000


def _prune_route_cache(now: float) -> None:
    if len(_route_cache) < _ROUTE_CACHE_MAX:
        return
    expired = [key for key, (stored_at, _) in _route_cache.items() if now - stored_at > settings.route_cache_ttl]
    for key in expired:
        _route_cache.pop(key, None)
    # 仍超限时按写入时间淘汰最旧的（近似 LRU，避免无界内存）。
    while len(_route_cache) >= _ROUTE_CACHE_MAX:
        oldest = min(_route_cache.items(), key=lambda kv: kv[1][0])[0]
        _route_cache.pop(oldest, None)


def _coords(item: dict) -> tuple[float, float] | None:
    try:
        lat = item.get("latitude")
        lng = item.get("longitude")
        if lat is None or lng is None:
            return None
        return float(lat), float(lng)
    except (TypeError, ValueError):
        return None


def _item_key(item: dict) -> str:
    value = item.get("poi_id") or item.get("id") or item.get("poi_name") or item.get("name")
    if value:
        return str(value)
    coords = _coords(item)
    return ":".join(f"{part:.6f}" for part in coords) if coords else "unknown"


def _departure_hour(departure_time: str | datetime | None) -> int | None:
    if isinstance(departure_time, datetime):
        return departure_time.hour
    if isinstance(departure_time, str):
        try:
            return datetime.fromisoformat(departure_time).hour
        except ValueError:
            try:
                return int(departure_time.strip().split(":", 1)[0])
            except (ValueError, IndexError):
                return None
    return None


def _peak_factor(departure_time: str | datetime | None) -> float:
    hour = _departure_hour(departure_time)
    if hour is None:
        return 1.0
    return settings.route_peak_factor if hour in set(range(7, 10)) | set(range(17, 20)) else 1.0


def is_estimated(source: str | None) -> bool:
    """路线是否来自坐标估算（去高德后：除注入替身外唯一可能的来源）。"""
    return str(source or "") == ESTIMATE_SOURCE


def _base_result(
    first: dict,
    second: dict,
    mode: str,
    *,
    source: str,
    distance_m: float,
    duration_min: int,
    confidence: float,
    degraded: bool,
    departure_time: str | datetime | None = None,
    fallback_reason: str | None = None,
) -> dict:
    result = {
        "from_poi_id": _item_key(first),
        "to_poi_id": _item_key(second),
        "mode": mode,
        "distance_m": round(max(distance_m, 0.0), 1),
        "duration_min": max(int(duration_min), 0),
        "source": source,
        "fetched_at": datetime.now().astimezone().isoformat(),
        "confidence": confidence,
        "degraded": degraded,
    }
    if fallback_reason:
        result["fallback_reason"] = fallback_reason
    factor = _peak_factor(departure_time)
    if factor != 1.0:
        result["base_duration_min"] = result["duration_min"]
        result["peak_factor"] = factor
        result["duration_min"] = math.ceil(result["duration_min"] * factor)
    return result


def coordinate_estimate(
    first: dict,
    second: dict,
    mode: str = "walking",
    departure_time: str | datetime | None = None,
    reason: str = "route_service_unavailable",
) -> dict | None:
    """使用坐标产生保守估算；任一地点缺坐标时不猜路线。"""
    first_coords = _coords(first)
    second_coords = _coords(second)
    if first_coords is None or second_coords is None:
        return None
    distance_m = haversine_meters(*first_coords, *second_coords)
    speed_kmh = 25.0 if mode == "walking" else 32.0
    base = max(5.0, distance_m * ROAD_DISTANCE_FACTOR / (speed_kmh * 1000 / 60))
    duration = math.ceil(base * (1 + ROUTE_BUFFER_RATIO) + ROUTE_FIXED_BUFFER_MIN)
    return _base_result(
        first,
        second,
        mode,
        source=ESTIMATE_SOURCE,
        distance_m=distance_m,
        duration_min=duration,
        confidence=0.45,
        degraded=True,
        departure_time=departure_time,
        fallback_reason=reason,
    )


def _with_peak_factor(result: dict | None, departure_time: str | datetime | None) -> dict | None:
    """对路线结果统一做高峰系数修正（注入的供应商/测试替身同样适用）。"""
    if result is None or not departure_time or "base_duration_min" in result:
        return result
    factor = _peak_factor(departure_time)
    if factor != 1.0:
        result = dict(result)
        result["base_duration_min"] = int(result.get("duration_min") or 0)
        result["peak_factor"] = factor
        result["duration_min"] = math.ceil(result["base_duration_min"] * factor)
    return result


class RouteService:
    """带缓存和降级策略的路线服务。

    去高德后**没有真实路线供应商**：默认路径就是坐标估算（`source=coordinate-estimate`
    且 `degraded=True`）。`fetcher` 注入保留给测试替身与将来的本地路线源——
    不传即不查任何外部服务。
    """

    def __init__(self, fetcher: RouteFetcher | None = None):
        self.fetcher = fetcher
        self._local = threading.local()

    def get_route(
        self, first: dict, second: dict, *, mode: str = "walking", departure_time: str | datetime | None = None
    ) -> dict | None:
        first_key, second_key = _item_key(first), _item_key(second)
        cache_key = (
            first_key,
            second_key,
            tuple(_coords(first) or ()),
            tuple(_coords(second) or ()),
            mode,
            str(departure_time or ""),
        )
        now = time.monotonic()
        cached = _route_cache.get(cache_key)
        if cached and now - cached[0] <= settings.route_cache_ttl:
            return dict(cached[1])

        result: dict | None = None
        if self.fetcher is not None:
            calls = getattr(self._local, "calls", 0)
            if calls >= settings.route_max_calls:
                fallback_reason = "route_call_budget_exhausted"
                result = None
            else:
                self._local.calls = calls + 1
                try:
                    result = self.fetcher(first, second, mode, departure_time)
                except Exception as exc:
                    record_event("tool", "route.fetch", status="error", error=str(exc), metadata={"mode": mode})
                    result = None
                    fallback_reason = "route_service_error"
                else:
                    fallback_reason = "route_service_empty"
        else:
            fallback_reason = "route_estimator_only"

        result = _with_peak_factor(result, departure_time)

        if result is None:
            result = coordinate_estimate(
                first,
                second,
                mode,
                departure_time,
                reason=fallback_reason,
            )
        if result is not None:
            # 清扫要遍历整张表，必须与写入互斥：generation_pool 的多个 worker 同时
            # 走到这里时，未加锁的迭代会在表被改大的一瞬间抛
            # `RuntimeError: dictionary changed size during iteration`，
            # 症状却远在千里之外——当天生成中断 → plan_days 上抛 → 整趟行程判 FAILED。
            with _route_lock:
                _prune_route_cache(now)
                _route_cache[cache_key] = (now, dict(result))
            record_event(
                "tool",
                "route.get",
                metadata={
                    "mode": mode,
                    "source": result["source"],
                    "degraded": result["degraded"],
                    "duration_min": result["duration_min"],
                },
            )
        return result

    def matrix(
        self, items: list[dict], *, mode: str = "walking", departure_time: str | datetime | None = None
    ) -> dict[tuple[str, str], dict]:
        # 调用预算属于一次矩阵构建，不应在全局单例生命周期内累计。
        self._local.calls = 0
        matrix: dict[tuple[str, str], dict] = {}
        for index, first in enumerate(items):
            for second in items[index + 1 :]:
                result = self.get_route(first, second, mode=mode, departure_time=departure_time)
                reverse = self.get_route(second, first, mode=mode, departure_time=departure_time)
                if result is not None:
                    matrix[(_item_key(first), _item_key(second))] = result
                if reverse is not None:
                    matrix[(_item_key(second), _item_key(first))] = reverse
        return matrix


def clear_route_cache() -> None:
    _route_cache.clear()


default_route_service = RouteService()


def get_route_matrix(
    items: list[dict], *, mode: str = "walking", departure_time: str | datetime | None = None
) -> dict[tuple[str, str], dict]:
    return default_route_service.matrix(items, mode=mode, departure_time=departure_time)
