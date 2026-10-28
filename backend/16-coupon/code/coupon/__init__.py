"""营销与优惠券模块：券模板库存、每人限领（槽位唯一约束）、门槛与抵扣计算、核销防重。

- 库存：条件 UPDATE remaining > 0，原子领取。
- 每人限领：UserCoupon 的 (template_id, user_id, slot) 唯一约束，slot 从 1 到 per_user_limit。并发领取时唯一约束兜底。
- 核销：条件 UPDATE status = 'unused' -> 'used'，同一张券只能被一笔订单核销。
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import Integer, String, UniqueConstraint, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Mapped, Session, mapped_column

from luzback import AppError, Base, ErrorCode


def _aware(dt: datetime) -> datetime:
    """SQLite 读回的 datetime 不带时区，统一按 UTC 处理。"""
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


class CouponTemplate(Base):
    __tablename__ = "coupon_templates"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(64))
    face_cents: Mapped[int] = mapped_column(Integer)           # 面额
    threshold_cents: Mapped[int] = mapped_column(Integer, default=0)  # 满减门槛，0 表示无门槛
    remaining_stock: Mapped[int] = mapped_column(Integer)
    per_user_limit: Mapped[int] = mapped_column(Integer, default=1)
    valid_until: Mapped[datetime | None] = mapped_column(nullable=True)


class UserCoupon(Base):
    __tablename__ = "user_coupons"
    __table_args__ = (UniqueConstraint("template_id", "user_id", "slot", name="uq_user_coupon_slot"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    template_id: Mapped[int] = mapped_column(Integer, index=True)
    user_id: Mapped[int] = mapped_column(Integer, index=True)
    slot: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(16), default="unused")  # unused | used | expired
    order_id: Mapped[int | None] = mapped_column(Integer, nullable=True)


def claim(db: Session, user_id: int, template_id: int, now: datetime | None = None) -> UserCoupon:
    now = now or datetime.now(timezone.utc)
    template = db.get(CouponTemplate, template_id)
    if template is None:
        raise AppError(ErrorCode.NOT_FOUND, "coupon template not found")
    if template.valid_until is not None and _aware(template.valid_until) < now:
        raise AppError(ErrorCode.BUSINESS_RULE, "coupon activity ended")

    owned = db.execute(
        select(func.count()).select_from(UserCoupon).where(
            UserCoupon.template_id == template_id, UserCoupon.user_id == user_id
        )
    ).scalar_one()
    if owned >= template.per_user_limit:
        raise AppError(ErrorCode.BUSINESS_RULE, "per-user limit reached")

    stock = db.execute(
        update(CouponTemplate)
        .where(CouponTemplate.id == template_id, CouponTemplate.remaining_stock > 0)
        .values(remaining_stock=CouponTemplate.remaining_stock - 1)
    )
    if stock.rowcount != 1:
        db.rollback()
        raise AppError(ErrorCode.BUSINESS_RULE, "coupon sold out")

    coupon = UserCoupon(template_id=template_id, user_id=user_id, slot=owned + 1)
    db.add(coupon)
    try:
        db.flush()
    except IntegrityError as exc:
        db.rollback()  # 并发下同一用户槽位被抢占，整体回滚（库存也一并回滚）
        raise AppError(ErrorCode.BUSINESS_RULE, "per-user limit reached") from exc
    db.commit()
    return coupon


def discount_for(template: CouponTemplate, order_amount_cents: int) -> int:
    """计算抵扣金额；未达门槛返回 0，抵扣不超过订单金额。"""
    if order_amount_cents < template.threshold_cents:
        return 0
    return min(template.face_cents, order_amount_cents)


def redeem(db: Session, user_coupon_id: int, order_id: int) -> None:
    """核销优惠券。只允许 unused -> used。调用方 commit。"""
    result = db.execute(
        update(UserCoupon)
        .where(UserCoupon.id == user_coupon_id, UserCoupon.status == "unused")
        .values(status="used", order_id=order_id)
    )
    if result.rowcount != 1:
        raise AppError(ErrorCode.CONFLICT, "coupon not usable")
    # 条件 UPDATE 不经过 ORM 对象，这里刷新同一会话中已加载的对象，避免读到旧值
    coupon = db.get(UserCoupon, user_coupon_id)
    if coupon is not None:
        db.refresh(coupon)


# ---------------------------------------------------------------- 下单锁定（references/patterns/coupon.md）
# unused → locked（下单）→ used（支付成功）；locked → unused（取消 / 超时）；used → unused（退款且商户允许退回）。
from luzback import StateMachine  # noqa: E402

UNUSED, LOCKED, USED = "unused", "locked", "used"
COUPON_FLOW = StateMachine("user_coupon", {UNUSED: {LOCKED, USED}, LOCKED: {USED, UNUSED}, USED: {UNUSED}})


def lock(db: Session, user_coupon_id: int, user_id: int, order_id: int, now: datetime | None = None) -> UserCoupon:
    """下单事务内把券锁定到订单。同一张券并发用于两笔订单只有一笔成功。调用方 commit。"""
    coupon = db.get(UserCoupon, user_coupon_id)
    if coupon is None or coupon.user_id != user_id:
        raise AppError(ErrorCode.NOT_FOUND, "coupon not found")
    if coupon.status == LOCKED and coupon.order_id == order_id:
        return coupon  # 同一订单重复提交
    template = db.get(CouponTemplate, coupon.template_id)
    if template is not None and template.valid_until is not None and _aware(template.valid_until) < (now or datetime.now(timezone.utc)):
        raise AppError(ErrorCode.BUSINESS_RULE, "coupon expired")
    COUPON_FLOW.advance(db, UserCoupon, coupon.id, UNUSED, LOCKED, order_id=order_id)
    db.refresh(coupon)
    return coupon


def unlock(db: Session, user_coupon_id: int, order_id: int) -> None:
    """订单取消或超时关闭时解锁；不是该订单锁定的券不处理（幂等）。"""
    db.execute(update(UserCoupon)
               .where(UserCoupon.id == user_coupon_id, UserCoupon.status == LOCKED, UserCoupon.order_id == order_id)
               .values(status=UNUSED, order_id=None))


def confirm_use(db: Session, user_coupon_id: int, order_id: int) -> None:
    """支付成功核销：只核销由该订单锁定的券。重复调用幂等。"""
    coupon = db.get(UserCoupon, user_coupon_id)
    if coupon is not None and coupon.status == USED and coupon.order_id == order_id:
        return
    COUPON_FLOW.advance(db, UserCoupon, user_coupon_id, LOCKED, USED, extra_where=(UserCoupon.order_id == order_id,))


def return_coupon(db: Session, user_coupon_id: int, order_id: int) -> None:
    """全额退款后按商户策略退回券（是否退回由方案决定，RFD-004）。"""
    COUPON_FLOW.advance(db, UserCoupon, user_coupon_id, USED, UNUSED, extra_where=(UserCoupon.order_id == order_id,),
                        order_id=None)
