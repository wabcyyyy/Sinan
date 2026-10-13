"""会话 JWT 签发/校验（PyJWT）。

历史：最初为与已退役的 Java 侧（jjwt 0.12.5）互认而用纯标准库手写 HMAC/base64url。
Java 退役后那条理由不再成立，2026-10-12 换成 PyJWT：签名、base64url、时间声明的
校验交给维护中的库，本模块只保留业务约束：

- 算法按密钥字节长度定档（≥512bit→HS512，≥384→HS384，≥256→HS256，更短拒绝）。
  沿用旧实现的定档规则，升级前签发的在线会话不会因换库全部失效；
- 解码只接受「当前密钥定档出的那一个」HMAC 算法：alg=none、RS*/ES* 算法混淆，
  以及同一密钥下的降档签名一律拒绝（比旧实现「HMAC 族任一」更严）；
- claims 必须带 exp/sub/jti（旧实现缺 exp 也放行；登出吊销按 jti 与剩余秒数工作）。
"""

from __future__ import annotations

import time
import uuid
from typing import Any

import jwt

_HMAC_BY_MIN_BITS: tuple[tuple[int, str], ...] = ((512, "HS512"), (384, "HS384"), (256, "HS256"))
_REQUIRED_CLAIMS = ["exp", "sub", "jti"]


class JwtError(Exception):
    """验签失败、结构非法或声明过期。调用方按「未认证」处理，不区分具体原因。"""


def select_alg(secret: str) -> str:
    """按密钥 UTF-8 字节长度定档 HMAC 算法；短于 256 bit 直接拒绝。"""
    bits = len(secret.encode("utf-8")) * 8
    for min_bits, alg in _HMAC_BY_MIN_BITS:
        if bits >= min_bits:
            return alg
    raise JwtError(f"JWT 密钥过弱（{bits} bit < 256），与启动期密钥校验同口径拒绝")


def encode_token(subject: str, secret: str, ttl_seconds: int, *, jti: str | None = None) -> str:
    """签发会话票：jti/sub/iat/exp，算法按密钥长度定档。"""
    now = int(time.time())
    payload = {"jti": jti or str(uuid.uuid4()), "sub": subject, "iat": now, "exp": now + int(ttl_seconds)}
    return jwt.encode(payload, secret, algorithm=select_alg(secret))


def decode_token(token: str, secret: str, *, leeway_seconds: int = 5) -> dict[str, Any]:
    """验签并返回 claims；任何失败一律抛 JwtError（调用方转 401）。"""
    try:
        claims = jwt.decode(
            token,
            secret,
            algorithms=[select_alg(secret)],
            leeway=leeway_seconds,
            options={"require": _REQUIRED_CLAIMS},
        )
    except jwt.PyJWTError as exc:
        raise JwtError(str(exc)) from exc
    return claims


def remaining_seconds(claims: dict[str, Any]) -> int:
    """剩余有效秒数（用于吊销黑名单 TTL）；已过期返回 0，向上取整。"""
    exp = claims.get("exp")
    if exp is None:
        return 0
    remaining = float(exp) - time.time()
    return int(remaining) + 1 if remaining > 0 else 0
