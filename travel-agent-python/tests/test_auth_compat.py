"""鉴权互认单测（离线，不连 MySQL / Redis）。

覆盖 PLAN v3.0 §1.1 的四个易错点：
1. 算法按密钥字节长度定档（jjwt signWith 行为），写死 HS256 会在长密钥下全判失败；
2. 只接受 HMAC 族，alg=none / RS256 必须拒；
3. 黑名单 key 与 Java 逐字一致（auth:jwt:revoked: + sha256(整串 token) 小写十六进制）；
4. Redis 故障降级进程内表时语义不变（否则一边登出另一边仍有效）。
"""

from __future__ import annotations

import base64
import hashlib
import json
import time

import pytest

from app.common import token_revocation
from app.common.jwt_compat import (
    JwtError,
    decode_token,
    encode_token,
    remaining_seconds,
    select_alg,
)

SECRET_32 = "0123456789abcdef0123456789abcdef"  # 256 bit → HS256
SECRET_48 = "0" * 48
SECRET_64 = "0" * 64


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


# ---------- 1. 算法定档 ----------


@pytest.mark.parametrize(
    "secret,expected",
    [(SECRET_32, "HS256"), (SECRET_48, "HS384"), (SECRET_64, "HS512")],
)
def test_select_alg_mirrors_jjwt_key_length_rule(secret, expected):
    assert select_alg(secret) == expected


def test_weak_secret_rejected_like_java_startup_validator():
    with pytest.raises(JwtError):
        select_alg("short")


def test_token_header_alg_follows_secret_length():
    for secret, expected in ((SECRET_32, "HS256"), (SECRET_64, "HS512")):
        header_b64 = encode_token("alice", secret, 60).split(".")[0]
        header = json.loads(base64.urlsafe_b64decode(header_b64 + "=="))
        assert header["alg"] == expected


# ---------- 2. 验签与算法混淆 ----------


def test_roundtrip_claims_match_java_shape():
    token = encode_token("alice", SECRET_32, 3600, jti="fixed-jti")
    claims = decode_token(token, SECRET_32)
    assert set(claims) == {"jti", "sub", "iat", "exp"}
    assert claims["sub"] == "alice" and claims["jti"] == "fixed-jti"


def test_tampered_payload_fails_verification():
    token = encode_token("alice", SECRET_32, 3600)
    header, payload, signature = token.split(".")
    forged = json.loads(base64.urlsafe_b64decode(payload + "=="))
    forged["sub"] = "admin"
    tampered = f"{header}.{_b64(json.dumps(forged, separators=(',', ':')).encode())}.{signature}"
    with pytest.raises(JwtError):
        decode_token(tampered, SECRET_32)


@pytest.mark.parametrize("alg", ["none", "None", "RS256", "ES256"])
def test_non_hmac_algorithms_rejected(alg):
    header = _b64(json.dumps({"alg": alg, "typ": "JWT"}).encode())
    payload = _b64(json.dumps({"sub": "alice", "exp": int(time.time()) + 300}).encode())
    with pytest.raises(JwtError):
        decode_token(f"{header}.{payload}.ZmFrZXNpZw", SECRET_32)


@pytest.mark.parametrize("broken", ["", "a.b", "a.b.c.d", "!!!.###.$$$"])
def test_malformed_token_rejected(broken):
    with pytest.raises(JwtError):
        decode_token(broken, SECRET_32)


def test_header_json_formatting_is_irrelevant_to_verification():
    """jjwt 写出的 header 与本模块的序列化细节不必一致：验签作用在原始段上。"""
    claims_json = json.dumps(
        {"jti": "j", "sub": "alice", "iat": int(time.time()), "exp": int(time.time()) + 300},
        separators=(",", ":"),
    )
    header = _b64(b'{  "typ" : "JWT" ,  "alg" : "HS256"  }')  # 故意用怪异空格
    payload = _b64(claims_json.encode())
    signing_input = f"{header}.{payload}".encode()
    import hmac

    signature = _b64(hmac.new(SECRET_32.encode(), signing_input, hashlib.sha256).digest())
    assert decode_token(f"{header}.{payload}.{signature}", SECRET_32)["sub"] == "alice"


def test_expired_rejected_and_leeway_tolerates_clock_skew():
    token = encode_token("alice", SECRET_32, -60)  # 已过期 60s
    with pytest.raises(JwtError):
        decode_token(token, SECRET_32)
    barely = encode_token("alice", SECRET_32, -2)
    assert decode_token(barely, SECRET_32, leeway_seconds=5)["sub"] == "alice"


def test_remaining_seconds_ceil_and_zero_on_expired():
    assert remaining_seconds({"exp": time.time() + 10.2}) == 11
    assert remaining_seconds({"exp": time.time() - 5}) == 0
    assert remaining_seconds({}) == 0


# ---------- 3/4. 黑名单 key 与降级 ----------


class FakeRedis:
    def __init__(self, fail: bool = False):
        self.store: dict[str, str] = {}
        self.fail = fail

    def set(self, key, value, ex=None):
        if self.fail:
            raise RuntimeError("redis down")
        self.store[key] = value
        return True

    def exists(self, key):
        if self.fail:
            raise RuntimeError("redis down")
        return 1 if key in self.store else 0


@pytest.fixture(autouse=True)
def isolated_blacklist(monkeypatch):
    token_revocation.reset_for_tests()
    monkeypatch.setattr(token_revocation.settings, "jwt_revocation_prefer_redis", True)
    yield
    token_revocation.reset_for_tests()


def test_blacklist_key_matches_java_hash_scheme(monkeypatch):
    fake = FakeRedis()
    monkeypatch.setattr(token_revocation, "_get_client", lambda: fake)
    token = encode_token("alice", SECRET_32, 3600)
    token_revocation.revoke(token, 3600)
    expected = "auth:jwt:revoked:" + hashlib.sha256(token.encode("utf-8")).hexdigest()
    assert list(fake.store) == [expected]
    assert fake.store[expected] == "1"
    assert token_revocation.is_revoked(token) is True


def test_redis_outage_degrades_to_local_but_still_revokes(monkeypatch):
    monkeypatch.setattr(token_revocation, "_get_client", lambda: FakeRedis(fail=True))
    token = encode_token("alice", SECRET_32, 3600)
    assert token_revocation.is_revoked(token) is False
    token_revocation.revoke(token, 3600)
    assert token_revocation.is_revoked(token) is True


def test_expired_ttl_entries_are_pruned_from_local_fallback(monkeypatch):
    monkeypatch.setattr(token_revocation, "_get_client", lambda: FakeRedis(fail=True))
    token = encode_token("alice", SECRET_32, 3600)
    token_revocation._local_blacklist[token_revocation.token_hash(token)] = int(time.time()) - 1
    assert token_revocation.is_revoked(token) is False
    assert token_revocation._local_blacklist == {}


def test_blank_inputs_are_no_ops():
    assert token_revocation.is_revoked("") is False
    token_revocation.revoke("", 100)
    token_revocation.revoke("x", 0)


# ---------- 依赖层：取票顺序与用户映射 ----------


def test_extract_token_prefers_bearer_over_cookie():
    from app.api.deps import extract_token

    token, source = extract_token("Bearer  abc ", {"TA_AUTH": "cookie-val"})
    assert (token, source) == ("abc", "bearer")
    token, source = extract_token("Bearer ", {"TA_AUTH": "cookie-val"})  # 空 Bearer 回落 Cookie
    assert (token, source) == ("cookie-val", "cookie")
    assert extract_token(None, {}) == (None, None)


def _authenticate_with(monkeypatch, user_row):
    from app.api import deps

    monkeypatch.setattr(deps.settings, "jwt_secret", SECRET_32)
    monkeypatch.setattr(deps.token_revocation, "is_revoked", lambda _t: False)
    monkeypatch.setattr(deps.user_repository, "find_by_username", lambda _u: user_row)
    return deps.authenticate(encode_token("alice", SECRET_32, 3600))


def test_authenticate_maps_admin_role_and_accepts_null_status(monkeypatch):
    user = _authenticate_with(monkeypatch, {"id": 7, "username": "alice", "role": "admin", "status": None})
    assert user is not None and user.id == 7 and user.is_admin is True


def test_authenticate_rejects_disabled_and_missing_user(monkeypatch):
    assert _authenticate_with(monkeypatch, {"id": 7, "role": "user", "status": 0}) is None
    assert _authenticate_with(monkeypatch, None) is None


def test_authenticate_rejects_revoked_token(monkeypatch):
    from app.api import deps

    monkeypatch.setattr(deps.settings, "jwt_secret", SECRET_32)
    monkeypatch.setattr(deps.token_revocation, "is_revoked", lambda _t: True)
    monkeypatch.setattr(deps.user_repository, "find_by_username", lambda _u: {"id": 1, "role": "user"})
    assert deps.authenticate(encode_token("alice", SECRET_32, 3600)) is None


def test_authenticate_rejects_wrong_secret(monkeypatch):
    from app.api import deps

    monkeypatch.setattr(deps.settings, "jwt_secret", "another-secret-that-is-long-enough-32b")
    monkeypatch.setattr(deps.token_revocation, "is_revoked", lambda _t: False)
    monkeypatch.setattr(deps.user_repository, "find_by_username", lambda _u: {"id": 1, "role": "user"})
    assert deps.authenticate(encode_token("alice", SECRET_32, 3600)) is None
