"""纯函数规则：单号、文本清洗、状态机、退款限制、查询节奏、账单解析。便于单测，不访问数据库与网络。"""
from __future__ import annotations

import csv
import io
import re
import secrets
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any
from zoneinfo import ZoneInfo

from luzback import AppError, ErrorCode, StateMachine

# ---------------------------------------------------------------- 常量

GOODS_PHYSICAL = "physical"
GOODS_OFFLINE_SERVICE = "offline_service"
GOODS_VIRTUAL_CONTENT = "virtual_content"
GOODS_KINDS = (GOODS_PHYSICAL, GOODS_OFFLINE_SERVICE, GOODS_VIRTUAL_CONTENT)
WECHAT_PAY_GOODS_KINDS = (GOODS_PHYSICAL, GOODS_OFFLINE_SERVICE)

PAY_CREATED = "created"     # 本地已建单，尚未拿到 prepay_id
PAY_PAYING = "paying"       # 已拿到 prepay_id，等待用户支付
PAY_PAID = "paid"
PAY_CLOSED = "closed"
PAY_FAILED = "failed"
PAY_OPEN = (PAY_CREATED, PAY_PAYING)
PAY_STATUSES = (PAY_CREATED, PAY_PAYING, PAY_PAID, PAY_CLOSED, PAY_FAILED)

REFUND_PENDING = "pending"         # 本地已建单、额度已占用，渠道尚未受理（含可重试失败）
REFUND_PROCESSING = "processing"   # 渠道已受理
REFUND_SUCCEEDED = "succeeded"
REFUND_ABNORMAL = "abnormal"       # 原路退回失败，需人工或异常退款
REFUND_CLOSED = "closed"           # 渠道关闭（退款失败），需换新单号重新发起
REFUND_FAILED = "failed"           # 渠道明确拒绝（不可重试）
REFUND_HOLDING = (REFUND_PENDING, REFUND_PROCESSING, REFUND_ABNORMAL, REFUND_SUCCEEDED)  # 占用可退额度的状态
REFUND_STATUSES = (REFUND_PENDING, REFUND_PROCESSING, REFUND_SUCCEEDED, REFUND_ABNORMAL, REFUND_CLOSED, REFUND_FAILED)

# 支付单：closed → paid 只在“关单后仍收到支付成功事实”时发生（记录事实 + 人工 + 自动退款）
PAYMENT_MACHINE = StateMachine(
    "payment",
    {
        PAY_CREATED: {PAY_PAYING, PAY_PAID, PAY_CLOSED, PAY_FAILED},
        PAY_PAYING: {PAY_PAID, PAY_CLOSED, PAY_FAILED},
        PAY_CLOSED: {PAY_PAID},
    },
    terminal=frozenset({PAY_PAID, PAY_FAILED}),
)

REFUND_MACHINE = StateMachine(
    "refund",
    {
        REFUND_PENDING: {REFUND_PROCESSING, REFUND_SUCCEEDED, REFUND_ABNORMAL, REFUND_CLOSED, REFUND_FAILED},
        REFUND_PROCESSING: {REFUND_SUCCEEDED, REFUND_ABNORMAL, REFUND_CLOSED},
        REFUND_ABNORMAL: {REFUND_PROCESSING, REFUND_SUCCEEDED, REFUND_CLOSED},
    },
    terminal=frozenset({REFUND_SUCCEEDED, REFUND_CLOSED, REFUND_FAILED}),
)

# 退款渠道状态 → 本地状态（4012791904 / 4013071031）
REFUND_STATUS_MAP = {
    "SUCCESS": REFUND_SUCCEEDED,
    "PROCESSING": REFUND_PROCESSING,
    "ABNORMAL": REFUND_ABNORMAL,
    "CLOSED": REFUND_CLOSED,
}
REFUND_EVENT_STATUS = {"REFUND.SUCCESS": "SUCCESS", "REFUND.ABNORMAL": "ABNORMAL", "REFUND.CLOSED": "CLOSED"}

OUT_TRADE_NO_RE = re.compile(r"^[0-9A-Za-z_\-|*]{6,32}$")
OUT_REFUND_NO_RE = re.compile(r"^[0-9A-Za-z_\-|*@]{1,64}$")
PREFIX_RE = re.compile(r"^[0-9A-Za-z]{0,4}$")
_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"  # Crockford base32，无易混字符

RETRY_BACKOFF_S = (60, 300, 1800, 7200, 43200)  # 与发件箱一致：1m / 5m / 30m / 2h / 12h


# ---------------------------------------------------------------- 单号


def new_out_trade_no(prefix: str, now: datetime, tz: str = "Asia/Shanghai") -> str:
    """前缀(≤4) + yyMMddHHmmss(本地时区) + 10 位随机 = ≤ 26 位，满足 6–32 位 [0-9A-Za-z_-|*]。"""
    if not PREFIX_RE.match(prefix):
        raise ValueError("out_trade_no prefix must be 0-4 alphanumerics")
    stamp = now.astimezone(ZoneInfo(tz)).strftime("%y%m%d%H%M%S")
    value = prefix + stamp + "".join(secrets.choice(_ALPHABET) for _ in range(10))
    assert OUT_TRADE_NO_RE.match(value)
    return value


def out_refund_no(out_trade_no: str, seq: int) -> str:
    """R + 商户订单号 + 两位序号（1–50）：同一支付单内唯一、可读，≤ 35 字节。"""
    value = f"R{out_trade_no}{seq:02d}"
    if not OUT_REFUND_NO_RE.match(value):
        raise ValueError("invalid out_refund_no")
    return value


# ---------------------------------------------------------------- 文本与时间


def sanitize_text(text: str | None, max_chars: int) -> str:
    """去掉 4 字节字符（emoji 等，官方只支持 UTF-8 1~3 字节）与控制字符，截断到 max_chars。"""
    cleaned = "".join(ch for ch in (text or "") if ord(ch) <= 0xFFFF and (ch.isprintable() or ch == " "))
    return cleaned.strip()[:max_chars]


def aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def rfc3339(dt: datetime, tz: str = "Asia/Shanghai") -> str:
    return aware(dt).astimezone(ZoneInfo(tz)).replace(microsecond=0).isoformat()


def parse_rfc3339(text: str | None) -> datetime | None:
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


def local_day_bounds(day: date, tz: str = "Asia/Shanghai") -> tuple[datetime, datetime]:
    start = datetime.combine(day, datetime.min.time(), tzinfo=ZoneInfo(tz))
    return start.astimezone(timezone.utc), (start + timedelta(days=1)).astimezone(timezone.utc)


def mask_openid(openid: str | None) -> str | None:
    if not openid:
        return openid
    return openid[:4] + "****" + openid[-4:] if len(openid) > 8 else "****"


# ---------------------------------------------------------------- 支付规则


def assert_wechat_pay_allowed(goods_kind: str) -> None:
    """运营规范 2.5.13 / 2.14.7：纯线上虚拟商品必须走小程序虚拟支付，普通微信支付只收实物与线下服务。"""
    if goods_kind not in GOODS_KINDS:
        raise AppError(ErrorCode.BAD_REQUEST, f"goods_kind must be one of {', '.join(GOODS_KINDS)}")
    if goods_kind not in WECHAT_PAY_GOODS_KINDS:
        raise AppError(ErrorCode.BUSINESS_RULE,
                       "线上虚拟商品须使用小程序虚拟支付，不能使用微信支付",
                       data={"reason": "virtual_goods_require_virtual_payment", "goods_kind": goods_kind})


def validate_expire(expires_at: datetime, now: datetime) -> None:
    """time_expire：不早于下单后 1 分钟、不晚于 15 天（4012791897 会自动调整，这里提前拒绝避免与本地时限不一致）。"""
    expires_at = aware(expires_at)
    if expires_at < now + timedelta(minutes=1):
        raise AppError(ErrorCode.BUSINESS_RULE, "支付截止时间已到或过近", data={"reason": "expire_too_soon"})
    if expires_at > now + timedelta(days=15):
        raise AppError(ErrorCode.BAD_REQUEST, "支付截止时间不能晚于 15 天后")


def next_query_delay(query_count: int, schedule: tuple[int, ...]) -> int:
    """主动查单节奏（官方方案一）：第 n 次查询后的等待秒数，超出列表后保持最后一个间隔。"""
    if not schedule:
        return 60
    return schedule[min(query_count, len(schedule) - 1)]


# ---------------------------------------------------------------- 退款规则


@dataclass(frozen=True)
class RefundLimits:
    max_times: int = 50
    min_interval_s: int = 60
    window_days: int = 365


def refund_block_reason(*, status: str, amount_cents: int, refunded_cents: int, refund_count: int,
                        last_refund_at: datetime | None, paid_at: datetime | None, amount: int,
                        now: datetime, limits: RefundLimits) -> AppError | None:
    """条件 UPDATE 失败后给出准确原因（调用方用最新一行重新判断）。返回 None 表示规则允许（并发竞争失败）。"""
    if status != PAY_PAID:
        return AppError(ErrorCode.CONFLICT, "支付单未支付，不能退款", data={"reason": "not_paid", "status": status})
    if amount <= 0:
        return AppError(ErrorCode.BAD_REQUEST, "退款金额必须大于 0")
    if refunded_cents + amount > amount_cents:
        return AppError(ErrorCode.BUSINESS_RULE, "退款金额超过可退金额",
                        data={"reason": "exceeds_refundable", "refundable_cents": amount_cents - refunded_cents})
    if refund_count >= limits.max_times:
        return AppError(ErrorCode.LIMIT_REACHED, f"一笔支付最多退款 {limits.max_times} 次",
                        data={"reason": "refund_times_exceeded"})
    paid_at = aware(paid_at)
    if paid_at is None or paid_at < now - timedelta(days=limits.window_days):
        return AppError(ErrorCode.BUSINESS_RULE, f"支付超过 {limits.window_days} 天，不能原路退款",
                        data={"reason": "refund_window_expired"})
    last = aware(last_refund_at)
    if last is not None and last > now - timedelta(seconds=limits.min_interval_s):
        wait = int((last + timedelta(seconds=limits.min_interval_s) - now).total_seconds()) + 1
        return AppError(ErrorCode.BUSINESS_RULE, "两次部分退款需间隔 1 分钟", data={"reason": "refund_interval", "retry_after_s": wait})
    return None


def refund_query_delay(since_submit_s: float, query_count: int, *, fast_s: int = 60, fast_window_s: int = 300,
                       backoff_min: tuple[int, ...] = (5, 10, 20, 30)) -> int:
    """退款查询节奏（4012791904）：提交后每 1 分钟；超过 5 分钟仍处理中按 5、10、20、30 分钟衰减，之后保持 30 分钟。"""
    if since_submit_s < fast_window_s:
        return fast_s
    fast_queries = max(1, fast_window_s // max(1, fast_s))
    index = max(0, query_count - fast_queries)
    return backoff_min[min(index, len(backoff_min) - 1)] * 60


def retry_delay(attempts: int) -> int:
    return RETRY_BACKOFF_S[min(max(attempts, 1) - 1, len(RETRY_BACKOFF_S) - 1)]


# ---------------------------------------------------------------- 账单


def yuan_to_cents(text: str | None) -> int | None:
    if text is None:
        return None
    text = text.strip()
    if not text:
        return None
    try:
        value = Decimal(text)
    except InvalidOperation:
        return None
    return int((value * 100).quantize(Decimal("1")))


def _cell(value: str) -> str:
    value = value.strip()
    return value[1:] if value.startswith("`") else value


def parse_trade_bill(content: str) -> tuple[list[dict[str, str]], dict[str, str]]:
    """解析交易账单 CSV（4013071246）：每个字段前有反引号；明细表头、明细行、汇总表头、汇总行。"""
    content = content.lstrip("﻿")
    rows = [row for row in csv.reader(io.StringIO(content)) if any(cell.strip() for cell in row)]
    if not rows:
        return [], {}
    header = [_cell(c) for c in rows[0]]
    details: list[dict[str, str]] = []
    summary: dict[str, str] = {}
    index = 1
    while index < len(rows):
        cells = [_cell(c) for c in rows[index]]
        if cells and cells[0].startswith("总交易单数"):
            if index + 1 < len(rows):
                summary = dict(zip(cells, (_cell(c) for c in rows[index + 1])))
            break
        details.append(dict(zip(header, cells)))
        index += 1
    return details, summary


@dataclass
class BillLine:
    kind: str              # payment | refund
    out_trade_no: str
    transaction_id: str
    out_refund_no: str | None
    refund_id: str | None
    amount_cents: int | None
    status: str


def bill_lines(details: list[dict[str, str]]) -> list[BillLine]:
    """账单明细 → 支付行与退款行。订单金额优先用“订单金额”，旧格式回退“应结订单金额 / 总金额”。"""
    out: list[BillLine] = []
    for row in details:
        out_refund = row.get("商户退款单号", "").strip()
        is_refund = bool(out_refund) and out_refund != "0"
        if is_refund:
            amount = yuan_to_cents(row.get("申请退款金额")) if row.get("申请退款金额") else yuan_to_cents(row.get("退款金额"))
            out.append(BillLine("refund", row.get("商户订单号", ""), row.get("微信订单号", ""), out_refund,
                                row.get("微信退款单号"), amount, row.get("退款状态", "")))
        else:
            amount = None
            for column in ("订单金额", "应结订单金额", "总金额"):
                if row.get(column):
                    amount = yuan_to_cents(row[column])
                    break
            out.append(BillLine("payment", row.get("商户订单号", ""), row.get("微信订单号", ""), None, None, amount,
                                row.get("交易状态", "")))
    return out


def as_dict(obj: Any, fields: tuple[str, ...]) -> dict[str, Any]:
    return {name: getattr(obj, name) for name in fields}
