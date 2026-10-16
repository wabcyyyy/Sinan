"""配置值原语（从 `app/common/config.py` 就地拆出，分工口径同 `config_rules`）。

这里只放**不依赖 Settings 实例**的形态判定与解析：回环地址集合、占位符密钥识别、
`TRUSTED_PROXIES` 的网段解析。启动期校验（`Settings.validate_boot`）与请求期归因
（`app.common.client_ip`）必须共用同一份——各写一份迟早漂移，结果是"配置里写错的
代理"在一边报错、另一边静默不信任。

env 键与默认值的权威定义仍然只有一处：`app/common/config.py`（INV-7，
`tests/test_env_example_alignment.py` 只认那一处）。
"""

from __future__ import annotations

import ipaddress

LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", "0:0:0:0:0:0:0:1"})

# 已知占位符前缀（小写匹配，审计 §3.4.2 / P0-5 应用侧）：.env.example 是公开仓库里的
# 文件，其中的示例串人尽皆知——JWT_SECRET 的 replace-with-…-at-least-32-chars 恰好
# 51 字符，能穿过下文 ≥32 的长度校验直上生产（误配 = 任何人可离线伪造含 admin 的
# 会话票）。deploy.sh 在部署侧拦一道，validate_boot 在应用侧再拦一道（两层口径见
# deploy/deploy.sh 的 reject_secret）。真实随机串（openssl rand -hex 32）不会命中
# 任何前缀；空串不在此判（走各自的既有校验）。
_SECRET_PLACEHOLDER_PREFIXES: tuple[str, ...] = ("replace-with", "your-", "changeme")


def secret_placeholder_mark(value: str) -> str | None:
    """返回命中的占位符前缀原样（如 ``your-``）；非占位串返回 None。"""
    text = value.strip().lower()
    for prefix in _SECRET_PLACEHOLDER_PREFIXES:
        if text.startswith(prefix):
            return prefix
    return None


def parse_trusted_proxy(item: str) -> ipaddress.IPv4Network | ipaddress.IPv6Network:
    """把 `TRUSTED_PROXIES` 的一项解析成网段：裸地址按单主机（/32 或 /128）。

    同一解析器供启动期校验与请求期归因（app.common.client_ip）共用——各写一份，
    "配置里写错的代理"会在一边报错、另一边静默不信任。
    """
    text = item.strip()
    if "/" not in text:
        text = f"{text}/32" if ":" not in text else f"{text}/128"
    return ipaddress.ip_network(text)


def proxy_ok(item: str) -> bool:
    try:
        parse_trusted_proxy(item)
    except ValueError:
        return False
    return True
