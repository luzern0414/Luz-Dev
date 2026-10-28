"""库存扣减与回补：带条件的原子 UPDATE，由数据库裁决，不在应用内先读后写。

    UPDATE skus SET stock = stock - :qty, version = version + 1
    WHERE id = :sku_id AND stock >= :qty AND status = 'on_sale'

rowcount == 1 才算扣减成功。传入 ref（业务引用，如 after_sales:12）时同一 (ref, sku, 动作) 只生效一次：
先查流水，再在保存点内写流水（唯一约束兜底并发），流水写成功才改库存。本模块函数不 commit。
"""
from __future__ import annotations

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from luzback import AppError, ErrorCode

from .models import STATUS_ON_SALE, Sku, StockLedger


def _claim(db: Session, ref: str, sku_id: int, reason: str, delta: int) -> bool:
    """登记 (ref, sku, reason) 流水；已登记返回 False（重放 / 并发的另一方已执行）。"""
    ref = str(ref)[:64]
    if db.execute(select(StockLedger.id).where(StockLedger.ref == ref, StockLedger.sku_id == sku_id,
                                               StockLedger.reason == reason)).first() is not None:
        return False
    try:
        with db.begin_nested():
            db.add(StockLedger(sku_id=sku_id, delta=delta, reason=reason, ref=ref))
            db.flush()
    except IntegrityError:
        return False
    return True


def deduct(db: Session, sku_id: int, qty: int, ref: str | None = None) -> bool:
    """扣减库存。失败抛 OUT_OF_STOCK（调用方回滚整个事务）。带 ref 时幂等：已扣过返回 False。"""
    if qty <= 0:
        raise AppError(ErrorCode.BAD_REQUEST, "qty must be positive")
    if ref is not None and not _claim(db, ref, sku_id, "deduct", -qty):
        return False
    result = db.execute(update(Sku).where(Sku.id == sku_id, Sku.stock >= qty, Sku.status == STATUS_ON_SALE)
                        .values(stock=Sku.stock - qty, version=Sku.version + 1))
    if result.rowcount != 1:
        raise AppError(ErrorCode.OUT_OF_STOCK, f"sku {sku_id} insufficient stock", data={"sku_id": sku_id})
    return True


def restore(db: Session, sku_id: int, qty: int, ref: str | None = None) -> bool:
    """回补库存（退货入库、取消）。带 ref 时同一 ref 对同一 SKU 只回补一次，重复调用返回 False。

    不带 ref 的调用不去重（旧调用方式），由调用方的状态机保证只执行一次。
    """
    if qty <= 0:
        raise AppError(ErrorCode.BAD_REQUEST, "qty must be positive")
    if ref is not None and not _claim(db, ref, sku_id, "restore", qty):
        return False
    result = db.execute(update(Sku).where(Sku.id == sku_id).values(stock=Sku.stock + qty, version=Sku.version + 1))
    if result.rowcount != 1:
        raise AppError(ErrorCode.NOT_FOUND, f"sku {sku_id} not found", data={"sku_id": sku_id})
    return True


def restocked(db: Session, ref: str, sku_id: int) -> bool:
    """该业务引用是否已对该 SKU 回补过。"""
    return db.execute(select(StockLedger.id).where(StockLedger.ref == str(ref)[:64], StockLedger.sku_id == sku_id,
                                                   StockLedger.reason == "restore")).first() is not None
