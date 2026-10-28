"""售后：仅退款 / 退货退款（references/patterns/after-sales.md）。

- 每个订单行的可退数量用计数行 RefundableLine 控制：申请时原子占用（reserved_qty + n <= qty），驳回 / 撤销时释放，
  所以并发申请不会超退。
- 退款金额按订单行实付（已分摊优惠）计算：本次 = 实付 × 占用后数量 // 总数量 − 已占用金额（不小于 0）。
  同时记录已占用数量与金额，驳回后按实际金额释放，所以任意申请、驳回顺序下，全部退完合计都严格等于实付。
- 状态：applied → approved → (return_pending → returned) → refunding → refunded；
  applied → rejected | cancelled；approved / return_pending → cancelled；refunding → refund_failed → refunding。
- 真正的退款由支付模块执行：进入 refunding 时写发件箱事件，支付退款结果回来后调用 on_refund_result。
本模块不 commit。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import DateTime, Integer, String, select, update
from sqlalchemy.orm import Mapped, Session, mapped_column

from luzback import AppError, Base, ErrorCode, StateMachine, enqueue_event

REFUND_ONLY = "refund_only"
RETURN_REFUND = "return_refund"

APPLIED, APPROVED, REJECTED, CANCELLED = "applied", "approved", "rejected", "cancelled"
RETURN_PENDING, RETURNED = "return_pending", "returned"
REFUNDING, REFUNDED, REFUND_FAILED = "refunding", "refunded", "refund_failed"

AFTER_SALE_FLOW = StateMachine(
    "after_sale",
    {
        APPLIED: {APPROVED, REJECTED, CANCELLED},
        APPROVED: {RETURN_PENDING, REFUNDING, CANCELLED},
        RETURN_PENDING: {RETURNED, CANCELLED},
        RETURNED: {REFUNDING},
        REFUNDING: {REFUNDED, REFUND_FAILED},
        REFUND_FAILED: {REFUNDING},
    },
    terminal=frozenset({REJECTED, CANCELLED, REFUNDED}),
)
RELEASES_QUOTA = {REJECTED, CANCELLED}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


class RefundableLine(Base):
    """订单行的退款额度；订单支付成功时写入（数量、实付金额来自下单快照与分摊结果）。"""

    __tablename__ = "refundable_lines"

    id: Mapped[int] = mapped_column(primary_key=True)  # = order_items.id
    order_id: Mapped[int] = mapped_column(Integer, index=True)
    user_id: Mapped[int] = mapped_column(Integer, index=True)
    qty: Mapped[int] = mapped_column(Integer)
    paid_cents: Mapped[int] = mapped_column(Integer)
    reserved_qty: Mapped[int] = mapped_column(Integer, default=0)  # 已申请（含处理中与已退）的数量
    reserved_cents: Mapped[int] = mapped_column(Integer, default=0)  # 已申请的金额
    paid_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class AfterSale(Base):
    __tablename__ = "after_sales"

    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(Integer, index=True)
    user_id: Mapped[int] = mapped_column(Integer, index=True)
    kind: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(16), default=APPLIED, index=True)
    amount_cents: Mapped[int] = mapped_column(Integer)
    reason: Mapped[str] = mapped_column(String(200))
    reject_reason: Mapped[str | None] = mapped_column(String(200), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class AfterSaleLine(Base):
    __tablename__ = "after_sale_lines"

    id: Mapped[int] = mapped_column(primary_key=True)
    after_sale_id: Mapped[int] = mapped_column(Integer, index=True)
    line_id: Mapped[int] = mapped_column(Integer, index=True)
    qty: Mapped[int] = mapped_column(Integer)
    amount_cents: Mapped[int] = mapped_column(Integer)


def apply(
    db: Session, user_id: int, order_id: int, lines: dict[int, int], kind: str, reason: str, now: datetime,
    *, window_days: int = 7,
) -> AfterSale:
    """用户申请售后。lines={订单行ID: 数量}。超出可退数量、超出售后期或非本人订单时拒绝。"""
    if kind not in (REFUND_ONLY, RETURN_REFUND) or not lines or not reason.strip():
        raise AppError(ErrorCode.BAD_REQUEST, "invalid after-sale request")
    total = 0
    detail: list[tuple[int, int, int]] = []
    for line_id in sorted(lines):
        qty = lines[line_id]
        line = db.get(RefundableLine, line_id)
        if line is None or line.user_id != user_id or line.order_id != order_id:
            raise AppError(ErrorCode.NOT_FOUND, "order line not found")
        if now > _aware(line.paid_at) + timedelta(days=window_days):
            raise AppError(ErrorCode.BUSINESS_RULE, "after-sale window closed", data={"window_days": window_days})
        before, before_cents = line.reserved_qty, line.reserved_cents
        if qty <= 0 or before + qty > line.qty:
            raise AppError(ErrorCode.CONFLICT, "quantity exceeds refundable", data={"line_id": line_id})
        amount = max(0, line.paid_cents * (before + qty) // line.qty - before_cents)
        result = db.execute(
            update(RefundableLine)
            .where(RefundableLine.id == line_id, RefundableLine.reserved_qty == before,
                   RefundableLine.reserved_cents == before_cents)
            .values(reserved_qty=before + qty, reserved_cents=before_cents + amount)
        )
        if result.rowcount != 1:
            raise AppError(ErrorCode.CONFLICT, "refundable quantity changed concurrently, retry", data={"line_id": line_id})
        detail.append((line_id, qty, amount))
        total += amount
    case = AfterSale(order_id=order_id, user_id=user_id, kind=kind, status=APPLIED, amount_cents=total, reason=reason)
    db.add(case)
    db.flush()
    for line_id, qty, amount in detail:
        db.add(AfterSaleLine(after_sale_id=case.id, line_id=line_id, qty=qty, amount_cents=amount))
    enqueue_event(db, "after_sale.applied", {"after_sale_id": case.id, "order_id": order_id})
    db.flush()
    return case


def _release_quota(db: Session, case: AfterSale) -> None:
    for line in db.execute(select(AfterSaleLine).where(AfterSaleLine.after_sale_id == case.id)).scalars():
        db.execute(update(RefundableLine)
                   .where(RefundableLine.id == line.line_id, RefundableLine.reserved_qty >= line.qty,
                          RefundableLine.reserved_cents >= line.amount_cents)
                   .values(reserved_qty=RefundableLine.reserved_qty - line.qty,
                           reserved_cents=RefundableLine.reserved_cents - line.amount_cents))


def _move(db: Session, case_id: int, target: str, **values) -> AfterSale:
    case = db.get(AfterSale, case_id)
    if case is None:
        raise AppError(ErrorCode.NOT_FOUND, "after-sale not found")
    AFTER_SALE_FLOW.advance(db, AfterSale, case.id, case.status, target, **values)
    db.refresh(case)
    if target in RELEASES_QUOTA:
        _release_quota(db, case)
    if target == REFUNDING:
        # 退款单号固定为售后单号，失败重试沿用同一单号，不会重复退款（RFD-002）
        enqueue_event(db, "after_sale.refund_requested",
                      {"after_sale_id": case.id, "order_id": case.order_id, "amount_cents": case.amount_cents,
                       "out_refund_no": f"AS{case.id}"})
    db.flush()
    return case


def approve(db: Session, case_id: int) -> AfterSale:
    case = _move(db, case_id, APPROVED)
    return _move(db, case_id, REFUNDING if case.kind == REFUND_ONLY else RETURN_PENDING)


def reject(db: Session, case_id: int, reason: str) -> AfterSale:
    if not reason.strip():
        raise AppError(ErrorCode.BAD_REQUEST, "reject reason required")
    return _move(db, case_id, REJECTED, reject_reason=reason)


def cancel(db: Session, case_id: int, user_id: int) -> AfterSale:
    case = db.get(AfterSale, case_id)
    if case is None or case.user_id != user_id:
        raise AppError(ErrorCode.NOT_FOUND, "after-sale not found")
    return _move(db, case_id, CANCELLED)


def confirm_returned(db: Session, case_id: int) -> AfterSale:
    """仓库 / 门店确认收到退货后进入退款。"""
    _move(db, case_id, RETURNED)
    return _move(db, case_id, REFUNDING)


def on_refund_result(db: Session, case_id: int, success: bool) -> AfterSale:
    """支付模块退款终态回调；重复通知幂等。"""
    case = db.get(AfterSale, case_id)
    if case is None:
        raise AppError(ErrorCode.NOT_FOUND, "after-sale not found")
    target = REFUNDED if success else REFUND_FAILED
    if case.status == target:
        return case
    return _move(db, case_id, target)


def retry_refund(db: Session, case_id: int) -> AfterSale:
    return _move(db, case_id, REFUNDING)
