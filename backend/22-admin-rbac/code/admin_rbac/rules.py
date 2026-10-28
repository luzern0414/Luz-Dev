"""纯函数规则：账号名、手机号、锁定时长、会话时限判断。不访问数据库，便于单测。"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

from luzback import AppError, ErrorCode

USERNAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{2,31}$")
RESERVED_USERNAMES = frozenset({"admin", "administrator", "root", "sa", "system", "superuser", "test", "guest", "luz"})
PHONE_PATTERN = re.compile(r"^1\d{10}$")


def aware(dt: datetime | None) -> datetime | None:
    """SQLite 读回的时间可能是 naive（按 UTC 解释）。"""
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def normalize_username(username: str | None) -> str:
    return (username or "").strip().lower()[:64]


def field_error(field: str, message: str, code: ErrorCode = ErrorCode.BAD_REQUEST) -> AppError:
    return AppError(code, message, data={"errors": [{"field": field, "message": message}]})


def validate_username(username: str | None) -> str:
    """3–32 位小写字母、数字、. _ -；不允许默认账号名（ASVS 6.3.2）。"""
    name = normalize_username(username)
    if not USERNAME_PATTERN.match(name):
        raise field_error("username", "username must be 3-32 chars of a-z, 0-9, . _ -")
    if name in RESERVED_USERNAMES:
        raise field_error("username", "this username is reserved")
    return name


def validate_phone(phone: str | None) -> str | None:
    if phone is None or phone.strip() == "":
        return None
    phone = phone.strip()
    if not PHONE_PATTERN.match(phone):
        raise field_error("phone", "invalid mainland China mobile number")
    return phone


def lock_seconds(lock_count: int, base_seconds: int, max_seconds: int) -> int:
    """第 n 次锁定（n 从 1 开始）时长 = base × 2^(n-1)，封顶 max（NIST 800-63B §3.2.2 递增等待）。"""
    n = max(1, lock_count)
    return min(max_seconds, base_seconds * (2 ** min(n - 1, 20)))


def session_expiry_reason(now: datetime, last_seen_at: datetime, expires_at: datetime, idle_seconds: int) -> str | None:
    """返回 'absolute_timeout' / 'idle_timeout' / None。"""
    if now >= aware(expires_at):
        return "absolute_timeout"
    if now - aware(last_seen_at) >= timedelta(seconds=idle_seconds):
        return "idle_timeout"
    return None


def is_fresh(now: datetime, auth_at: datetime | None, window_seconds: int) -> bool:
    return auth_at is not None and now - aware(auth_at) <= timedelta(seconds=window_seconds)
