"""LLM 通道级熔断 + 最小告警闭环（审计 §3.1.1 / §3.2.2 / §4.1 P0-2）。

背景（为什么要有它）：2026-10-05 主网关全量失败 2 小时才被人肉翻日志发现——
llm_client 只有"每次调用最多 2 次尝试"的**无记忆**重试，上游宕机时每个请求都
全额支付 connect 5s×2 的超时代价，且全仓没有任何告警出口。本模块把
external_client 已验证的固定窗熔断模式（其 circuit_failure_threshold /
circuit_cooldown_seconds 一族）搬到最贵的 LLM 依赖上，并补最小告警闭环。

口径：
- **通道 = base_url**（网关地址）：部署默认网关与 BYOK 用户网关各算各的；
  进程内状态（单进程前提与 event_hub / 幂等一致）。
- **只有通道级失败计数**：连接层异常与可重试白名单状态（llm_client 的
  `_RETRYABLE_STATUS` 白名单，判定在调用侧 `_is_retryable` 完成后以
  `retryable=True` 传入）；业务 4xx / 内容审查（空 choices）是"这一条请求的
  问题"，不计数——否则一个坏 prompt 连发几次就能把通道熔断，殃及所有人。
- **固定窗不续期**（同 external_client / redis_client 口径）：开窗后冷却期内
  快速失败不外呼；到期后下一次调用自然放行 = 半开探测，探测成功即关窗（发
  恢复事件），探测失败一次即重开窗。
- **告警**（LLM_ALERT_WEBHOOK_URL 配置时才发）：熔断开 / 恢复与滑动窗错误率
  超阈时 POST 一条 JSON；同一 (事件, 通道) 在告警冷却期内不重复发。发送失败
  只记日志（URL/密钥不进日志），绝不影响 LLM 主流程。

依赖：httpx + app.common.http_client（告警走既有 api_client 通道）+
app.common.external_client（redact_secrets）+ app.common.config。被
app.common.llm_client 消费；model_registry 有意不 import 本模块（import
方向见其 docstring），main 角色的备选切换决策因此在 llm_client。
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from dataclasses import dataclass, field

from app.common.config import settings
from app.common.external_client import redact_secrets
from app.common.http_client import api_client

logger = logging.getLogger(__name__)

#: 连续通道级失败达到即开冷却窗（与 external_client.circuit_failure_threshold
#: 同值：对"少量失败后成功"的正常抖动足够迟钝，对全量故障 5 次调用内即切断）
CIRCUIT_FAILURE_THRESHOLD = 5
#: 冷却窗（秒）：固定不续期，到期后下一次调用自然放行半开探测
CIRCUIT_COOLDOWN_SECONDS = 60.0
#: 滑动窗错误率告警的窗长 / 阈值 / 最小样本（样本不足不判，避免 2/3 之类小样本误报）
ALERT_WINDOW_SECONDS = 300.0
ALERT_ERROR_RATE = 0.5
ALERT_MIN_SAMPLES = 10
#: 同一 (事件, 通道) 的告警冷却（秒）：重开窗 / 持续高错误率不刷屏
ALERT_COOLDOWN_SECONDS = 600.0
#: 滑动窗样本上限：让高 QPS 下窗内内存天然有界（R2-10 同课），到顶丢最旧
_WINDOW_MAX_SAMPLES = 500


class LLMCircuitOpen(RuntimeError):
    """通道熔断开窗中的快速失败：冷却窗内不外呼（省下全额超时重试）。

    上层按普通 LLM 失败降级（agent 面走 fallback / 业务面 502 带原因）；配了
    LLM_MAIN_FALLBACK_PROVIDER 时 main 解析层已切到备选通道，正常到不了这里。
    """


@dataclass
class _ChannelHealth:
    """一个通道（base_url）的熔断与错误率状态；全部字段只在 _lock 内读写。"""

    consecutive_failures: int = 0
    #: monotonic 时刻；> now 即开窗。0.0 = 从未开窗（或已被成功探测关闭）。
    open_until: float = 0.0
    #: 错误率滑动窗样本：(monotonic 时刻, 是否通道级失败)。成功也入窗（做分母）；
    #: 业务 4xx / 内容审查不入窗——它量的是通道健康，不是请求健康。
    window: deque[tuple[float, bool]] = field(default_factory=lambda: deque(maxlen=_WINDOW_MAX_SAMPLES))


_states: dict[str, _ChannelHealth] = {}
_alert_last_sent: dict[tuple[str, str], float] = {}
_lock = threading.Lock()


def is_open(base_url: str) -> bool:
    """该通道当前是否开窗（冷却窗已过 = False，即放行半开探测）。"""
    with _lock:
        state = _states.get(base_url)
        return state is not None and state.open_until > time.monotonic()


def check(base_url: str) -> None:
    """开窗中抛 LLMCircuitOpen（快速失败，不外呼）；关窗 / 半开放行。

    半开（冷却窗已过）在这里**不**改状态：放行的那次真实调用就是探测，成败由
    note_success（关窗） / note_failure（一次即重开）决定。
    """
    with _lock:
        state = _states.get(base_url)
        remaining = (state.open_until - time.monotonic()) if state is not None else 0.0
    if remaining > 0:
        raise LLMCircuitOpen(
            f"LLM 通道 {base_url} 熔断开窗中（连续 {CIRCUIT_FAILURE_THRESHOLD} 次通道级失败，"
            f"约 {remaining:.0f}s 后放行半开探测）：快速失败不外呼"
        )


def note_success(base_url: str) -> None:
    """一次真实调用成功：清零连续失败；若此前开窗（含半开探测通过）则关窗并告警恢复。"""
    now = time.monotonic()
    recovered = False
    rate_detail: dict | None = None
    with _lock:
        state = _states.setdefault(base_url, _ChannelHealth())
        _prune_window(state, now)
        state.window.append((now, False))
        state.consecutive_failures = 0
        if state.open_until > 0.0:
            state.open_until = 0.0
            recovered = True
        rate_detail = _error_rate_detail(state)
    if recovered:
        logger.info("llm breaker CLOSED on %s（探测成功，通道恢复）", base_url)
        _fire_alert("circuit_recovered", base_url, {"cooldown_seconds": CIRCUIT_COOLDOWN_SECONDS})
    if rate_detail is not None:
        _fire_alert("error_rate", base_url, rate_detail)


def note_failure(base_url: str, *, retryable: bool) -> None:
    """一次真实调用失败；retryable = 是否通道级失败（连接层错误 / 可重试白名单状态）。

    业务 4xx / 内容审查（retryable=False）不计熔断、不入错误率窗——它们说明的是
    "这条请求有问题"，不是"通道有问题"。
    """
    now = time.monotonic()
    open_detail: dict | None = None
    rate_detail: dict | None = None
    with _lock:
        state = _states.setdefault(base_url, _ChannelHealth())
        if not retryable:
            return
        _prune_window(state, now)
        state.window.append((now, True))
        if state.open_until > now:
            # 开窗期内的在途调用失败（check 放行早于开窗的竞态）：固定窗不续期、不重复告警
            return
        state.consecutive_failures += 1
        half_open_probe_failed = state.open_until > 0.0
        if half_open_probe_failed or state.consecutive_failures >= CIRCUIT_FAILURE_THRESHOLD:
            state.open_until = now + CIRCUIT_COOLDOWN_SECONDS
            state.consecutive_failures = 0
            open_detail = {
                "half_open_probe": half_open_probe_failed,
                "cooldown_seconds": CIRCUIT_COOLDOWN_SECONDS,
                "threshold": CIRCUIT_FAILURE_THRESHOLD,
            }
        rate_detail = _error_rate_detail(state)
    if open_detail is not None:
        logger.warning(
            "llm breaker OPEN on %s for %.0fs（连续通道级失败，窗口内快速失败不外呼）",
            base_url,
            CIRCUIT_COOLDOWN_SECONDS,
        )
        _fire_alert("circuit_open", base_url, open_detail)
    if rate_detail is not None:
        _fire_alert("error_rate", base_url, rate_detail)


def reset_for_tests() -> None:
    """测试用：清空通道熔断与告警节流状态（conftest 逐用例调用，防跨用例串味）。"""
    with _lock:
        _states.clear()
        _alert_last_sent.clear()


def _prune_window(state: _ChannelHealth, now: float) -> None:
    """丢掉滑出错误率窗的样本（调用方持锁）。"""
    cutoff = now - ALERT_WINDOW_SECONDS
    while state.window and state.window[0][0] <= cutoff:
        state.window.popleft()


def _error_rate_detail(state: _ChannelHealth) -> dict | None:
    """窗内通道级失败占比超阈且样本足够时返回告警明细（调用方持锁）。"""
    samples = len(state.window)
    if samples < ALERT_MIN_SAMPLES:
        return None
    failed = sum(1 for _ts, is_fail in state.window if is_fail)
    rate = failed / samples
    if rate < ALERT_ERROR_RATE:
        return None
    return {
        "error_rate": round(rate, 3),
        "failed": failed,
        "samples": samples,
        "window_seconds": ALERT_WINDOW_SECONDS,
    }


def _fire_alert(event: str, base_url: str, detail: dict) -> None:
    """POST 一条 JSON 到 settings.llm_alert_webhook_url；未配置 = 不发。

    节流按"发起即占位"记（发送失败也占冷却位）：告警是低频事件，反过来会让
    死 webhook 被每次事件重试刷屏。网络 I/O 在锁外（同 external_client 的
    "睡眠永远在锁外"纪律）；任何失败只记 warning（异常经 redact_secrets）。
    """
    hook = settings.llm_alert_webhook_url.strip()
    if not hook:
        return
    key = (event, base_url)
    now = time.monotonic()
    with _lock:
        if now - _alert_last_sent.get(key, 0.0) < ALERT_COOLDOWN_SECONDS:
            return
        _alert_last_sent[key] = now
    payload = {"event": f"llm.{event}", "channel": base_url, "detail": detail, "ts": int(time.time())}
    try:
        response = api_client().post(hook, json=payload)
        if response.status_code >= 400:
            logger.warning("llm alert webhook (%s) returned HTTP %s", event, response.status_code)
    except Exception as exc:
        logger.warning("llm alert webhook (%s) send failed: %s", event, redact_secrets(exc))
