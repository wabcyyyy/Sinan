"""运维告警的唯一出口（上线 v1 T8b）。

一条 webhook、一个冷却表、一套脱敏口径。`llm_breaker` 原先自带一份发送器与冷却字典，
本模块把它收成**一处**：告警是观测面，出现第二条通道就会开始各自漂移（谁在静默、
谁在脱敏、冷却多长都不一样）。

配置：`alert_webhook_url`；未配置时回落到历史键 `llm_alert_webhook_url`——同一个出口的
两个名字（兼容既有部署），不是两条通道。两者都空 = 一条都不发（本地与离线测试的默认态）。

纪律（沿用 breaker 的口径，不另立）：
- 网络 I/O 永远在锁外；
- 冷却按"发起即占位"计，发送失败也占位——否则一个死掉的 webhook 会被每次事件重试刷屏；
- 任何失败只记一条 warning，异常文本经 `redact_secrets`；**绝不**向业务路径抛异常；
- detail 里的字符串递归脱敏，用户原话/凭据不得进载荷。
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

from app.common.config import settings
from app.common.external_client import redact_secrets
from app.common.http_client import api_client

logger = logging.getLogger(__name__)

#: 同一事件+同一作用域的默认冷却（秒）。磁盘/备份这类日级问题不需要每分钟重报。
COOLDOWN_SECONDS = 300.0
_SEND_TIMEOUT_SECONDS = 10.0

_cooldown: dict[str, float] = {}
_lock = threading.Lock()


def webhook() -> str:
    """实际出口地址；空串表示"没有配置告警目标"。"""
    configured = str(getattr(settings, "alert_webhook_url", "") or "").strip()
    return configured or str(settings.llm_alert_webhook_url or "").strip()


def _redact(value: Any) -> Any:
    if isinstance(value, str):
        return redact_secrets(value)
    if isinstance(value, dict):
        return {key: _redact(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact(item) for item in value]
    return value


def send(
    event: str,
    detail: dict[str, Any] | None = None,
    *,
    scope: str = "",
    cooldown_seconds: float = COOLDOWN_SECONDS,
) -> bool:
    """发一条告警。返回是否真的发出（未配置目标 / 仍在冷却 = False）。

    载荷键沿用既有的 `channel`（承载"这条告警属于哪个作用域"）：webhook 那侧可能按它过滤，
    把字段改名等于悄悄改掉对外契约——参数叫 scope，出网仍写 channel。

    调用方不需要 try/except：本函数吃掉所有发送异常，只留日志。告警是观测面，不能把
    被观测的那条路径带崩。
    """
    hook = webhook()
    if not hook:
        return False
    key = f"{event}|{scope}"
    now = time.monotonic()
    with _lock:
        if now - _cooldown.get(key, 0.0) < cooldown_seconds:
            return False
        _cooldown[key] = now
    payload = {"event": event, "channel": scope, "detail": _redact(detail or {}), "ts": int(time.time())}
    try:
        response = api_client().post(hook, json=payload, timeout=_SEND_TIMEOUT_SECONDS)
        if getattr(response, "status_code", 0) >= 400:
            logger.warning("alert %s (%s) webhook returned HTTP %s", event, scope or "-", response.status_code)
    except Exception as exc:
        logger.warning("alert %s (%s) send failed: %s", event, scope or "-", redact_secrets(exc))
    return True


def reset_state() -> None:
    """清冷却表（测试与 breaker 复位用）。"""
    with _lock:
        _cooldown.clear()
