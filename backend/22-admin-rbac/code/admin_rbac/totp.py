"""TOTP（RFC 6238）与恢复码。纯函数，便于用 RFC 附录 B 测试向量单测。

- HOTP（RFC 4226 §5.3）：HMAC → 动态截断 → mod 10^digits。
- TOTP：T = floor((now - T0) / X)，T0 = 0，X = 30 秒；默认 SHA1、6 位（主流验证器 App 兼容）。
- 校验允许前后 drift 个时间步（RFC 6238 §5.2 建议最多 1 步），返回匹配到的时间步；
  调用方保存最后一次成功的时间步，只接受更大的时间步（防重放：同一 OTP 不得二次接受）。
- 密钥 160 bit（RFC 4226 R6 推荐，与 HMAC-SHA1 输出等长），CSPRNG 生成，Base32 编码。
- 恢复码：10 个，每个 10 位（去掉易混字符的 Base32 字母表，约 50 bit），Argon2id 加盐哈希存储（ASVS 6.5.2）。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import struct
from urllib.parse import quote, urlencode

ALGORITHMS = {"SHA1": hashlib.sha1, "SHA256": hashlib.sha256, "SHA512": hashlib.sha512}
SECRET_BYTES = 20
STEP_SECONDS = 30
DIGITS = 6

RECOVERY_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"  # 去掉 I L O 0 1
RECOVERY_LENGTH = 10


def new_secret() -> str:
    return base64.b32encode(secrets.token_bytes(SECRET_BYTES)).decode().rstrip("=")


def decode_secret(secret_b32: str) -> bytes:
    cleaned = secret_b32.replace(" ", "").upper()
    return base64.b32decode(cleaned + "=" * (-len(cleaned) % 8))


def hotp(key: bytes, counter: int, digits: int = DIGITS, algorithm: str = "SHA1") -> str:
    mac = hmac.new(key, struct.pack(">Q", counter), ALGORITHMS[algorithm]).digest()
    offset = mac[-1] & 0x0F
    code = (struct.unpack(">I", mac[offset:offset + 4])[0] & 0x7FFFFFFF) % (10 ** digits)
    return str(code).zfill(digits)


def time_step(unix_seconds: float, step: int = STEP_SECONDS, t0: int = 0) -> int:
    return int((unix_seconds - t0) // step)


def totp_at(key: bytes, unix_seconds: float, digits: int = DIGITS, algorithm: str = "SHA1", step: int = STEP_SECONDS) -> str:
    return hotp(key, time_step(unix_seconds, step), digits, algorithm)


def match_step(secret_b32: str, code: str, unix_seconds: float, drift: int = 1, last_step: int | None = None,
               digits: int = DIGITS, algorithm: str = "SHA1", step: int = STEP_SECONDS) -> int | None:
    """返回匹配的时间步；不匹配或时间步 ≤ last_step（重放）返回 None。逐个常量时间比较。"""
    code = (code or "").strip().replace(" ", "")
    if len(code) != digits or not code.isdigit():
        return None
    key = decode_secret(secret_b32)
    current = time_step(unix_seconds, step)
    matched: int | None = None
    for counter in range(current - drift, current + drift + 1):
        if hmac.compare_digest(hotp(key, counter, digits, algorithm), code) and matched is None:
            matched = counter
    if matched is None:
        return None
    if last_step is not None and matched <= last_step:
        return None
    return matched


def otpauth_uri(secret_b32: str, account: str, issuer: str, digits: int = DIGITS, step: int = STEP_SECONDS) -> str:
    """Key URI（Google Authenticator 格式）：otpauth://totp/Issuer:account?secret=..&issuer=..&algorithm=SHA1&digits=6&period=30"""
    label = quote(f"{issuer}:{account}", safe=":@")
    query = urlencode({"secret": secret_b32, "issuer": issuer, "algorithm": "SHA1", "digits": digits, "period": step})
    return f"otpauth://totp/{label}?{query}"


def new_recovery_codes(count: int = 10) -> list[str]:
    codes: set[str] = set()
    while len(codes) < count:
        raw = "".join(secrets.choice(RECOVERY_ALPHABET) for _ in range(RECOVERY_LENGTH))
        codes.add(f"{raw[:5]}-{raw[5:]}")
    return sorted(codes)


def normalize_recovery_code(code: str) -> str:
    raw = "".join(ch for ch in (code or "").upper() if ch.isalnum())
    return f"{raw[:5]}-{raw[5:]}" if len(raw) == RECOVERY_LENGTH else raw
