"""会员卡纯函数规则：有效期边界、时段窗口、已用天数、退卡公式。

退卡公式（法释〔2025〕4号 第十四、十五、十六、十八、十九、二十条）：
- 七日规则：付款之日起 cooling_off_days 日内、卡未使用 → 全额退实付本金（不论原因）。
- 经营者原因：已兑付按折扣价计价；有赠送时折扣比例按 实付 /（实付 + 赠送）——即每单位价值 = 实付 / 总单位（含赠送）；
  已兑付价值 = ⌊实付 × 已用单位 / 总单位⌋；可退 = 实付 − 已兑付价值；另按合同成立时一年期 LPR 计利息。
- 消费者原因：已兑付按打折前价格计价，每单位 = 原价 / 付费单位；已兑付价值 = min(实付, ⌊原价 × 已用单位 / 付费单位⌋)
  （超出实付的部分不再追要）；可退 = 实付 − 已兑付价值；可按卡种扣手续费（默认 0）。
- 赠送部分不退：赠送单位只在经营者原因时摊薄单价，剩余赠送单位不折现。
"""
from __future__ import annotations

import json
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from luzback import AppError, ErrorCode

CAUSE_CUSTOMER = "customer"
CAUSE_MERCHANT = "merchant"
CAUSES = (CAUSE_CUSTOMER, CAUSE_MERCHANT)
RULE_COOLING_OFF = "cooling_off_full"
RULE_CUSTOMER = "customer_original_price"
RULE_MERCHANT = "merchant_discounted"

DAY = 86400


def aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def ceil_days(seconds: float) -> int:
    if seconds <= 0:
        return 0
    return int(-(-seconds // DAY))


def local_midnight_after(start: datetime, days: int, tz: str) -> datetime:
    """开始时刻所在的本地日算第 1 天，返回第 days 天结束（次日 0 点）的 UTC 时刻。"""
    local = aware(start).astimezone(ZoneInfo(tz))
    end_day = local.date() + timedelta(days=days)
    return datetime.combine(end_day, time.min, tzinfo=ZoneInfo(tz)).astimezone(timezone.utc)


def day_start(day: date, tz: str) -> datetime:
    return datetime.combine(day, time.min, tzinfo=ZoneInfo(tz)).astimezone(timezone.utc)


# ---------------------------------------------------------------- 时段窗口


def _minutes(text: str) -> int:
    try:
        hh, mm = text.split(":")
        value = int(hh) * 60 + int(mm)
    except (ValueError, AttributeError) as exc:
        raise AppError(ErrorCode.BAD_REQUEST, f"invalid time {text!r}, expected HH:MM") from exc
    if not 0 <= value <= 24 * 60:
        raise AppError(ErrorCode.BAD_REQUEST, f"invalid time {text!r}")
    return value


def validate_windows(windows: list[dict]) -> list[dict]:
    """[{"weekdays":[1..7], "start":"HH:MM", "end":"HH:MM"}]；end <= start 表示跨午夜。"""
    if not isinstance(windows, list) or not windows:
        raise AppError(ErrorCode.BAD_REQUEST, "time_slot card requires at least one window")
    out = []
    for w in windows:
        days = sorted({int(d) for d in w.get("weekdays", [])})
        if not days or any(d < 1 or d > 7 for d in days):
            raise AppError(ErrorCode.BAD_REQUEST, "weekdays must be 1..7")
        start, end = _minutes(w.get("start", "")), _minutes(w.get("end", ""))
        if start == end:
            raise AppError(ErrorCode.BAD_REQUEST, "window start and end must differ")
        out.append({"weekdays": days, "start": w["start"], "end": w["end"]})
    return out


def in_windows(at: datetime, windows_json: str, tz: str) -> bool:
    windows = json.loads(windows_json or "[]")
    if not windows:
        return True
    local = aware(at).astimezone(ZoneInfo(tz))
    minute = local.hour * 60 + local.minute
    weekday = local.isoweekday()
    yesterday = 7 if weekday == 1 else weekday - 1
    for w in windows:
        start, end = _minutes(w["start"]), _minutes(w["end"])
        if start < end:
            if weekday in w["weekdays"] and start <= minute < end:
                return True
        else:  # 跨午夜：前半段属当天，后半段属前一天的窗口
            if weekday in w["weekdays"] and minute >= start:
                return True
            if yesterday in w["weekdays"] and minute < end:
                return True
    return False


# ---------------------------------------------------------------- 已用单位


def period_used_days(*, total_units: int, starts_at: datetime | None, ends_at: datetime | None, at: datetime,
                     pending_freeze_days: int = 0) -> int:
    """期限卡已用天数 = 总天数 − 剩余天数；剩余天数 = ⌈(到期 − max(现在, 开始)) / 1 天⌉ − 冻结中尚未过去的天数。"""
    if starts_at is None or ends_at is None:
        return 0
    starts_at, ends_at = aware(starts_at), aware(ends_at)
    remaining = ceil_days((ends_at - max(at, starts_at)).total_seconds()) - pending_freeze_days
    remaining = max(0, min(total_units, remaining))
    return total_units - remaining


# ---------------------------------------------------------------- 退卡公式


def lpr_interest(principal_cents: int, lpr_bp: int, days: int) -> int:
    if principal_cents <= 0 or lpr_bp <= 0 or days <= 0:
        return 0
    return principal_cents * lpr_bp * days // (10000 * 365)


def quote_card_refund(*, paid_cents: int, original_price_cents: int, paid_units: int, total_units: int,
                      used_units: int, untouched: bool, cause: str, paid_at: datetime | None, at: datetime,
                      cooling_off_days: int, fee_bp: int = 0, lpr_bp: int = 0) -> dict:
    if cause not in CAUSES:
        raise AppError(ErrorCode.BAD_REQUEST, "cause must be customer or merchant")
    used_units = max(0, min(used_units, total_units))
    paid_at = aware(paid_at)
    within = paid_at is not None and (at - paid_at).total_seconds() < cooling_off_days * DAY
    fee = interest = 0
    if within and untouched:
        rule, used_value = RULE_COOLING_OFF, 0
    elif cause == CAUSE_MERCHANT:
        rule = RULE_MERCHANT
        used_value = paid_cents * used_units // total_units if total_units else paid_cents
    else:
        rule = RULE_CUSTOMER
        base_units = paid_units or total_units
        used_value = min(paid_cents, original_price_cents * used_units // base_units) if base_units else paid_cents
    refundable = max(0, paid_cents - used_value)
    if rule == RULE_CUSTOMER and fee_bp > 0:
        fee = refundable * fee_bp // 10000
    if rule == RULE_MERCHANT and paid_at is not None:
        interest = lpr_interest(refundable, lpr_bp, ceil_days((at - paid_at).total_seconds()))
    return {
        "rule": rule, "cause": cause, "cooling_off": bool(within), "untouched": untouched,
        "paid_cents": paid_cents, "original_price_cents": original_price_cents,
        "paid_units": paid_units, "gift_units": total_units - paid_units, "total_units": total_units,
        "used_units": used_units, "remaining_units": total_units - used_units,
        "unit_price_discounted_cents": paid_cents // total_units if total_units else 0,
        "unit_price_original_cents": original_price_cents // paid_units if paid_units else 0,
        "used_value_cents": used_value, "refundable_cents": refundable, "fee_cents": fee,
        "refund_cents": refundable - fee, "interest_cents": interest,
        "gift_units_forfeited": max(0, min(total_units - paid_units, total_units - used_units)),
    }
