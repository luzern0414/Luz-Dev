"""安全基线：JWT、口令哈希、TOTP、服务间签名、脱敏、安全响应头、请求 ID。

- JWT：HS256，含 iss / sub / iat / exp / jti / kind / ver；decode 校验签名、过期、kind、issuer。
- 口令：优先 argon2id（安装 argon2-cffi 时），否则 PBKDF2-HMAC-SHA256 600k 轮；格式自描述，可平滑升级。
- TOTP：RFC 6238，30 秒步长，6 位，允许前后各 1 步漂移；一次性码用过即作废由调用方记录。
- 服务间签名：HMAC-SHA256(secret, method\npath\ntimestamp\nnonce\nsha256(body))，时间偏差 ≤ 5 分钟，nonce 10 分钟去重。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import struct
import time
import uuid
from typing import Any

from .errors import AppError, ErrorCode

try:  # pragma: no cover - 可选依赖
    from argon2 import PasswordHasher as _Argon2Hasher
    from argon2.exceptions import VerifyMismatchError as _Argon2Mismatch
except Exception:  # noqa: BLE001
    _Argon2Hasher = None
    _Argon2Mismatch = Exception


# ---------- 编码 ----------

def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def unb64url(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _dump(obj: Any) -> bytes:
    return json.dumps(obj, separators=(",", ":"), sort_keys=True).encode()


def sha256_hex(data: bytes | str) -> str:
    if isinstance(data, str):
        data = data.encode()
    return hashlib.sha256(data).hexdigest()


# ---------- JWT ----------

def create_token(
    subject: str,
    secret: str,
    ttl_seconds: int,
    kind: str = "access",
    extra: dict | None = None,
    issuer: str = "luz",
) -> str:
    now = int(time.time())
    payload: dict[str, Any] = {"iss": issuer, "sub": subject, "iat": now, "exp": now + ttl_seconds, "kind": kind, "jti": uuid.uuid4().hex}
    if extra:
        payload.update(extra)
    signing_input = f"{b64url(_dump({'alg': 'HS256', 'typ': 'JWT'}))}.{b64url(_dump(payload))}"
    sig = hmac.new(secret.encode(), signing_input.encode(), hashlib.sha256).digest()
    return f"{signing_input}.{b64url(sig)}"


def decode_token(token: str, secret: str, kind: str = "access", issuer: str = "luz", leeway: int = 5) -> dict[str, Any]:
    try:
        head_b64, payload_b64, sig_b64 = token.split(".")
        header = json.loads(unb64url(head_b64))
    except Exception as exc:  # noqa: BLE001
        raise AppError(ErrorCode.UNAUTHORIZED, "malformed token") from exc
    if header.get("alg") != "HS256":
        raise AppError(ErrorCode.UNAUTHORIZED, "unsupported algorithm")
    expected = hmac.new(secret.encode(), f"{head_b64}.{payload_b64}".encode(), hashlib.sha256).digest()
    if not hmac.compare_digest(expected, unb64url(sig_b64)):
        raise AppError(ErrorCode.UNAUTHORIZED, "bad signature")
    payload = json.loads(unb64url(payload_b64))
    now = time.time()
    if payload.get("exp", 0) + leeway < now:
        raise AppError(ErrorCode.UNAUTHORIZED, "token expired")
    if payload.get("iat", 0) - leeway > now:
        raise AppError(ErrorCode.UNAUTHORIZED, "token not yet valid")
    if payload.get("kind") != kind:
        raise AppError(ErrorCode.UNAUTHORIZED, "wrong token kind")
    if payload.get("iss") != issuer:
        raise AppError(ErrorCode.UNAUTHORIZED, "wrong issuer")
    return payload


# ---------- 口令哈希 ----------

_PBKDF2_ITERATIONS = 600_000


def hash_password(password: str) -> str:
    if _Argon2Hasher is not None:  # pragma: no cover - 取决于环境
        return "argon2$" + _Argon2Hasher().hash(password)
    salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, _PBKDF2_ITERATIONS)
    return f"pbkdf2_sha256${_PBKDF2_ITERATIONS}${b64url(salt)}${b64url(digest)}"


def verify_password(password: str, stored: str) -> bool:
    try:
        if stored.startswith("argon2$"):
            if _Argon2Hasher is None:  # pragma: no cover
                return False
            try:
                return _Argon2Hasher().verify(stored[len("argon2$"):], password)
            except _Argon2Mismatch:
                return False
        algo, iterations, salt, digest = stored.split("$")
        if algo != "pbkdf2_sha256":
            return False
        calc = hashlib.pbkdf2_hmac("sha256", password.encode(), unb64url(salt), int(iterations))
        return hmac.compare_digest(calc, unb64url(digest))
    except Exception:  # noqa: BLE001
        return False


def password_needs_rehash(stored: str) -> bool:
    """旧算法或轮数不足时在下次登录成功后重新哈希。"""
    if stored.startswith("argon2$"):
        return False
    try:
        _, iterations, _, _ = stored.split("$")
        return int(iterations) < _PBKDF2_ITERATIONS
    except ValueError:
        return True


def check_password_policy(password: str, min_length: int = 10) -> None:
    if len(password) < min_length or not any(c.isalpha() for c in password) or not any(c.isdigit() for c in password):
        raise AppError(ErrorCode.BAD_REQUEST, f"password must be >= {min_length} chars with letters and digits")


# ---------- TOTP ----------

def totp_secret() -> str:
    return base64.b32encode(os.urandom(20)).decode().rstrip("=")


def _hotp(secret_b32: str, counter: int, digits: int = 6) -> str:
    key = base64.b32decode(secret_b32 + "=" * (-len(secret_b32) % 8))
    mac = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = mac[-1] & 0x0F
    code = (struct.unpack(">I", mac[offset:offset + 4])[0] & 0x7FFFFFFF) % (10 ** digits)
    return str(code).zfill(digits)


def totp_now(secret_b32: str, at: float | None = None, step: int = 30) -> str:
    return _hotp(secret_b32, int((at if at is not None else time.time()) // step))


def totp_verify(secret_b32: str, code: str, at: float | None = None, step: int = 30, drift: int = 1) -> bool:
    if not code or not code.isdigit():
        return False
    counter = int((at if at is not None else time.time()) // step)
    return any(hmac.compare_digest(_hotp(secret_b32, counter + d), code) for d in range(-drift, drift + 1))


# ---------- 服务间签名 ----------

def service_signature(secret: str, method: str, path: str, timestamp: str, nonce: str, body: bytes) -> str:
    message = "\n".join([method.upper(), path, timestamp, nonce, sha256_hex(body)]).encode()
    return hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()


class NonceStore:
    """内存 nonce 去重；生产换 Redis（SET NX EX）。"""

    def __init__(self, ttl_seconds: int = 600):
        self.ttl = ttl_seconds
        self._seen: dict[str, float] = {}

    def check_and_add(self, nonce: str, now: float | None = None) -> bool:
        now = now if now is not None else time.time()
        self._seen = {k: v for k, v in self._seen.items() if v > now - self.ttl}
        if nonce in self._seen:
            return False
        self._seen[nonce] = now
        return True


def verify_service_signature(
    secret: str,
    method: str,
    path: str,
    timestamp: str,
    nonce: str,
    body: bytes,
    signature: str,
    nonce_store: NonceStore,
    skew_seconds: int = 300,
    now: float | None = None,
) -> None:
    now = now if now is not None else time.time()
    try:
        ts = int(timestamp)
    except (TypeError, ValueError) as exc:
        raise AppError(ErrorCode.UNAUTHORIZED, "bad service timestamp") from exc
    if abs(now - ts) > skew_seconds:
        raise AppError(ErrorCode.UNAUTHORIZED, "service signature expired")
    expected = service_signature(secret, method, path, timestamp, nonce, body)
    if not hmac.compare_digest(expected, signature or ""):
        raise AppError(ErrorCode.UNAUTHORIZED, "bad service signature")
    if not nonce or not nonce_store.check_and_add(nonce, now):
        raise AppError(ErrorCode.UNAUTHORIZED, "service nonce reused")


# ---------- 脱敏 ----------

def mask_phone(phone: str | None) -> str | None:
    if not phone:
        return phone
    digits = "".join(ch for ch in phone if ch.isdigit())
    if len(digits) < 7:
        return "*" * len(phone)
    return digits[:3] + "*" * (len(digits) - 7) + digits[-4:]


def mask_name(name: str | None) -> str | None:
    if not name:
        return name
    return name[0] + "*" * (len(name) - 1) if len(name) > 1 else name


def mask_address(address: str | None, keep: int = 6) -> str | None:
    if not address:
        return address
    return address[:keep] + "****" if len(address) > keep else address


def random_code(length: int = 6) -> str:
    return "".join(secrets.choice("0123456789") for _ in range(length))


def random_token(nbytes: int = 32) -> str:
    return secrets.token_urlsafe(nbytes)


# ---------- 响应头与请求 ID ----------

SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
    "Strict-Transport-Security": "max-age=31536000; includeSubDomains",
    "Permissions-Policy": "geolocation=(), camera=(), microphone=()",
}


def new_request_id(incoming: str | None) -> str:
    """沿用网关传入的 X-Request-Id（1–64 位字母数字横线），否则生成。"""
    if incoming and len(incoming) <= 64 and all(c.isalnum() or c == "-" for c in incoming):
        return incoming
    return uuid.uuid4().hex


def new_trace_id(incoming: str | None) -> str:
    if incoming and len(incoming) <= 64 and all(c.isalnum() or c in "-_" for c in incoming):
        return incoming
    return uuid.uuid4().hex
