"""框架核心：配置、错误码、安全、加密、限流、分页。"""
from __future__ import annotations

import os
import time

import pytest

from luzback.config import DEV_SECRET, Settings, reload_settings
from luzback.crypto import FieldCipher
from luzback.errors import AppError, ErrorCode
from luzback.pagination import decode_cursor, encode_cursor, parse_limit, parse_page, parse_sort
from luzback.ratelimit import FixedWindowLimiter, TokenBucketLimiter
from luzback.security import (
    NonceStore, check_password_policy, create_token, decode_token, hash_password, mask_phone, password_needs_rehash,
    service_signature, totp_now, totp_secret, totp_verify, verify_password, verify_service_signature,
)
from luzback.testing import FakeClock


def test_prod_settings_reject_dev_defaults(monkeypatch):
    monkeypatch.setenv("LUZ_ENV", "prod")
    settings = reload_settings()
    with pytest.raises(RuntimeError) as exc:
        settings.assert_production_safe()
    message = str(exc.value)
    assert "LUZ_JWT_SECRET" in message and "sqlite" in message and "LUZ_REDIS_URL" in message
    monkeypatch.setenv("LUZ_ENV", "dev")
    reload_settings()


def test_dev_settings_pass_and_service_secret():
    settings = Settings()
    settings.assert_production_safe()
    assert settings.jwt_secret == DEV_SECRET
    assert settings.service_secret("agent") == "dev-agent-secret"
    assert settings.service_secret("unknown") is None
    assert settings.feature_enabled("anything")


def test_error_codes_map_to_http_and_retryable():
    assert AppError(ErrorCode.NOT_FOUND).status == 404
    assert AppError(ErrorCode.SERVICE_UNAVAILABLE).status == 503
    assert AppError(ErrorCode.RATE_LIMITED).retryable
    assert not AppError(ErrorCode.BUSINESS_RULE).retryable
    assert AppError(ErrorCode.QUOTE_EXPIRED).status == 422


def test_jwt_roundtrip_and_rejections():
    token = create_token("42", "secret-key-32-chars-long-xxxxxxxx", 60, extra={"role": "user"})
    payload = decode_token(token, "secret-key-32-chars-long-xxxxxxxx")
    assert payload["sub"] == "42" and payload["role"] == "user" and payload["jti"]
    with pytest.raises(AppError) as exc:
        decode_token(token, "other-secret")
    assert exc.value.code == ErrorCode.UNAUTHORIZED
    with pytest.raises(AppError):
        decode_token(token, "secret-key-32-chars-long-xxxxxxxx", kind="refresh")
    expired = create_token("42", "s", -100)
    with pytest.raises(AppError) as exc:
        decode_token(expired, "s")
    assert "expired" in exc.value.message
    with pytest.raises(AppError):
        decode_token("not.a.jwt", "s")


def test_password_hash_verify_policy_and_rehash():
    stored = hash_password("Passw0rd!2026")
    assert verify_password("Passw0rd!2026", stored)
    assert not verify_password("wrong", stored)
    assert not verify_password("x", "garbage")
    assert not password_needs_rehash(stored)
    assert password_needs_rehash("pbkdf2_sha256$1000$abc$def")
    with pytest.raises(AppError):
        check_password_policy("short1")
    with pytest.raises(AppError):
        check_password_policy("onlyletterslong")
    check_password_policy("letters123456")


def test_totp_accepts_drift_and_rejects_garbage():
    secret = totp_secret()
    now = 1_700_000_000.0
    code = totp_now(secret, at=now)
    assert totp_verify(secret, code, at=now)
    assert totp_verify(secret, code, at=now + 30)
    assert not totp_verify(secret, code, at=now + 90)
    assert not totp_verify(secret, "abc", at=now)


def test_service_signature_rejects_replay_skew_and_tamper():
    store = NonceStore()
    body = b'{"a":1}'
    ts = str(int(time.time()))
    sig = service_signature("s", "POST", "/api/v1/app/orders", ts, "n1", body)
    verify_service_signature("s", "POST", "/api/v1/app/orders", ts, "n1", body, sig, store)
    with pytest.raises(AppError):  # nonce 重放
        verify_service_signature("s", "POST", "/api/v1/app/orders", ts, "n1", body, sig, store)
    with pytest.raises(AppError):  # 篡改 body
        verify_service_signature("s", "POST", "/api/v1/app/orders", ts, "n2", b"{}", sig, store)
    old = str(int(time.time()) - 1000)
    old_sig = service_signature("s", "POST", "/p", old, "n3", body)
    with pytest.raises(AppError):
        verify_service_signature("s", "POST", "/p", old, "n3", body, old_sig, store)


def test_masks():
    assert mask_phone("13800001234") == "138****1234"
    assert mask_phone("123") == "***"
    assert mask_phone(None) is None


def test_field_cipher_dev_mode_roundtrip_and_hash():
    cipher = FieldCipher("")
    token = cipher.encrypt("13800001234")
    assert token.startswith("dev:") and cipher.decrypt(token) == "13800001234"
    assert cipher.lookup_hash("13800001234") == cipher.lookup_hash("13800001234")
    assert cipher.lookup_hash("a") != cipher.lookup_hash("b")
    with pytest.raises(ValueError):
        FieldCipher("c2hvcnQ=")


def test_token_bucket_and_fixed_window_with_fake_clock():
    clock = FakeClock(0)
    bucket = TokenBucketLimiter(rate=1, capacity=2, clock=clock)
    assert bucket.allow("k") and bucket.allow("k") and not bucket.allow("k")
    clock.advance(1)
    assert bucket.allow("k")
    window = FixedWindowLimiter(limit=2, window_seconds=60, clock=clock)
    assert window.allow("ip") and window.allow("ip") and not window.allow("ip")
    clock.advance(60)
    assert window.allow("ip")


def test_cursor_is_signed_and_sort_whitelisted():
    cursor = encode_cursor("2026-10-23", 99, "secret")
    assert decode_cursor(cursor, "secret") == ("2026-10-23", 99)
    with pytest.raises(AppError):
        decode_cursor(cursor + "x", "secret")
    with pytest.raises(AppError):
        decode_cursor(cursor, "other")
    assert decode_cursor(None, "s") is None
    assert parse_limit(None) == 10 and parse_limit(500) == 20
    with pytest.raises(AppError):
        parse_limit(0)
    assert parse_page(None, None) == (1, 20) and parse_page(2, 1000) == (2, 100)
    assert parse_sort("-created_at,amount", {"created_at", "amount"}) == [("created_at", True), ("amount", False), ("id", True)]
    with pytest.raises(AppError):
        parse_sort("password", {"created_at"})
