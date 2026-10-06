"""JWT 签发/校验（纯标准库），与 Java 侧 jjwt 0.12.5 逐字段互认。

迁移双跑期（PLAN v3.0 §1.1）要求 Java 与 Python 认同一张票：同一 JWT_SECRET、
同一 Cookie（TA_AUTH）、claims 语义为 sub=用户名 + jti + iat + exp。

为什么不引 PyJWT/jose：
- 需要显式允许 HMAC 族并按 header 声明的 alg 取摘要——jjwt 的 signWith(SecretKey)
  **按密钥字节长度自动选** HS256/384/512（≥512bit→HS512，≥384→HS384，≥256→HS256），
  不是固定 HS256。写死 HS256 会在长密钥下把所有 Java 签发的票判为验签失败。
- 依赖越少，跨语言兼容面越小。

安全边界：只接受 HS256/384/512；alg=none 与任何非 HMAC 算法一律拒绝（防算法混淆降级）。
"""

from __future__ import annotations

import base64
import hmac
import json
import time
import uuid
from typing import Any

# 摘要名与 jjwt 的密钥长度选择规则保持一致
_HMAC_ALGS: dict[str, str] = {"HS256": "sha256", "HS384": "sha384", "HS512": "sha512"}


class JwtError(Exception):
    """验签失败、结构非法或声明过期。调用方按「未认证」处理，不区分具体原因。"""


def _b64url_decode(segment: str) -> bytes:
    padding = "=" * (-len(segment) % 4)
    try:
        return base64.urlsafe_b64decode(segment + padding)
    except Exception as exc:  # binascii.Error 等
        raise JwtError(f"base64url 段不可解析: {exc}") from exc


def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def select_alg(secret: str) -> str:
    """镜像 jjwt signWith(SecretKey) 的算法选择：按 UTF-8 字节长度定档。"""
    bits = len(secret.encode("utf-8")) * 8
    if bits >= 512:
        return "HS512"
    if bits >= 384:
        return "HS384"
    if bits >= 256:
        return "HS256"
    raise JwtError(f"JWT 密钥过弱（{bits} bit < 256），与 Java 侧 enforce-secret-check 同口径拒绝")


def encode_token(
    subject: str,
    secret: str,
    ttl_seconds: int,
    *,
    jti: str | None = None,
    alg: str | None = None,
) -> str:
    """签发与 Java 结构一致的票：jti/sub/iat/exp，HMAC 族。"""
    algorithm = alg or select_alg(secret)
    if algorithm not in _HMAC_ALGS:
        raise JwtError(f"不支持的算法 {algorithm}")
    now = int(time.time())
    header = {"alg": algorithm, "typ": "JWT"}
    payload = {"jti": jti or str(uuid.uuid4()), "sub": subject, "iat": now, "exp": now + int(ttl_seconds)}
    signing_input = ".".join(
        (
            _b64url_encode(json.dumps(header, separators=(",", ":"), sort_keys=True).encode()),
            _b64url_encode(json.dumps(payload, separators=(",", ":")).encode()),
        )
    ).encode("ascii")
    signature = hmac.new(secret.encode("utf-8"), signing_input, _HMAC_ALGS[algorithm]).digest()
    return f"{signing_input.decode('ascii')}.{_b64url_encode(signature)}"


def decode_token(token: str, secret: str, *, leeway_seconds: int = 5) -> dict[str, Any]:
    """验签并返回 claims；任何异常一律抛 JwtError（调用方转 401）。"""
    if not token or token.count(".") != 2:
        raise JwtError("token 结构非法")
    header_b64, payload_b64, signature_b64 = token.split(".")
    try:
        header = json.loads(_b64url_decode(header_b64))
    except (JwtError, ValueError) as exc:
        raise JwtError(f"header 不可解析: {exc}") from exc

    alg = header.get("alg") if isinstance(header, dict) else None
    if alg not in _HMAC_ALGS:
        # 覆盖 alg=none / RS* 混淆攻击：非 HMAC 族直接拒绝，不看签名
        raise JwtError(f"算法不被允许: {alg!r}")

    signing_input = f"{header_b64}.{payload_b64}".encode()
    expected = hmac.new(secret.encode("utf-8"), signing_input, _HMAC_ALGS[alg]).digest()
    try:
        provided = _b64url_decode(signature_b64)
    except JwtError as exc:
        raise JwtError(f"签名段不可解析: {exc}") from exc
    if not hmac.compare_digest(expected, provided):
        raise JwtError("验签失败")

    try:
        claims = json.loads(_b64url_decode(payload_b64))
    except (JwtError, ValueError) as exc:
        raise JwtError(f"payload 不可解析: {exc}") from exc
    if not isinstance(claims, dict):
        raise JwtError("claims 必须是对象")

    now = time.time()
    exp = claims.get("exp")
    if exp is not None and now > float(exp) + leeway_seconds:
        raise JwtError("token 已过期")
    nbf = claims.get("nbf")
    if nbf is not None and now + leeway_seconds < float(nbf):
        raise JwtError("token 尚未生效")
    return claims


def remaining_seconds(claims: dict[str, Any]) -> int:
    """剩余有效秒数（用于吊销黑名单 TTL）；已过期返回 0。向上取整，对齐 Java 侧。"""
    exp = claims.get("exp")
    if exp is None:
        return 0
    remaining = float(exp) - time.time()
    return int(remaining) + 1 if remaining > 0 else 0
