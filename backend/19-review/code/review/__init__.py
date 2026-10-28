"""评价模块：只有本人、已完成的订单可以评价；每笔订单只能评价一次；内容先进入待审核。

评价与订单通过 order_id 关联，状态校验读取 order 模块的 Order 表（见 MODULE.md §1）。
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import Integer, String, Text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Mapped, Session, mapped_column

from luzback import AppError, Base, ErrorCode
from order import COMPLETED, Order

MAX_CONTENT = 500
BANNED_WORDS = {"广告", "微信号"}  # 示例：实际接入内容安全服务


class Review(Base):
    __tablename__ = "reviews"

    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(Integer, unique=True)  # 每笔订单仅一条评价
    user_id: Mapped[int] = mapped_column(Integer, index=True)
    rating: Mapped[int] = mapped_column(Integer)
    content: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(16), default="pending")  # pending | visible | hidden
    created_at: Mapped[datetime] = mapped_column(default=lambda: datetime.now(timezone.utc))


def _moderate(content: str) -> str:
    """返回审核结果：visible 或 pending。命中敏感词转人工审核，不直接拒绝。"""
    return "pending" if any(word in content for word in BANNED_WORDS) else "visible"


def create_review(db: Session, user_id: int, order_id: int, rating: int, content: str) -> Review:
    if not 1 <= rating <= 5:
        raise AppError(ErrorCode.BAD_REQUEST, "rating must be 1-5")
    if len(content) > MAX_CONTENT:
        raise AppError(ErrorCode.BAD_REQUEST, f"content longer than {MAX_CONTENT}")
    order = db.get(Order, order_id)
    if order is None:
        raise AppError(ErrorCode.NOT_FOUND, "order not found")
    if order.user_id != user_id:
        raise AppError(ErrorCode.FORBIDDEN, "not your order")
    if order.status != COMPLETED:
        raise AppError(ErrorCode.BUSINESS_RULE, "only completed orders can be reviewed")
    review = Review(order_id=order_id, user_id=user_id, rating=rating, content=content,
                    status=_moderate(content))
    db.add(review)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise AppError(ErrorCode.CONFLICT, "order already reviewed") from exc
    return review
