"""库存与超卖防护。核心原则：扣减必须是一条带条件的原子 UPDATE，由数据库裁决，不在应用内先读后写。

    UPDATE skus SET stock = stock - :qty, version = version + 1
    WHERE id = :sku_id AND stock >= :qty AND status = 'on_sale'

rowcount == 1 才算扣减成功；否则库存不足或商品已下架，直接拒绝。
本模块函数不 commit，由调用方（订单服务）与业务写入放在同一个事务中。
"""
from __future__ import annotations

from sqlalchemy import Integer, String, update
from sqlalchemy.orm import Mapped, Session, mapped_column

from luzback import AppError, Base, ErrorCode


class Sku(Base):
    __tablename__ = "skus"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(128))
    price_cents: Mapped[int] = mapped_column(Integer)
    stock: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(16), default="on_sale")  # on_sale | off_sale
    version: Mapped[int] = mapped_column(Integer, default=0)


def deduct(db: Session, sku_id: int, qty: int) -> None:
    """扣减库存。失败抛 OUT_OF_STOCK，调用方应让整个事务回滚。"""
    if qty <= 0:
        raise AppError(ErrorCode.BAD_REQUEST, "qty must be positive")
    result = db.execute(
        update(Sku)
        .where(Sku.id == sku_id, Sku.stock >= qty, Sku.status == "on_sale")
        .values(stock=Sku.stock - qty, version=Sku.version + 1)
    )
    if result.rowcount != 1:
        raise AppError(ErrorCode.OUT_OF_STOCK, f"sku {sku_id} insufficient stock", data={"sku_id": sku_id})


def restore(db: Session, sku_id: int, qty: int) -> None:
    """回补库存（取消订单、支付超时关闭）。同样不 commit。"""
    if qty <= 0:
        raise AppError(ErrorCode.BAD_REQUEST, "qty must be positive")
    db.execute(
        update(Sku).where(Sku.id == sku_id).values(stock=Sku.stock + qty, version=Sku.version + 1)
    )
