"""identity 纯函数规则：手机号规范化与脱敏、会员号、验证码哈希、资料字段校验、时间窗口。"""
from __future__ import annotations

import hashlib
import re
import secrets
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from luzback import AppError, ErrorCode
from luzback.security import mask_phone

CN_MOBILE = re.compile(r"^1[3-9]\d{9}$")
INTL_PHONE = re.compile(r"^\+\d{6,15}$")
BIRTHDAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")
PROFILE_FIELDS = ("nickname", "avatar_url", "gender", "birthday")  # 用户可改字段白名单
NICKNAME_MAX = 20


def aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def normalize_cn_mobile(phone: str | None) -> str:
    phone = (phone or "").strip()
    if not CN_MOBILE.match(phone):
        raise AppError(ErrorCode.BAD_REQUEST, "invalid phone number")
    return phone


def normalize_wechat_phone(pure: str, country_code: str) -> str:
    """微信返回 purePhoneNumber + countryCode：中国大陆存 11 位，其余存 +区号号码。"""
    pure = (pure or "").strip()
    cc = (country_code or "86").strip().lstrip("+")
    if cc == "86":
        return normalize_cn_mobile(pure)
    full = f"+{cc}{pure}"
    if not INTL_PHONE.match(full):
        raise AppError(ErrorCode.BAD_REQUEST, "invalid phone number")
    return full


def masked(phone: str | None) -> str | None:
    return mask_phone(phone)


def new_member_no() -> str:
    """会员号：M + 10 位随机数字（不按注册顺序递增，避免泄露规模与被遍历）。"""
    return "M" + "".join(secrets.choice("0123456789") for _ in range(10))


def new_sms_code() -> str:
    return "".join(secrets.choice("0123456789") for _ in range(6))  # CSPRNG，6 位（NIST ≥ 6 位）


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def day_start(now: datetime, tz: str) -> datetime:
    local = now.astimezone(ZoneInfo(tz))
    return local.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)


def validate_nickname(value: str) -> str:
    value = (value or "").strip()
    if not 1 <= len(value) <= NICKNAME_MAX or any(ord(ch) < 32 for ch in value):
        raise AppError(ErrorCode.BAD_REQUEST, f"nickname must be 1-{NICKNAME_MAX} chars", {"field": "nickname"})
    return value


def validate_avatar_url(value: str) -> str:
    value = (value or "").strip()
    if value and (not value.startswith("https://") or len(value) > 500):
        raise AppError(ErrorCode.BAD_REQUEST, "avatar_url must be an https url", {"field": "avatar_url"})
    return value


def validate_gender(value: int) -> int:
    if value not in (0, 1, 2):
        raise AppError(ErrorCode.BAD_REQUEST, "gender must be 0/1/2", {"field": "gender"})
    return value


def validate_birthday(value: str | None, today: date) -> str | None:
    if not value:
        return None
    if not BIRTHDAY.match(value):
        raise AppError(ErrorCode.BAD_REQUEST, "birthday must be YYYY-MM-DD", {"field": "birthday"})
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise AppError(ErrorCode.BAD_REQUEST, "birthday must be a real date", {"field": "birthday"}) from exc
    if parsed > today or parsed.year < 1900:
        raise AppError(ErrorCode.BAD_REQUEST, "birthday out of range", {"field": "birthday"})
    return value


def deletion_due(now: datetime, cooling_days: int) -> datetime:
    return now + timedelta(days=cooling_days)
