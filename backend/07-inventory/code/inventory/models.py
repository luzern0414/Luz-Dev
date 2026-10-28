"""inventory 数据模型（Sku 的唯一定义；其他模块 `from inventory import Sku`）。

- `skus`：库存列（stock / lock_stock / sold_out_today / low_stock_threshold）由本模块持有；
  商品归属（product_id）与规格组合（spec_values_json）由 catalog 写入。
- `stock_reservations`：下单预占（reserved → confirmed | released）。
- `stock_ledger`：带业务引用的库存变化流水；`(ref, sku_id, reason)` 唯一，是扣减 / 回补 / 预占 / 释放去重的最后一道防线。
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from luzback import Base, BigId, BigIdMixin, TimestampMixin, utcnow

STATUS_ON_SALE = "on_sale"
STATUS_OFF_SALE = "off_sale"

RESERVED = "reserved"
CONFIRMED = "confirmed"
RELEASED = "released"


class Sku(Base, BigIdMixin, TimestampMixin):
    __tablename__ = "skus"
    __table_args__ = (UniqueConstraint("product_id", "spec_values_json", name="uq_skus_product_spec"),)

    product_id: Mapped[int | None] = mapped_column(BigId, nullable=True, index=True)
    name: Mapped[str] = mapped_column(String(128), default="")
    spec_values_json: Mapped[str] = mapped_column(String(500), default="{}")  # 规范化 JSON（键排序）
    barcode: Mapped[str | None] = mapped_column(String(64), nullable=True)
    price_cents: Mapped[int] = mapped_column(Integer)
    stock: Mapped[int] = mapped_column(Integer, default=0)
    lock_stock: Mapped[int] = mapped_column(Integer, default=0)  # 预占（reserve 模式）
    low_stock_threshold: Mapped[int] = mapped_column(Integer, default=0)  # 0 = 不预警
    sold_out_today: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(String(16), default=STATUS_ON_SALE)  # on_sale | off_sale
    version: Mapped[int] = mapped_column(Integer, default=0)
    low_alerted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)  # stock.low 去重窗口


class StockReservation(Base):
    """下单预占：每个业务单（ref，如 order:123）对每个 SKU 一条（唯一约束）。"""

    __tablename__ = "stock_reservations"
    __table_args__ = (UniqueConstraint("ref", "sku_id", name="uq_stock_reservation_ref_sku"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    ref: Mapped[str] = mapped_column(String(64), index=True)
    sku_id: Mapped[int] = mapped_column(Integer, index=True)
    qty: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(16), default=RESERVED, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class StockLedger(Base):
    """库存流水（只追加）。delta 为可售库存变化量：预占 / 扣减为负，释放 / 回补为正，调整可正可负。"""

    __tablename__ = "stock_ledger"
    __table_args__ = (UniqueConstraint("ref", "sku_id", "reason", name="uq_stock_ledger_ref_sku_reason"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    sku_id: Mapped[int] = mapped_column(Integer, index=True)
    delta: Mapped[int] = mapped_column(Integer)
    reason: Mapped[str] = mapped_column(String(24))  # reserve | release | deduct | restore | adjust
    ref: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
