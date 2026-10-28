"""纯函数规则：订阅消息关键词格式化、文本渲染、静默时段、错误码分类、退避、推送事件解析。

全部无数据库、无网络，便于单测。官方依据见 MODULE.md §0（subscribeMessage.send 关键词类型表、订阅消息事件推送）。
"""
from __future__ import annotations

import re
import string
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

# ---------------------------------------------------------------- 关键词类型（官方表）

KEYWORD_LIMITS: dict[str, int | None] = {
    "thing": 20, "number": 32, "letter": 32, "symbol": 5, "character_string": 32, "time": None, "date": None,
    "amount": 10, "phone_number": 17, "car_number": 8, "name": 10, "phrase": 5, "enum": None,
}
KEYWORD_TYPES = frozenset(KEYWORD_LIMITS)
TRUNCATABLE = frozenset({"thing", "letter", "name", "phrase"})  # 截断不改变含义类别；编号 / 金额 / 时间不截断，直接判无效
ELLIPSIS = "…"

_KEY_RE = re.compile(r"^([a-z_]+?)(\d{1,2})$")
_CJK = r"〇㐀-䶿一-鿿"
_CJK_RE = re.compile(f"[{_CJK}]")
_ALL_CJK_RE = re.compile(f"^[{_CJK}]+$")
_DATE = r"(?:\d{4}年\d{1,2}月\d{1,2}日|\d{4}年\d{1,2}月|\d{1,2}月\d{1,2}日|\d{4}[-/.]\d{1,2}[-/.]\d{1,2})"
_TIME = r"(?:[01]?\d|2[0-3]):[0-5]\d(?::[0-5]\d)?"
_TIME_ONE = rf"(?:{_DATE} ?)?{_TIME}"
_DATE_ONE = rf"{_DATE}(?: ?{_TIME})?"
_TIME_RE = re.compile(rf"^{_TIME_ONE}(?: ?~ ?{_TIME_ONE})?$")
_DATE_RE = re.compile(rf"^{_DATE_ONE}(?: ?~ ?{_DATE_ONE})?$")
_NUMBER_RE = re.compile(r"^\d+(?:\.\d+)?$")
_LETTER_RE = re.compile(r"^[A-Za-z]+$")
_CHARSTR_RE = re.compile(r"^[\x21-\x7e]+$")
_AMOUNT_RE = re.compile(r"^([¥￥$€£])?(\d+(?:\.\d+)?)(元)?$")
_PHONE_RE = re.compile(r"^[0-9+\-()#*]{1,17}$")
_CAR_RE = re.compile(rf"^(?:[{_CJK}]|[A-Z0-9])[A-Z0-9]{{0,6}}(?:[{_CJK}]|[A-Z0-9])?$")
_FIELD_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")


class FormatError(ValueError):
    """值不满足关键词类型规则（不能安全截断）。投递记为 skipped，错误码 format。"""

    def __init__(self, key: str, reason: str):
        super().__init__(f"{key}: {reason}")
        self.key = key
        self.reason = reason


class MissingField(KeyError):
    """模板占位符在业务数据里不存在。投递记为 skipped，错误码 missing_field。"""

    def __init__(self, field: str):
        super().__init__(field)
        self.field = field


def keyword_type(key: str) -> str:
    """thing1 → thing；character_string2 → character_string；number01 → number。未知类型抛 FormatError。"""
    match = _KEY_RE.match(key or "")
    if not match or match.group(1) not in KEYWORD_TYPES:
        raise FormatError(key, "unknown keyword type")
    return match.group(1)


def clean_text(value: Any) -> str:
    """去掉控制字符（含换行、零宽字符），合并空白，首尾去空格。官方：符号不能带换行等控制字符。"""
    text = "" if value is None else str(value)
    out = []
    for ch in text:
        cat = unicodedata.category(ch)
        if ch in "\r\n\t  ":
            out.append(" ")
        elif cat in ("Cc", "Cf", "Cs", "Co", "Cn"):
            continue
        else:
            out.append(ch)
    return re.sub(r"\s+", " ", "".join(out)).strip()


def _truncate(text: str, limit: int, ellipsis: bool = True) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 1] + ELLIPSIS if ellipsis and limit > 1 else text[:limit]


def _local(value: datetime, tz: str) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(ZoneInfo(tz))


def _cn_date(d: date) -> str:
    return f"{d.year}年{d.month}月{d.day}日"


def format_keyword(key: str, value: Any, *, tz: str = "Asia/Shanghai", enum_values: list[str] | None = None) -> str:
    """把业务值格式化为该关键词类型的合法值。可截断类型安全截断，其余不合法抛 FormatError。"""
    kind = keyword_type(key)
    if value is None or (isinstance(value, str) and not value.strip()):
        raise FormatError(key, "empty value")
    if isinstance(value, bool):
        raise FormatError(key, "boolean not allowed")

    if kind == "time":
        if isinstance(value, datetime):
            local = _local(value, tz)
            return f"{_cn_date(local.date())} {local:%H:%M}"
        if isinstance(value, time):
            return f"{value:%H:%M}"
        text = clean_text(value)
        if not _TIME_RE.match(text):
            raise FormatError(key, "time must be HH:MM[:SS] with optional date, or a range joined by ~")
        return text
    if kind == "date":
        if isinstance(value, datetime):
            local = _local(value, tz)
            return f"{_cn_date(local.date())} {local:%H:%M}"
        if isinstance(value, date):
            return _cn_date(value)
        text = clean_text(value)
        if not _DATE_RE.match(text):
            raise FormatError(key, "date must be y年m月d日 / y-m-d with optional time")
        return text
    if kind == "amount":
        if isinstance(value, int):  # 整数一律视为分
            if value < 0:
                raise FormatError(key, "negative amount")
            text = f"¥{value // 100}.{value % 100:02d}"
            symbol, digits = "¥", f"{value // 100}{value % 100:02d}"
        else:
            text = clean_text(value)
            match = _AMOUNT_RE.match(text)
            if not match:
                raise FormatError(key, "amount must be a currency symbol plus digits")
            symbol, digits = match.group(1), match.group(2).replace(".", "")
            if not symbol:
                text = "¥" + text
        if len(digits) > 10:
            raise FormatError(key, "amount longer than 10 digits")
        return text
    if kind == "number":
        if isinstance(value, (int, Decimal)) or isinstance(value, float):
            text = format(value, "f") if isinstance(value, (Decimal, float)) else str(value)
            if isinstance(value, float):
                text = text.rstrip("0").rstrip(".") or "0"
        else:
            text = clean_text(value)
        if not _NUMBER_RE.match(text) or len(text) > 32:
            raise FormatError(key, "number must be digits (optional decimals), at most 32")
        return text

    text = clean_text(value)
    if not text:
        raise FormatError(key, "empty value")
    if kind == "thing":
        return _truncate(text, 20)
    if kind == "letter":
        if not _LETTER_RE.match(text):
            raise FormatError(key, "letter must contain only letters")
        return text[:32]
    if kind == "symbol":
        if any(ch.isalnum() or _CJK_RE.match(ch) or ch.isspace() for ch in text) or len(text) > 5:
            raise FormatError(key, "symbol must be at most 5 symbols")
        return text
    if kind == "character_string":
        if not _CHARSTR_RE.match(text) or len(text) > 32:
            raise FormatError(key, "character_string must be at most 32 ASCII letters, digits or symbols")
        return text
    if kind == "phone_number":
        compact = text.replace(" ", "")
        if not _PHONE_RE.match(compact):
            raise FormatError(key, "phone_number must be at most 17 digits or symbols")
        return compact
    if kind == "car_number":
        compact = text.replace(" ", "").upper()
        if len(compact) > 8 or not _CAR_RE.match(compact):
            raise FormatError(key, "car_number must be at most 8 chars, Chinese only at first / last position")
        return compact
    if kind == "name":
        if any(ch.isdigit() for ch in text):
            raise FormatError(key, "name must not contain digits")
        if _CJK_RE.search(text):  # 纯中文或中英混合：按中文算，10 个以内
            return _truncate(text, 10, ellipsis=False)
        return _truncate(text, 20, ellipsis=False)
    if kind == "phrase":
        if not _ALL_CJK_RE.match(text):
            raise FormatError(key, "phrase must be Chinese characters only")
        return text[:5]
    if kind == "enum":
        if not enum_values or text not in enum_values:
            raise FormatError(key, "value not in enum range")
        return text
    raise FormatError(key, "unsupported type")  # pragma: no cover


# ---------------------------------------------------------------- 文本渲染

def placeholders(template: str) -> list[str]:
    """模板里的占位符名；只允许 {标识符}，不允许属性访问、下标、格式说明（防模板注入）。"""
    names: list[str] = []
    try:
        parsed = list(string.Formatter().parse(template or ""))
    except ValueError as exc:
        raise ValueError(f"bad template: {exc}") from exc
    for _literal, field, spec, conversion in parsed:
        if field is None:
            continue
        if not _FIELD_RE.match(field) or spec or conversion:
            raise ValueError(f"placeholder not allowed: {{{field}}}")
        names.append(field)
    return names


def render_text(template: str, data: dict[str, Any]) -> str:
    """用业务数据渲染 {field} 模板；缺字段抛 MissingField。"""
    names = placeholders(template)
    values = {}
    for name in names:
        if name not in data or data[name] is None:
            raise MissingField(name)
        values[name] = data[name]
    return template.format_map(values)


def resolve_source(source: str, data: dict[str, Any], default: Any = None) -> Any:
    """关键词取值：裸字段名返回原始类型值（金额分、datetime），含花括号按文本模板渲染。"""
    if "{" in source:
        try:
            return render_text(source, data)
        except MissingField:
            if default is not None:
                return default
            raise
    if source not in data or data[source] is None:
        if default is not None:
            return default
        raise MissingField(source)
    return data[source]


def build_subscribe_data(mapping: list[dict[str, Any]], data: dict[str, Any], tz: str) -> dict[str, dict[str, str]]:
    """按关键词映射生成 subscribeMessage.send 的 data：{"thing1": {"value": "..."}}。"""
    out: dict[str, dict[str, str]] = {}
    for item in mapping:
        key = item["key"]
        raw = resolve_source(item["source"], data, item.get("default"))
        out[key] = {"value": format_keyword(key, raw, tz=tz, enum_values=item.get("enum"))}
    return out


def validate_subscribe_mapping(mapping: Any) -> list[dict[str, Any]]:
    """后台保存映射时校验：key 类型合法且不重复、source 合法、enum 类型带取值范围。返回规范化后的列表。"""
    if not isinstance(mapping, list) or not mapping:
        raise ValueError("keywords must be a non-empty list")
    if len(mapping) > 20:
        raise ValueError("too many keywords")
    seen: set[str] = set()
    out = []
    for item in mapping:
        if not isinstance(item, dict) or not isinstance(item.get("key"), str) or not isinstance(item.get("source"), str):
            raise ValueError("each keyword needs key and source")
        key = item["key"].strip()
        try:
            kind = keyword_type(key)
        except FormatError as exc:
            raise ValueError(str(exc)) from exc
        if key in seen:
            raise ValueError(f"duplicate keyword {key}")
        seen.add(key)
        source = item["source"].strip()
        if "{" in source:
            placeholders(source)
        elif not _FIELD_RE.match(source):
            raise ValueError(f"{key}: source must be a field name or a {{field}} template")
        norm: dict[str, Any] = {"key": key, "source": source}
        if kind == "enum":
            values = item.get("enum")
            if not isinstance(values, list) or not values or not all(isinstance(v, str) for v in values):
                raise ValueError(f"{key}: enum keyword needs an enum value list")
            norm["enum"] = values
        if item.get("default") is not None:
            norm["default"] = str(item["default"])
        out.append(norm)
    return out


def validate_sms_params(params: Any) -> list[dict[str, str]]:
    """短信模板参数：[{"name": "code", "source": "{order_no}"}]，按顺序作为模板变量。"""
    if params is None:
        return []
    if not isinstance(params, list) or len(params) > 10:
        raise ValueError("sms params must be a list (at most 10)")
    out = []
    for item in params:
        if not isinstance(item, dict) or not isinstance(item.get("name"), str) or not isinstance(item.get("source"), str):
            raise ValueError("each sms param needs name and source")
        if not _FIELD_RE.match(item["name"]):
            raise ValueError("sms param name must be an identifier")
        if "{" in item["source"]:
            placeholders(item["source"])
        elif not _FIELD_RE.match(item["source"]):
            raise ValueError("sms param source must be a field name or template")
        out.append({"name": item["name"], "source": item["source"]})
    return out


def build_sms_params(params: list[dict[str, str]], data: dict[str, Any]) -> dict[str, str]:
    return {p["name"]: clean_text(resolve_source(p["source"], data)) for p in params}


# ---------------------------------------------------------------- 静默时段

def parse_hhmm(text: str) -> time:
    hour, minute = text.strip().split(":")
    return time(int(hour), int(minute))


def in_quiet_hours(local: datetime, start: time, end: time) -> bool:
    """[start, end) 内为静默；跨午夜（22:00–08:00）支持；start == end 表示不静默。"""
    t = local.time().replace(tzinfo=None)
    if start == end:
        return False
    if start < end:
        return start <= t < end
    return t >= start or t < end


def quiet_release(moment: datetime, tz: str, start: time, end: time) -> datetime | None:
    """moment 在静默时段内 → 返回静默结束时刻（UTC）；否则 None。"""
    local = _local(moment, tz)
    if not in_quiet_hours(local, start, end):
        return None
    release = local.replace(hour=end.hour, minute=end.minute, second=0, microsecond=0)
    if release <= local:
        release += timedelta(days=1)
    return release.astimezone(timezone.utc)


def local_day(moment: datetime, tz: str) -> str:
    return _local(moment, tz).strftime("%Y-%m-%d")


def next_local_midnight(moment: datetime, tz: str) -> datetime:
    local = _local(moment, tz)
    nxt = (local + timedelta(days=1)).replace(hour=0, minute=0, second=5, microsecond=0)
    return nxt.astimezone(timezone.utc)


# ---------------------------------------------------------------- 错误码分类

OUTCOME_SENT = "sent"
OUTCOME_RETRY = "retry"          # 可重试：系统繁忙、并发下发、令牌失效、超时
OUTCOME_DEFER_DAY = "defer_day"  # 日调用额度用完：延后到次日
OUTCOME_NO_QUOTA = "no_quota"    # 43101：用户未订阅 / 次数用完 / 已拒收
OUTCOME_PERMANENT = "permanent"  # 模板、参数、敏感词、封禁、openid 错误：不重试

WECHAT_RETRYABLE = frozenset({-1, 43108, 45011, 40001, 40014, 42001})
WECHAT_DEFER_DAY = frozenset({45009})
WECHAT_NO_QUOTA = frozenset({43101})
WECHAT_PERMANENT_HINTS = {
    40003: "invalid openid", 40037: "invalid template_id", 43107: "subscribe capability banned",
    45168: "sensitive content", 47003: "data format invalid", 41030: "invalid page", 43104: "openid not of this appid",
}


def classify_wechat(errcode: int) -> str:
    if errcode == 0:
        return OUTCOME_SENT
    if errcode in WECHAT_RETRYABLE:
        return OUTCOME_RETRY
    if errcode in WECHAT_DEFER_DAY:
        return OUTCOME_DEFER_DAY
    if errcode in WECHAT_NO_QUOTA:
        return OUTCOME_NO_QUOTA
    return OUTCOME_PERMANENT  # 未知错误码不盲目重试


# 短信：频控类错误码（腾讯云 / 阿里云）不重试，直接按限流跳过；系统错误可重试
SMS_RATE_LIMIT_CODES = frozenset({
    "LimitExceeded.PhoneNumberDailyLimit", "LimitExceeded.PhoneNumberOneHourLimit",
    "LimitExceeded.PhoneNumberThirtySecondLimit", "LimitExceeded.PhoneNumberSameContentDailyLimit",
    "LimitExceeded.DeliveryFrequencyLimit", "isv.BUSINESS_LIMIT_CONTROL", "isv.DAY_LIMIT_CONTROL",
})
SMS_RETRYABLE_PREFIXES = ("InternalError", "isp.SYSTEM_ERROR", "RequestLimitExceeded", "Throttling", "ServiceUnavailable")


def classify_sms(code: str, retryable: bool | None = None) -> str:
    if code in ("", "0", "Ok", "OK"):
        return OUTCOME_SENT
    if code in SMS_RATE_LIMIT_CODES:
        return OUTCOME_PERMANENT
    if retryable is not None:
        return OUTCOME_RETRY if retryable else OUTCOME_PERMANENT
    return OUTCOME_RETRY if code.startswith(SMS_RETRYABLE_PREFIXES) else OUTCOME_PERMANENT


def backoff_seconds(attempts: int, schedule: tuple[int, ...]) -> int:
    """第 attempts 次失败后的等待秒数（attempts 从 1 开始）。"""
    if not schedule:
        return 60
    return schedule[min(max(attempts, 1) - 1, len(schedule) - 1)]


# ---------------------------------------------------------------- 订阅授权结果

ACCEPT_STATUSES = frozenset({"accept", "acceptWithAudio", "acceptWithAlert", "acceptWithForcePush"})
REJECT_STATUSES = frozenset({"reject"})
BAN_STATUSES = frozenset({"ban"})
IGNORED_STATUSES = frozenset({"filter"})
REQUEST_MAX_TEMPLATES = 5  # wx.requestSubscribeMessage 一次最多 5 个模板


@dataclass(frozen=True)
class SubscribeEventItem:
    event: str          # popup | change | sent
    openid: str
    template_id: str
    status: str = ""    # accept / reject（popup、change）
    msg_id: str = ""
    error_code: int = 0
    error_status: str = ""
    popup_scene: str = ""
    create_time: int = 0


_EVENT_ALIASES = {
    "subscribe_msg_popup_event": ("popup", "SubscribeMsgPopupEvent"),
    "subscribe_msg_change_event": ("change", "SubscribeMsgChangeEvent"),
    "subscribe_msg_sent_event": ("sent", "SubscribeMsgSentEvent"),
}


def _as_list(value: Any) -> list[dict[str, Any]]:
    """官方：List 只有一项时是对象，多于一项时是数组，两种都要兼容。"""
    if value is None:
        return []
    if isinstance(value, dict):
        return [value]
    if isinstance(value, list):
        return [v for v in value if isinstance(v, dict)]
    return []


def parse_subscribe_event(message: dict[str, Any]) -> list[SubscribeEventItem]:
    """解析已解密的消息推送（JSON，或 XML 转出的 dict）。非订阅事件返回空列表。"""
    name = str(message.get("Event") or "")
    alias = _EVENT_ALIASES.get(name)
    if alias is None:
        return []
    kind, wrapper = alias
    container = message.get(wrapper) if isinstance(message.get(wrapper), dict) else message
    items = _as_list(container.get("List"))
    openid = str(message.get("FromUserName") or "")
    try:
        created = int(message.get("CreateTime") or 0)
    except (TypeError, ValueError):
        created = 0
    out = []
    for raw in items:
        template_id = str(raw.get("TemplateId") or "")
        if not template_id:
            continue
        try:
            error_code = int(raw.get("ErrorCode") or 0)
        except (TypeError, ValueError):
            error_code = -1
        out.append(SubscribeEventItem(
            event=kind, openid=openid, template_id=template_id, status=str(raw.get("SubscribeStatusString") or ""),
            msg_id=str(raw.get("MsgID") or ""), error_code=error_code, error_status=str(raw.get("ErrorStatus") or ""),
            popup_scene=str(raw.get("PopupScene") or ""), create_time=created,
        ))
    return out
