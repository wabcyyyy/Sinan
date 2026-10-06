"""BYOK 网关密钥的静态加密（Fernet，V11）。

为什么必须静态加密而不是明文落库：api_key 是**可直接烧钱**的凭据，库文件/备份/
误投屏的 DB 客户端都会让明文外流；Fernet 提供认证加密（篡改与错 key 都会显式
失败），cryptography 是全平台纯 wheel，无原生编译链风险。

密钥派生（与角色键留空回落 main 的既有模式同构）：
- BYOK_ENC_KEY 非空：须是「解码后恰 32 字节」的 urlsafe base64 串（启动期
  validate_boot 校验，见 config.py）；
- 留空：从 JWT_SECRET 做域分隔派生 sha256("byok:" + secret)——默认部署零配置
  可用；代价是轮换 JWT_SECRET 会使存量密文失效，用户需在设置页重录（文档口径）。

依赖：app.common.config.settings；cryptography（自带头文件，无内部依赖）。
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import logging

from cryptography.fernet import Fernet, InvalidToken

from app.common.config import byok_key_material, settings

logger = logging.getLogger(__name__)

# 域分隔前缀：同一 JWT_SECRET 还签会话票，前缀保证两把派生 key 互不可冒用。
_DERIVATION_DOMAIN = b"byok:"


def _fernet() -> Fernet:
    """当前生效的 Fernet 实例：显式 key 优先，空则从 jwt_secret 派生。

    每次调用现读 settings：测试 monkeypatch 与运维轮换都不需要重启语义之外的口子。
    """
    if settings.byok_enc_key.strip():
        material = byok_key_material(settings.byok_enc_key.strip())
        if len(material) != 32:
            raise ValueError("BYOK_ENC_KEY 须为解码后恰 32 字节的 urlsafe base64 串")
    else:
        material = hashlib.sha256(_DERIVATION_DOMAIN + settings.jwt_secret.encode("utf-8")).digest()
    return Fernet(base64.urlsafe_b64encode(material))


def encrypt_secret(plaintext: str) -> str:
    """明文密钥 -> Fernet 密文（str）。密文只进 user_llm_gateway.api_key_cipher，不进日志。"""
    return _fernet().encrypt(plaintext.encode("utf-8")).decode("ascii")


def decrypt_secret(cipher: str) -> str:
    """Fernet 密文 -> 明文密钥。坏密文/错 key 抛 ValueError（上层按 fail-open 处理）。"""
    try:
        return _fernet().decrypt(cipher.encode("ascii")).decode("utf-8")
    except (InvalidToken, binascii.Error, UnicodeError) as exc:
        # 只描述"解不开"这一事实，不携带密文片段——密文虽不可逆，但避免误带出关联信息。
        raise ValueError("网关密钥密文无法解密（加密 key 已轮换或数据损坏）") from exc


def key_hint(plaintext: str) -> str:
    """脱敏提示：尾 4 位（前端展示形如 sk-***abcd）。空串安全返回空。"""
    return plaintext[-4:] if plaintext else ""
