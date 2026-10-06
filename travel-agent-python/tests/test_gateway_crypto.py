"""BYOK 网关密钥静态加密（gateway_crypto）单测：roundtrip / hint / 坏密文 / 派生路径。

密钥材料全部用短占位串或运行时拼接——secret scan 的熵模式会匹配形如
`api_key = <32+ 字符字面量>` 的赋值，测试里的假密钥保持 <32 字符。
"""

from __future__ import annotations

import base64

import pytest

from app.common import gateway_crypto
from app.common.config import byok_key_material, settings

# 短占位密钥（<32 字符，不触发 secret scan 熵模式）
PLAIN_KEY = "sk-demo-key-42"

_JWT_FOR_TEST = "example-only-crypto-test-signing-material"


@pytest.fixture(autouse=True)
def _stable_crypto_env(monkeypatch):
    """钉住加密材料：不依赖本机 .env 的 JWT_SECRET/BYOK_ENC_KEY/AGENT_HOST，测试自洽。"""
    monkeypatch.setattr(settings, "byok_enc_key", "")
    monkeypatch.setattr(settings, "jwt_secret", _JWT_FOR_TEST)
    monkeypatch.setattr(settings, "agent_host", "127.0.0.1")
    yield


def test_roundtrip_and_cipher_differs_from_plaintext() -> None:
    cipher = gateway_crypto.encrypt_secret(PLAIN_KEY)
    assert cipher != PLAIN_KEY
    assert PLAIN_KEY not in cipher
    assert gateway_crypto.decrypt_secret(cipher) == PLAIN_KEY


def test_key_hint_is_last_four_chars() -> None:
    assert gateway_crypto.key_hint(PLAIN_KEY) == PLAIN_KEY[-4:]
    assert gateway_crypto.key_hint("") == ""


def test_bad_ciphertext_raises_value_error() -> None:
    with pytest.raises(ValueError):
        gateway_crypto.decrypt_secret("not-a-fernet-token")
    # 合法 base64 但不是 Fernet 令牌：同样 ValueError（不是底层 InvalidToken 外泄）
    with pytest.raises(ValueError):
        gateway_crypto.decrypt_secret(base64.urlsafe_b64encode(b"x" * 40).decode("ascii"))


def test_jwt_derived_key_is_reversible_and_rotation_breaks_it(monkeypatch) -> None:
    cipher = gateway_crypto.encrypt_secret(PLAIN_KEY)
    # 换一个 jwt_secret（模拟轮换）：旧密文应解不开——这是 .env.example 里
    # 「轮换 JWT_SECRET 会使存量密文失效」告警的行为证据。
    monkeypatch.setattr(settings, "jwt_secret", "another-example-secret-for-rotation!")
    with pytest.raises(ValueError):
        gateway_crypto.decrypt_secret(cipher)


def test_explicit_byok_key_roundtrip(monkeypatch) -> None:
    material = "k" * 32  # 运行时拼接，避免长字面量进 secret scan 面
    monkeypatch.setattr(settings, "byok_enc_key", base64.urlsafe_b64encode(material.encode()).decode())
    cipher = gateway_crypto.encrypt_secret(PLAIN_KEY)
    assert gateway_crypto.decrypt_secret(cipher) == PLAIN_KEY
    # 与 jwt 派生路径互不可解：显式 key 加密的密文在派生模式下解不开
    monkeypatch.setattr(settings, "byok_enc_key", "")
    with pytest.raises(ValueError):
        gateway_crypto.decrypt_secret(cipher)


def test_explicit_byok_key_wrong_length_raises(monkeypatch) -> None:
    monkeypatch.setattr(settings, "byok_enc_key", base64.urlsafe_b64encode(b"short").decode())
    with pytest.raises(ValueError, match="32 字节"):
        gateway_crypto.encrypt_secret(PLAIN_KEY)


def test_validate_boot_rejects_malformed_byok_key(monkeypatch) -> None:
    # 合法 b64 但解码后不足 32B：启动即拒
    monkeypatch.setattr(settings, "byok_enc_key", base64.urlsafe_b64encode(b"short").decode())
    with pytest.raises(RuntimeError, match="BYOK_ENC_KEY"):
        settings.validate_boot()
    # 非 base64 串：同样启动即拒
    monkeypatch.setattr(settings, "byok_enc_key", "!!not-base64!!")
    with pytest.raises(RuntimeError, match="BYOK_ENC_KEY"):
        settings.validate_boot()
    # 空 = 派生回落，不因本项拒绝（本条同时证明前面的拒绝不是别的检查在拦）
    monkeypatch.setattr(settings, "byok_enc_key", "")
    settings.validate_boot()


def test_byok_key_material_pads_missing_equals() -> None:
    raw = base64.urlsafe_b64encode(b"y" * 32).decode().rstrip("=")
    assert byok_key_material(raw) == b"y" * 32
