"""validate_boot 的占位密钥拒绝（审计 §3.4.2 / P0-5 应用侧）。

.env.example 是公开仓库文件，其中的示例串（replace-with-… / your-… / changeme）
人尽皆知：JWT_SECRET 的占位串恰好 51 字符，能穿过 ≥32 的长度校验直上生产——
误配即任何人可离线伪造含 admin 的会话票。部署侧已由 deploy/deploy.sh 的
reject_secret 拦一道，这里钉住应用侧的 validate_boot 同样拒绝（两层防呆）。

密钥字面量全部用运行时拼接或 example 标记，避免被 secret scan 误报。
"""

from __future__ import annotations

import pytest

from app.common.config import _secret_placeholder_mark, settings

# 合法形态（≥32 字符的真实随机串）：本地开发不受影响
_REAL_JWT = "".join(["a2m3i8ku", "xjnq4fvs", "0dlq7wye", "9tzu3hbr", "6mce"])  # 运行时拼接


@pytest.fixture(autouse=True)
def _stable_boot_env(monkeypatch):
    """钉住启动语境：回环绑定 + 合法 JWT + 安全 Cookie，占位符用例只测自己改的那个键。"""
    monkeypatch.setattr(settings, "jwt_secret", _REAL_JWT)
    monkeypatch.setattr(settings, "agent_host", "127.0.0.1")
    monkeypatch.setattr(settings, "auth_cookie_secure", True)
    monkeypatch.setattr(settings, "byok_enc_key", "")
    yield


@pytest.mark.parametrize(
    "value",
    [
        "replace-with-a-long-random-secret-at-least-32-chars",  # .env.example:198 原串（51 字符，恰穿过长度校验）
        "replace-with-a-long-random-token",  # .env.example:45 AGENT_INTERNAL_TOKEN 占位串
        "Replace-With-Mixed-Case",  # 大小写不敏感
        "  replace-with-leading-space  ",  # 首尾空白不救场
        "your-llm-api-key",  # your- 前缀（LLM_API_KEY 占位串族）
        "your-jwt-secret-here-please-rotate-me-now",
        "changeme",
        "ChangeMe-32-chars-aaaaaaaaaaaaaaaaa",
    ],
)
def test_validate_boot_rejects_known_placeholder_secrets(monkeypatch, value) -> None:
    monkeypatch.setattr(settings, "jwt_secret", value)
    with pytest.raises(RuntimeError, match=r"JWT_SECRET.*占位符"):
        settings.validate_boot()
    monkeypatch.setattr(settings, "jwt_secret", _REAL_JWT)
    monkeypatch.setattr(settings, "agent_internal_token", value)
    with pytest.raises(RuntimeError, match=r"AGENT_INTERNAL_TOKEN.*占位符"):
        settings.validate_boot()


def test_placeholder_jwt_is_rejected_before_length_check_passes() -> None:
    """占位串 51 字符 > 32：若没有占位符判断，长度校验会放行——本用例钉住拦截者是谁。"""
    placeholder = "replace-with-a-long-random-secret-at-least-32-chars"
    assert len(placeholder) >= 32
    assert _secret_placeholder_mark(placeholder) == "replace-with"
    assert _secret_placeholder_mark("") is None  # 空串不在此判（走各自的既有校验）
    assert _secret_placeholder_mark(_REAL_JWT) is None


def test_real_random_secret_still_boots() -> None:
    """既有长度/组合校验不变：真实随机串 + 回环绑定照常启动（含空 AGENT_INTERNAL_TOKEN）。"""
    settings.validate_boot()
