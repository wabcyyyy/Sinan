"""外部调用基类（G-3.2）：超时 / 响应上限 / TTL 缓存 / 自节流 / 重试 / 熔断 / key 解析链。

适用面：三处对外取数——图片查找（Unsplash / Wikipedia）、实时价格（酒店/餐饮
的联网核价）、联网搜索补池。它们此前各写各的：有的裸 `httpx.get`（超时是有，
但没有缓存、没有节流、没有响应字节上限），有的经 llm_client（有超时，同样
无缓存无节流）。本基类把这些事收成一处，调用方只提供"怎么取数"。

能力清单（INV-9 的落地形态）：
1. **超时**：每个调用都有明确上限（`timeout_seconds`），由 loader 使用；
2. **响应字节上限**：`clamp_bytes` 用于 httpx 通道（超限即拒，不截断后当成功）；
   LLM 通道由 `max_tokens` 承担，调用方在 loader 里设；
3. **TTL 缓存**：命中即返回，不打网络；**负结果用更短 TTL**（查不到要能较快重试，
   查得到的结果可以缓存久些）；
4. **自节流（双车道）**：interactive（用户在等，间隔短）与 background（后台补池，
   间隔长）；等待超过 `max_wait_seconds` 就**放弃本次调用**（返回 None），
   绝不把后台补池拖成前台阻塞；
5. **重试（L1）**：loader 抛异常时按 `retry_attempts` 额外重试，重试前重新走车道
   节流（退避与限速一体，不会绕过供应商限流）。**只对幂等 GET 开**——开重试的
   通道必须只读（本基类的 loader 全部是取数 GET）；默认 0 = 不重试（旧行为）；
6. **熔断（L1）**：连续失败达阈值即开冷却窗；窗口内直接返回 None **不外呼**
   （高基数键场景负缓存挡不住：enrich 12 个不同 xid 就是 12 次真实请求，熔断在
   第 5 次失败后切断其余）。窗口固定不续期（同 redis_client 口径），到期后下一
   次调用自然放行探测；成功即清零计数。合法空结果（供应商答"没有"）不算失败；
7. **key 解析链**：env → 实例配置 → 调用方自带，逐级回落；**绝不复用他人的 key**
   （`resolve_key` 只在自己这一串里选，不跨实例借用）。

线程安全：缓存、车道时间槽与熔断状态都加锁（生成跑在 worker 线程，管理面在
主线程）；锁内只做计算与登记，**睡眠永远在锁外**（见 `_acquire_slot`）。

依赖：标准库（threading/time/logging/json）+ httpx（只为 `fetch_bytes`/`fetch_json`
两个外呼助手签名与超时服务）；具体的 loader 仍由调用方注入。
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Generic, TypeVar

import httpx

logger = logging.getLogger(__name__)

T = TypeVar("T")

INTERACTIVE = "interactive"
BACKGROUND = "background"

#: 车道最小调用间隔（秒）：前台已可容忍短等待，后台补池应明显更克制。
_LANE_MIN_INTERVAL: dict[str, float] = {INTERACTIVE: 0.2, BACKGROUND: 1.0}

# rate_group 共享限速表（R3-F2）：组名 → (车道 → 最近时间槽)。同组实例在此
# 预约时间槽，供应商总速率 = 组内同车道共用一条时间线，叠加不破约束。
_group_lane_last: dict[str, dict[str, float]] = {}
_group_lane_lock = threading.Lock()

#: 异常消息脱敏（L12）：httpx 的异常文本内嵌完整请求 URL，而 Hotellook/SerpApi
#: 的 token、OTM 的 apikey、Unsplash 的 client_id 都走 query 参数——一次 5xx
#: 就能把密钥打进日志。
#: 正则与 agent runtime trace 的 _SECRET_PATTERNS 同族；common 不能反向 import
#: agent，两边各自覆盖自己负责的出口（trace 管轨迹持久化，本模块管日志出口）。
_SECRET_QUERY_RE = re.compile(
    r"([?&](?:key|token|secret|api_key|apikey|access_key|client_id)=)[^&\s\"']+", re.IGNORECASE
)
_BEARER_RE = re.compile(r"(Bearer\s+)[A-Za-z0-9._\-]+", re.IGNORECASE)


def redact_secrets(text: str | BaseException) -> str:
    sanitized = _SECRET_QUERY_RE.sub(r"\1<redacted>", str(text))
    return _BEARER_RE.sub(r"\1<redacted>", sanitized)


@dataclass
class ExternalClient(Generic[T]):
    """一个外部数据源的取数门面（泛型参数 = 成功返回的类型）。"""

    name: str
    #: 成功结果 TTL（秒）
    ttl_seconds: float = 900.0
    #: 负结果（None / 空列表）TTL（秒）：更短，保证"供应商暂时挂了"能较快重试
    negative_ttl_seconds: float = 60.0
    #: 响应字节上限（httpx 通道用 clamp_bytes；LLM 通道由 max_tokens 承担）
    max_response_bytes: int = 256 * 1024
    #: 进程内缓存条目上限：高基数键（点位名、坐标串）在长跑进程里必须有界（R2-10）
    max_entries: int = 2048
    #: 单次调用超时（秒）：由 `fetch_json`/`fetch_bytes` 真正传给 httpx（per-request
    #: 覆盖共享客户端的默认超时）。以前它只是声明，调用点各写各的。
    timeout_seconds: float = 8.0
    #: 节流等待上限（秒）：超过即放弃本次调用（不自旋、不阻塞车道）
    max_wait_seconds: float = 1.5
    #: 本实例的车道最小间隔覆盖（秒）：个别供应商有硬性限流（如 Nominatim 1 rps），
    #: 需要比车道默认值更保守的间隔；None = 沿用车道默认。
    min_interval_seconds: float | None = None
    #: loader 抛异常后的额外重试次数（L1）。只对幂等只读通道开启；0 = 不重试。
    #: 重试不放大限流风险：每次重试都重新预约车道时间槽。
    retry_attempts: int = 0
    #: 重试前的最小退避（秒）；实际间隔 = max(退避, 车道槽等待)。
    retry_backoff_seconds: float = 0.3
    #: 熔断（L1）：连续失败（异常，非合法空结果）达到阈值即开冷却窗。
    circuit_failure_threshold: int = 5
    #: 熔断冷却窗（秒）：固定窗口不续期，到期后下一次调用自然放行探测。
    circuit_cooldown_seconds: float = 30.0
    #: 同一供应商的共享限速组（R3-F2）：同组实例的车道时间槽记在同一张表上，
    #: 多实例（按 geo/radius/detail 分缓存与重试策略）叠加也不破供应商总速率——
    #: per-instance 节流下 OTM 三车道叠加实测 ≈6.8 rps，破免费档 5 rps 的自设约束。
    #: 熔断计数与缓存仍按实例各记，不回归。None = 实例私有时间槽（旧行为）。
    rate_group: str | None = None

    _cache: dict[str, tuple[Any, float]] = field(default_factory=dict, init=False, repr=False)
    _lane_last: dict[str, float] = field(default_factory=dict, init=False, repr=False)
    _consecutive_failures: int = field(default=0, init=False, repr=False)
    _breaker_open_until: float = field(default=0.0, init=False, repr=False)
    _last_skip: str | None = field(default=None, init=False, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)

    # ---- key 解析链 -------------------------------------------------------

    @staticmethod
    def resolve_key(*candidates: str | None) -> str | None:
        """按 env → 实例配置 → 调用方自带 的顺序取第一个非空 key。

        只在本链内回落：任何一级都不去"借用"别处的凭据（G-3.2 硬约束）。
        """
        for candidate in candidates:
            text = str(candidate or "").strip()
            if text:
                return text
        return None

    # ---- 缓存 -------------------------------------------------------------

    def _is_negative(self, value: Any) -> bool:
        return value is None or (isinstance(value, list) and not value)

    def _cached(self, cache_key: str) -> tuple[bool, Any]:
        with self._lock:
            entry = self._cache.get(cache_key)
            if entry is None:
                return False, None
            value, expires_at = entry
            if expires_at <= time.monotonic():
                self._cache.pop(cache_key, None)
                return False, None
            return True, value

    def _store(self, cache_key: str, value: Any) -> None:
        ttl = self.negative_ttl_seconds if self._is_negative(value) else self.ttl_seconds
        if ttl <= 0:
            return
        with self._lock:
            self._cache[cache_key] = (value, time.monotonic() + ttl)
            if len(self._cache) <= self.max_entries:
                return
            # 与 cache_store 的兜底表同理：高基数键（POI 名、坐标串）+ 长跑进程 = 无界增长。
            # 先清过期，仍超限按最早到期逐出（不含刚写入的这条）。
            now = time.monotonic()
            for key in [k for k, (_v, exp) in self._cache.items() if exp <= now]:
                self._cache.pop(key, None)
            overflow = len(self._cache) - self.max_entries
            if overflow > 0:
                candidates = sorted((k for k in self._cache if k != cache_key), key=lambda k: self._cache[k][1])
                for key in candidates[:overflow]:
                    self._cache.pop(key, None)

    def clear_cache(self) -> None:
        """测试/管理用：清空缓存（便于断言"真的又打了一次"）。"""
        with self._lock:
            self._cache.clear()

    # ---- 节流 -------------------------------------------------------------

    def _acquire_slot(self, lane: str) -> bool:
        """按车道最小间隔**预约**时间槽；等待在锁外进行，超过 max_wait 放弃。

        旧实现持锁 sleep（L1 补充 2）：同一车道的并发调用（enrich 并行化后的
        detail 请求）会在锁后串行排队，把"限速"放大成"排队阻塞"。现改为锁内
        只做预约——每个调用领到 `max(now, 上次槽 + interval)` 的时间槽并立刻
        让出锁，睡与发都在锁外；相邻两次真实外呼的间隔仍严格 ≥ interval，且
        并发调用各自领到错开的槽，不会像旧实现那样同时醒来一齐打出去。

        `rate_group` 非空时时间槽记在**组级共享表**（R3-F2）：同组同车道共用
        一条时间线（各自的 min_interval 仍按实例生效），多实例叠加不破供应商
        总速率。组级表共一把模块锁——临界区只有一次字典读写，竞争可忽略。
        """
        interval = self.min_interval_seconds or _LANE_MIN_INTERVAL.get(lane, _LANE_MIN_INTERVAL[BACKGROUND])
        if self.rate_group is not None:
            with _group_lane_lock:
                table = _group_lane_last.setdefault(self.rate_group, {})
                now = time.monotonic()
                scheduled = max(now, table.get(lane, 0.0) + interval)
                wait = scheduled - now
                if wait > self.max_wait_seconds:
                    logger.info("external[%s] throttled on %s lane (wait %.2fs)", self.name, lane, wait)
                    return False
                table[lane] = scheduled
        else:
            with self._lock:
                now = time.monotonic()
                scheduled = max(now, self._lane_last.get(lane, 0.0) + interval)
                wait = scheduled - now
                if wait > self.max_wait_seconds:
                    logger.info("external[%s] throttled on %s lane (wait %.2fs)", self.name, lane, wait)
                    return False
                self._lane_last[lane] = scheduled
        if wait > 0:
            time.sleep(wait)
        return True

    # ---- 熔断（L1） ---------------------------------------------------------

    def _breaker_open(self) -> bool:
        with self._lock:
            return time.monotonic() < self._breaker_open_until

    def _record_outcome(self, failed: bool) -> None:
        """熔断计数：成功清零；连续失败达阈值开**固定**冷却窗（窗口内不续期，
        否则持续流量下永远探不到恢复——同 redis_client 的口径）。"""
        if not failed:
            with self._lock:
                self._consecutive_failures = 0
            return
        with self._lock:
            self._consecutive_failures += 1
            if self._consecutive_failures < self.circuit_failure_threshold:
                return
            now = time.monotonic()
            if now < self._breaker_open_until:
                return
            self._breaker_open_until = now + self.circuit_cooldown_seconds
            self._consecutive_failures = 0
            logger.warning(
                "external[%s] breaker OPEN for %.0fs after %d consecutive failures (degrading to fallback source)",
                self.name,
                self.circuit_cooldown_seconds,
                self.circuit_failure_threshold,
            )

    def reset_runtime_state(self) -> None:
        """测试/管理用：清空车道时间槽、熔断状态与连续失败计数（缓存不动）。

        车道时间戳必须一起清：模块级客户端的 `_lane_last` 记的是真实时钟，
        假钟用例（time.monotonic 被替换到很小的值）若读到它，会把每次调用都
        判成"等待超窗"而放弃。rate_group 实例清的是**整组**共享表——同组
        其它实例的时间槽同源，留着同样会毒化假钟用例。
        """
        with self._lock:
            self._lane_last.clear()
            self._consecutive_failures = 0
            self._breaker_open_until = 0.0
        if self.rate_group is not None:
            with _group_lane_lock:
                _group_lane_last.pop(self.rate_group, None)

    # ---- 取数 -------------------------------------------------------------

    def _call_with_retry(self, loader: Callable[[], T | None], lane: str) -> tuple[T | None, bool]:
        """执行 loader，返回 (结果, 是否失败)。失败 = 全部尝试都抛异常（含重试
        被车道放弃——重试同样要守供应商限流，守不住就不硬发）。"""
        attempts = 1 + max(0, self.retry_attempts)
        for attempt in range(attempts):
            if attempt:
                # 退避后再预约车道槽：实际间隔 = max(退避, 车道最小间隔)。
                time.sleep(self.retry_backoff_seconds)
                if not self._acquire_slot(lane):
                    logger.info("external[%s] retry on %s lane throttled past max_wait, giving up", self.name, lane)
                    return None, True
            try:
                return loader(), False
            except Exception as exc:
                logger.warning(
                    "external[%s] call failed (attempt %d/%d): %s",
                    self.name,
                    attempt + 1,
                    attempts,
                    redact_secrets(exc),
                )
        return None, True

    def last_skip_reason(self) -> str | None:
        """最近一次 `call()` 返回 None 的原因（R3-F3）。

        取值：`"breaker_open"`（熔断开窗未外呼）/ `"lane_busy"`（车道等待超窗放弃）/
        `"loader_error"`（loader 全部尝试都抛异常）/ `"loader_empty"`（loader 真跑且
        返回空——对 ASR 是"上游明确说这段音频没有内容"）/ `"cache"`（缓存命中，
        含负缓存）/ `"ok"`（拿到非空结果）。返回 None 仅指"从未调用过"。

        无锁裸读：**只配合同步调用方在 `call()` 返回后立刻读取**（同线程内写读，
        不保证跨线程可见性）；research 域等异步消费方不要用它。
        """
        return self._last_skip

    def call(
        self,
        cache_key: str,
        loader: Callable[[], T | None],
        *,
        lane: str = INTERACTIVE,
    ) -> T | None:
        """缓存 → 熔断 → 节流 → 重试 loader 的取数流程；任何异常都降级为 None
        （不打断生成），但降级不再静默：重试与熔断开窗都有 warning 日志（L1
        降级链可见），上层（research 域）以 degraded 证据包如实接住。

        None 的成因经 `last_skip_reason()` 可辨（R3-F3）：对"空结果要不要引导
        重试"敏感的调用方（如 ASR）据此区分"服务没调通"与"上游明确空转写"。
        """
        hit, value = self._cached(cache_key)
        if hit:
            self._last_skip = "cache"
            return value
        if self._breaker_open():
            self._last_skip = "breaker_open"
            return None
        if not self._acquire_slot(lane):
            self._last_skip = "lane_busy"
            return None
        result, failed = self._call_with_retry(loader, lane)
        self._last_skip = "ok" if result else ("loader_error" if failed else "loader_empty")
        self._record_outcome(failed)
        self._store(cache_key, result)
        return result

    def clamp_bytes(self, payload: bytes | str | None) -> bytes | None:
        """响应字节上限：超限返回 None（拒收），不做"截断后当成功"。

        截断的 JSON/HTML 交给上层解析只会产出脏数据，不如让调用方走降级。
        """
        if payload is None:
            return None
        data = payload.encode("utf-8") if isinstance(payload, str) else payload
        if len(data) > self.max_response_bytes:
            logger.warning("external[%s] response too large: %d > %d", self.name, len(data), self.max_response_bytes)
            return None
        return data


def fetch_bytes(
    client: ExternalClient[Any],
    http: httpx.Client,
    url: str,
    *,
    params: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    json_body: dict[str, Any] | None = None,
) -> bytes | None:
    """INV-9 的真实执行点：GET/POST → 本 client 的 per-request 超时 → 状态检查 → 字节上限。

    为什么要有这个函数：`timeout_seconds`/`max_response_bytes` 过去只是**声明**，调用点
    各写各的 `api_client().get(...).json()`——于是"每个外呼都有超时与响应上限"这条
    纪律只在图片通道成立，结构化 JSON 通道（OTM/Nominatim/Open-Meteo）三条都没上限。
    `json_body` 走 POST：给需要请求体的现代 API（Places、MiMo ASR 一类）留的口，纪律不变。
    """
    if json_body is not None:
        response = http.post(url, json=json_body, params=params, headers=headers, timeout=client.timeout_seconds)
    else:
        response = http.get(url, params=params, headers=headers, timeout=client.timeout_seconds)
    response.raise_for_status()
    return client.clamp_bytes(response.content)


def fetch_json(
    client: ExternalClient[Any],
    http: httpx.Client,
    url: str,
    *,
    params: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    json_body: dict[str, Any] | None = None,
) -> Any:
    """`fetch_bytes` + JSON 解析；形状是否可用由调用方判（None = 降级，不冒充数据）。"""
    raw = fetch_bytes(client, http, url, params=params, headers=headers, json_body=json_body)
    if raw is None:
        return None
    return json.loads(raw)
