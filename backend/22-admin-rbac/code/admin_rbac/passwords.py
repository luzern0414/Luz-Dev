"""口令：Argon2id 哈希（PHC 字符串）、口令策略、常见口令表、泄露检查钩子。

- 哈希：Argon2id，默认 m=19456 KiB、t=2、p=1（OWASP Password Storage Cheat Sheet 最低配置）；
  有 argon2-cffi 用它，否则用 cryptography（≥44）自带的 Argon2id，两者都输出标准 PHC 串
  `$argon2id$v=19$m=..,t=..,p=..$salt$hash`，可互相校验。
- 兼容旧格式：框架 luzback.security 的 `argon2$...` / `pbkdf2_sha256$...`，校验通过后 needs_rehash=True。
- 策略（NIST 800-63B-4 §3.1.1.2、ASVS 6.2）：NFC 规范化；启用 MFA ≥ 8 位，否则 ≥ 15 位；上限 ≥ 64（默认 128）；
  不设组成规则；拒绝常见口令、与账号 / 姓名 / 系统名相同或包含它们的口令；可接泄露库检查。
"""
from __future__ import annotations

import base64
import hmac
import logging
import os
import unicodedata
from dataclasses import dataclass

from luzback import AppError, ErrorCode
from luzback.security import verify_password as legacy_verify

from .config import get_breach_checker, get_config

log = logging.getLogger("admin_rbac.passwords")

try:  # pragma: no cover - 取决于环境
    from argon2 import PasswordHasher as _CffiHasher
    from argon2.exceptions import VerificationError as _CffiMismatch
except Exception:  # noqa: BLE001
    _CffiHasher = None
    _CffiMismatch = Exception

try:
    from cryptography.hazmat.primitives.kdf.argon2 import Argon2id as _CryptoArgon2id
except Exception:  # noqa: BLE001  # pragma: no cover
    _CryptoArgon2id = None

MAX_PASSWORD_BYTES = 1024  # 防止超长输入拖慢哈希（DoS）
SALT_BYTES = 16
HASH_BYTES = 32


def backend_name() -> str:
    if _CffiHasher is not None:  # pragma: no cover
        return "argon2-cffi"
    if _CryptoArgon2id is not None:
        return "cryptography"
    return "none"  # pragma: no cover


def normalize(password: str) -> str:
    return unicodedata.normalize("NFC", password or "")


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode().rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.b64decode(text + "=" * (-len(text) % 4))


@dataclass(frozen=True)
class Argon2Params:
    memory_kib: int
    time_cost: int
    parallelism: int


def current_params() -> Argon2Params:
    cfg = get_config()
    return Argon2Params(cfg.argon2_memory_kib, cfg.argon2_time_cost, cfg.argon2_parallelism)


def parse_phc(stored: str) -> tuple[Argon2Params, bytes, bytes] | None:
    """解析 `$argon2id$v=19$m=..,t=..,p=..$salt$hash`；不是 argon2id 返回 None。"""
    parts = stored.split("$")
    if len(parts) != 6 or parts[1] != "argon2id" or parts[2] != "v=19":
        return None
    try:
        kv = dict(item.split("=", 1) for item in parts[3].split(","))
        params = Argon2Params(int(kv["m"]), int(kv["t"]), int(kv["p"]))
        return params, _unb64(parts[4]), _unb64(parts[5])
    except (KeyError, ValueError):
        return None


def _derive(secret: bytes, salt: bytes, params: Argon2Params, length: int) -> bytes:
    if _CryptoArgon2id is None:  # pragma: no cover
        raise RuntimeError("Argon2id unavailable: install argon2-cffi or cryptography>=44")
    return _CryptoArgon2id(salt=salt, length=length, iterations=params.time_cost, lanes=params.parallelism,
                           memory_cost=params.memory_kib).derive(secret)


def hash_secret(secret: str, params: Argon2Params | None = None) -> str:
    """Argon2id 哈希任意秘密（口令、恢复码），返回 PHC 串。"""
    params = params or current_params()
    data = normalize(secret).encode()
    if _CffiHasher is not None:  # pragma: no cover
        return _CffiHasher(time_cost=params.time_cost, memory_cost=params.memory_kib, parallelism=params.parallelism,
                           hash_len=HASH_BYTES, salt_len=SALT_BYTES).hash(data)
    salt = os.urandom(SALT_BYTES)
    digest = _derive(data, salt, params, HASH_BYTES)
    return f"$argon2id$v=19$m={params.memory_kib},t={params.time_cost},p={params.parallelism}${_b64(salt)}${_b64(digest)}"


def verify_secret(secret: str, stored: str | None) -> bool:
    """常量时间比较；未知格式返回 False，不抛异常。"""
    if not stored:
        return False
    data = normalize(secret).encode()
    if len(data) > MAX_PASSWORD_BYTES:
        return False
    parsed = parse_phc(stored)
    if parsed is not None:
        params, salt, digest = parsed
        if _CffiHasher is not None:  # pragma: no cover
            try:
                return _CffiHasher().verify(stored, data)
            except _CffiMismatch:
                return False
            except Exception:  # noqa: BLE001
                return False
        try:
            return hmac.compare_digest(_derive(data, salt, params, len(digest)), digest)
        except Exception:  # noqa: BLE001
            return False
    # 框架旧格式（argon2$ / pbkdf2_sha256$）
    return legacy_verify(normalize(secret), stored)


def needs_rehash(stored: str) -> bool:
    parsed = parse_phc(stored or "")
    if parsed is None:
        return True
    params, _, _ = parsed
    want = current_params()
    return params.memory_kib < want.memory_kib or params.time_cost < want.time_cost or params.parallelism < want.parallelism


_dummy_hash: str | None = None


def dummy_verify(secret: str) -> None:
    """账号不存在时也跑一次同参数哈希，使响应耗时一致（OWASP Authentication Cheat Sheet）。"""
    global _dummy_hash
    if _dummy_hash is None:
        _dummy_hash = hash_secret("dummy-password-for-timing")
    verify_secret(secret, _dummy_hash)


# ---------- 策略 ----------

# 常见口令（节选自公开的高频泄露口令榜单，小写比较；项目可用 ADMIN_PASSWORD_BLOCKLIST 追加业务词）
COMMON_PASSWORDS: frozenset[str] = frozenset("""
123456 123456789 12345678 password qwerty123 qwerty 12345 1234567 111111 1234567890 123123 abc123 1234 password1
iloveyou 1q2w3e4r 000000 qwerty12 qwertyuiop 654321 555555 lovely 7777777 welcome 888888 princess dragon
password123 123qwe 666666 1qaz2wsx 121212 sunshine monkey football baseball master shadow superman michael
letmein trustno1 hello123 admin admin123 administrator root toor passw0rd p@ssw0rd p@ssword qwe123 zaq12wsx
q1w2e3r4 1q2w3e 1qazxsw2 asdfghjkl asdf1234 zxcvbnm zxcvbnm123 aa123456 a123456 a12345678 abc12345 abcd1234
woaini1314 woaini 5201314 1314520 520520 147258369 159357 147258 123321 112233 11223344 987654321 88888888
66666666 11111111 00000000 12341234 qq123456 iloveyou1 changeme changeme123 default guest test test123 test1234
welcome1 welcome123 login secret secret123 summer2024 winter2024 spring2025 autumn2025 password2024 password2025
admin@123 admin1234 admin12345 root123 root1234 manager manager123 user123 demo demo123 luz luz123456 luzadmin
1234qwer qwer1234 qwerasdf asdfqwer 1qaz2wsx3edc qazwsxedc qazwsx 7758521 7758258 aini1314 woaini520
111222tianya hotmail gmail baidu taobao alibaba tencent wechat weixin ilovechina china123 beijing shanghai
superadmin super123 adminadmin passwordpassword 123456123456 1234512345 0123456789 9876543210 abcdefg abcdefgh
abcdefghijk abcdefghijklmnop aaaaaaaa aaaaaaaaaaaaaaaa 1111111111111111 1234567890123456 qwertyuiopasdfgh
""".split())


def policy_min_length(mfa: bool) -> int:
    cfg = get_config()
    return cfg.password_min_length_mfa if mfa else cfg.password_min_length_no_mfa


def check_policy(password: str, *, mfa: bool, context: tuple[str | None, ...] = (), field_name: str = "password") -> str:
    """校验并返回规范化后的口令；失败 4001（errors[{field, message}]）。不设组成规则。"""
    cfg = get_config()
    pw = normalize(password)

    def fail(message: str) -> AppError:
        return AppError(ErrorCode.BAD_REQUEST, message, data={"errors": [{"field": field_name, "message": message}]})

    min_len = policy_min_length(mfa)
    if len(pw) < min_len:
        raise fail(f"password must be at least {min_len} characters")
    if len(pw) > cfg.password_max_length or len(pw.encode()) > MAX_PASSWORD_BYTES:
        raise fail(f"password must be at most {cfg.password_max_length} characters")
    lowered = pw.lower()
    blocked = COMMON_PASSWORDS | {w.lower() for w in cfg.password_blocklist_extra}
    if lowered in blocked or len(set(lowered)) <= 2:
        raise fail("password is too common")
    for word in (*context, *cfg.password_blocklist_extra):
        if word and len(word) >= 3 and word.lower() in lowered:
            raise fail("password must not contain your account name or other predictable words")
    checker = get_breach_checker()
    if checker is not None:
        try:
            breached = checker(pw)
        except Exception as exc:  # noqa: BLE001
            log.warning("breach_check_failed", extra={"fields": {"event": "breach_check_failed", "error": type(exc).__name__}})
            if cfg.password_breach_fail_closed:
                raise AppError(ErrorCode.UPSTREAM_FAILED, "password check unavailable, retry later") from exc
            breached = False
        if breached:
            raise fail("password appears in a known data breach")
    return pw
