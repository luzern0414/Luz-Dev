"""储值纯函数规则：消费拆分、FIFO 批次分配、批次退款公式、LPR 利息。

退款公式（法释〔2025〕4号 第十四、十五、十六、十八、十九、二十条），以一个充值批次为单位：
- A = 该批实付本金，B = 该批赠送金，p = 该批剩余本金，b = 该批剩余赠送金；已兑付 = (A − p) + (B − b)。
- 七日规则：付款之日起 cooling_off_days 日内、该批未兑付（p == A 且 b == B）→ 全额退 A（不论原因）。
- 经营者原因：已兑付按折扣价计价 = ⌊已兑付 × A / (A + B)⌋；可退 = A − 该值；另计一年期 LPR 利息。
- 消费者原因：已兑付按打折前价格（面值）计价，超过实付的部分不再追要；可退 = max(0, A − 已兑付)；可扣手续费。
- 可退金额永远不超过该批剩余本金 p（剩余赠送金 b 全部作废）。
"""
from __future__ import annotations

from datetime import datetime

from luzback import AppError, ErrorCode

PROPORTIONAL = "proportional"
PRINCIPAL_FIRST = "principal_first"

CAUSE_CUSTOMER = "customer"
CAUSE_MERCHANT = "merchant"
CAUSES = (CAUSE_CUSTOMER, CAUSE_MERCHANT)

RULE_COOLING_OFF = "cooling_off_full"
RULE_CUSTOMER = "customer_original_price"
RULE_MERCHANT = "merchant_discounted"


def split(amount: int, principal: int, bonus: int, policy: str = PROPORTIONAL) -> tuple[int, int]:
    """把消费金额拆成 (本金, 赠送金)。proportional：赠送金向下取整，本金多扣不超过 1 分。"""
    if amount <= 0:
        raise AppError(ErrorCode.BAD_REQUEST, "amount must be positive")
    if amount > principal + bonus:
        raise AppError(ErrorCode.INSUFFICIENT_BALANCE, "insufficient balance")
    if policy == PRINCIPAL_FIRST:
        from_principal = min(amount, principal)
        return from_principal, amount - from_principal
    if policy != PROPORTIONAL:
        raise ValueError(f"unknown policy {policy}")
    from_bonus = amount * bonus // (principal + bonus)
    from_principal = amount - from_bonus
    if from_principal > principal:  # 极端取整时回退到赠送金
        from_bonus += from_principal - principal
        from_principal = principal
    return from_principal, from_bonus


def allocate_fifo(amount: int, buckets: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """按顺序从 [(batch_id, 可用)] 中取 amount；返回 [(batch_id, 取用)]。不足抛 4224。"""
    out: list[tuple[int, int]] = []
    left = amount
    for batch_id, available in buckets:
        if left <= 0:
            break
        take = min(left, available)
        if take > 0:
            out.append((batch_id, take))
            left -= take
    if left > 0:
        raise AppError(ErrorCode.INSUFFICIENT_BALANCE, "batch balance insufficient", {"short_cents": left})
    return out


def lpr_interest(principal_cents: int, lpr_bp: int, days: int) -> int:
    """一年期 LPR 单利，按日计（年 365 天），向下取整到分。"""
    if principal_cents <= 0 or lpr_bp <= 0 or days <= 0:
        return 0
    return principal_cents * lpr_bp * days // (10000 * 365)


def days_between(start: datetime, end: datetime) -> int:
    """自然日差（不足一天按一天），用于利息与七日判断。"""
    seconds = (end - start).total_seconds()
    if seconds <= 0:
        return 0
    return int(-(-seconds // 86400))


def quote_batch_refund(*, amount: int, bonus: int, principal_remaining: int, bonus_remaining: int, cause: str,
                       paid_at: datetime | None, at: datetime, cooling_off_days: int, fee_bp: int = 0,
                       lpr_bp: int = 0) -> dict:
    if cause not in CAUSES:
        raise AppError(ErrorCode.BAD_REQUEST, "cause must be customer or merchant")
    used = (amount - principal_remaining) + (bonus - bonus_remaining)
    untouched = principal_remaining == amount and bonus_remaining == bonus
    within = paid_at is not None and (at - paid_at).total_seconds() < cooling_off_days * 86400
    fee = interest = 0
    if within and untouched:
        rule, used_value, refundable = RULE_COOLING_OFF, 0, amount
    elif cause == CAUSE_MERCHANT:
        rule = RULE_MERCHANT
        used_value = used * amount // (amount + bonus) if amount + bonus else 0
        refundable = amount - used_value
    else:
        rule = RULE_CUSTOMER
        used_value = min(used, amount)
        refundable = amount - used_value
    refundable = max(0, min(refundable, principal_remaining))
    if rule == RULE_CUSTOMER and fee_bp > 0:
        fee = refundable * fee_bp // 10000
    if rule == RULE_MERCHANT and paid_at is not None:
        interest = lpr_interest(refundable - fee, lpr_bp, days_between(paid_at, at))
    refund = refundable - fee
    return {
        "rule": rule, "cause": cause, "cooling_off": bool(within), "untouched": untouched,
        "amount_cents": amount, "bonus_cents": bonus,
        "principal_remaining_cents": principal_remaining, "bonus_remaining_cents": bonus_remaining,
        "used_cents": used, "used_value_cents": used_value,
        "refundable_cents": refundable, "fee_cents": fee, "refund_cents": refund, "interest_cents": interest,
        "bonus_forfeit_cents": bonus_remaining, "retained_cents": principal_remaining - refundable,
        "hold_principal_cents": principal_remaining, "hold_bonus_cents": bonus_remaining,
    }
