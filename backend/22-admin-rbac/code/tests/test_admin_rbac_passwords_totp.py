"""口令哈希参数、口令策略、TOTP（RFC 6238 附录 B 测试向量）、恢复码与纯规则。"""
from __future__ import annotations

import base64
import re
from datetime import datetime, timedelta, timezone

import pytest

from admin_rbac import config
from admin_rbac.passwords import (
    Argon2Params, check_policy, dummy_verify, hash_secret, needs_rehash, parse_phc, verify_secret,
)
from admin_rbac.rules import is_fresh, lock_seconds, session_expiry_reason, validate_phone, validate_username
from admin_rbac.totp import (
    decode_secret, hotp, match_step, new_recovery_codes, new_secret, normalize_recovery_code, otpauth_uri, totp_at,
)
from luzback import AppError, ErrorCode
from luzback.security import hash_password as framework_hash

from admin_rbac_testkit import reset_hooks


@pytest.fixture(autouse=True)
def _hooks():
    reset_hooks()
    yield
    reset_hooks()


# ---------- 哈希 ----------

def test_hash_is_argon2id_phc_with_owasp_minimum_params():
    stored = hash_secret("a long passphrase here")
    assert stored.startswith("$argon2id$v=19$m=19456,t=2,p=1$")
    params, salt, digest = parse_phc(stored)
    assert params == Argon2Params(19456, 2, 1)
    assert len(salt) == 16 and len(digest) == 32
    assert verify_secret("a long passphrase here", stored)
    assert not verify_secret("a long passphrase herE", stored)


def test_hash_salted_unique():
    assert hash_secret("same password value") != hash_secret("same password value")


def test_unicode_nfc_normalized_and_not_truncated():
    composed = "café-password-long"
    decomposed = "café-password-long"
    stored = hash_secret(composed)
    assert verify_secret(decomposed, stored)
    long_pw = "x" * 100 + "tail-A"
    stored_long = hash_secret(long_pw)
    assert not verify_secret("x" * 100 + "tail-B", stored_long)  # 不截断


def test_oversized_input_rejected_without_hashing():
    assert not verify_secret("a" * 2000, hash_secret("short-but-fine-pw"))


def test_needs_rehash_for_weaker_or_legacy():
    assert not needs_rehash(hash_secret("passphrase-value-1"))
    weak = hash_secret("passphrase-value-1", Argon2Params(9216, 1, 1))
    assert needs_rehash(weak)
    legacy = framework_hash("Legacy-password-123")
    assert needs_rehash(legacy)
    assert verify_secret("Legacy-password-123", legacy)  # 框架旧格式仍可校验


def test_configurable_params_raise_cost():
    config.set_config(argon2_time_cost=3)
    assert needs_rehash(hash_secret("x-passphrase", Argon2Params(19456, 2, 1)))
    assert hash_secret("x-passphrase").startswith("$argon2id$v=19$m=19456,t=3,p=1$")


def test_garbage_hash_is_false_not_error():
    assert not verify_secret("anything", "$argon2id$v=19$broken")
    assert not verify_secret("anything", None)
    dummy_verify("anything")  # 不抛异常


# ---------- 策略 ----------

def _bad(fn) -> AppError:
    with pytest.raises(AppError) as exc:
        fn()
    assert exc.value.code == ErrorCode.BAD_REQUEST
    return exc.value


def test_min_length_depends_on_mfa():
    assert check_policy("gr8-tune", mfa=True) == "gr8-tune"
    _bad(lambda: check_policy("gr8-tun", mfa=True))
    _bad(lambda: check_policy("fourteen-chars", mfa=False))
    assert check_policy("fifteen--chars!", mfa=False)


def test_no_composition_rules_lowercase_passphrase_ok():
    assert check_policy("purple monkey dishwasher", mfa=False)


def test_max_length_at_least_64():
    assert check_policy(("river-stone-cloud-" * 4)[:64], mfa=False)  # 64 位
    _bad(lambda: check_policy("ab" * 65, mfa=False))       # 130 > 128


def test_common_and_repetitive_blocked():
    _bad(lambda: check_policy("password123", mfa=True))
    _bad(lambda: check_policy("QWERTYUIOPASDFGH", mfa=False))
    _bad(lambda: check_policy("abababababababab", mfa=False))


def test_context_words_blocked():
    err = _bad(lambda: check_policy("zhangsan-is-great-2026", mfa=False, context=("zhangsan",), field_name="new_password"))
    assert err.data["errors"][0]["field"] == "new_password"
    config.set_config(password_blocklist_extra=("xiangcai",))
    _bad(lambda: check_policy("i-love-xiangcai-food", mfa=False))


def test_breach_checker_hook():
    config.set_breach_checker(lambda pw: pw == "breached passphrase")
    _bad(lambda: check_policy("breached passphrase", mfa=False))
    assert check_policy("unbreached passphrase", mfa=False)


def test_breach_checker_failure_fails_open_by_default_closed_when_configured():
    def boom(_):
        raise TimeoutError("hibp down")

    config.set_breach_checker(boom)
    assert check_policy("unbreached passphrase", mfa=False)
    config.set_config(password_breach_fail_closed=True)
    with pytest.raises(AppError) as exc:
        check_policy("unbreached passphrase", mfa=False)
    assert exc.value.code == ErrorCode.UPSTREAM_FAILED


# ---------- TOTP：RFC 6238 附录 B ----------

SEED_SHA1 = b"12345678901234567890"
SEED_SHA256 = b"12345678901234567890123456789012"
SEED_SHA512 = b"1234567890123456789012345678901234567890123456789012345678901234"

RFC6238_VECTORS = [
    (59, "94287082", "46119246", "90693936"),
    (1111111109, "07081804", "68084774", "25091201"),
    (1111111111, "14050471", "67062674", "99943326"),
    (1234567890, "89005924", "91819424", "93441116"),
    (2000000000, "69279037", "90698825", "38618901"),
    (20000000000, "65353130", "77737706", "47863826"),
]


@pytest.mark.parametrize("ts,sha1,sha256,sha512", RFC6238_VECTORS)
def test_rfc6238_appendix_b_vectors(ts, sha1, sha256, sha512):
    assert totp_at(SEED_SHA1, ts, digits=8, algorithm="SHA1") == sha1
    assert totp_at(SEED_SHA256, ts, digits=8, algorithm="SHA256") == sha256
    assert totp_at(SEED_SHA512, ts, digits=8, algorithm="SHA512") == sha512


def test_rfc4226_hotp_vectors():
    expected = ["755224", "287082", "359152", "969429", "338314", "254676", "287922", "162583", "399871", "520489"]
    assert [hotp(SEED_SHA1, i) for i in range(10)] == expected


def test_secret_is_160_bit_base32():
    secret = new_secret()
    assert len(decode_secret(secret)) == 20
    assert re.fullmatch(r"[A-Z2-7]{32}", secret)


def test_drift_one_step_accepted_two_rejected():
    secret = base64.b32encode(SEED_SHA1).decode()
    now = 1_700_000_000.0
    step = int(now // 30)
    key = decode_secret(secret)
    assert match_step(secret, totp_at(key, now), now) == step
    assert match_step(secret, totp_at(key, now - 30), now) == step - 1
    assert match_step(secret, totp_at(key, now + 30), now) == step + 1
    assert match_step(secret, totp_at(key, now - 60), now) is None
    assert match_step(secret, totp_at(key, now + 60), now) is None


def test_replay_rejected_by_last_step():
    secret = base64.b32encode(SEED_SHA1).decode()
    now = 1_700_000_000.0
    code = totp_at(decode_secret(secret), now)
    step = match_step(secret, code, now)
    assert match_step(secret, code, now, last_step=step) is None
    assert match_step(secret, code, now, last_step=step - 1) == step
    # 已用过当前步后，上一步的码也不再接受
    prev = totp_at(decode_secret(secret), now - 30)
    assert match_step(secret, prev, now, last_step=step) is None


def test_bad_code_shapes():
    secret = new_secret()
    assert match_step(secret, "12345", 0) is None
    assert match_step(secret, "abcdef", 0) is None
    assert match_step(secret, "", 0) is None


def test_otpauth_uri():
    uri = otpauth_uri("JBSWY3DPEHPK3PXP", "zhang.san", "Luz Store")
    assert uri.startswith("otpauth://totp/Luz%20Store:zhang.san?")
    for part in ("secret=JBSWY3DPEHPK3PXP", "issuer=Luz+Store", "algorithm=SHA1", "digits=6", "period=30"):
        assert part in uri


def test_recovery_codes_format_and_uniqueness():
    codes = new_recovery_codes(10)
    assert len(codes) == len(set(codes)) == 10
    for c in codes:
        assert re.fullmatch(r"[A-HJ-KM-NP-Z2-9]{5}-[A-HJ-KM-NP-Z2-9]{5}", c)
    assert normalize_recovery_code(codes[0].lower().replace("-", " ")) == codes[0]


# ---------- 纯规则 ----------

def test_lock_seconds_progressive_and_capped():
    assert [lock_seconds(n, 900, 86400) for n in (1, 2, 3, 4, 5, 6, 7, 8)] == [900, 1800, 3600, 7200, 14400, 28800, 57600, 86400]
    assert lock_seconds(99, 900, 86400) == 86400


def test_session_expiry_and_fresh():
    t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
    exp = t0 + timedelta(hours=8)
    assert session_expiry_reason(t0 + timedelta(minutes=14), t0, exp, 900) is None
    assert session_expiry_reason(t0 + timedelta(minutes=15), t0, exp, 900) == "idle_timeout"
    assert session_expiry_reason(exp, exp - timedelta(minutes=1), exp, 900) == "absolute_timeout"
    assert is_fresh(t0 + timedelta(seconds=300), t0, 300)
    assert not is_fresh(t0 + timedelta(seconds=301), t0, 300)
    assert not is_fresh(t0, None, 300)


def test_username_and_phone_validation():
    assert validate_username("  Zhang.San ") == "zhang.san"
    for bad in ("ab", "admin", "root", "has space", "x" * 33):
        with pytest.raises(AppError):
            validate_username(bad)
    assert validate_phone("13800138000") == "13800138000"
    assert validate_phone("") is None
    with pytest.raises(AppError):
        validate_phone("12345")
