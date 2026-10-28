"""inventory 数据模型。

- `skus`：库存列（stock / lock_stock / sold_out_today / low_stock_threshold）由本模块持有；
  商品归属（product_id）与规格组合（spec_values_json）由 catalog 通过本模块导出的函数写入。
- `stock_logs`：每次库存变化一行，记录 before / after；`(ref_type, ref_id, sku_id, kind)` 唯一，
  是回补 / 释放去重的最后一道防线（ref 为空的历史调用不受约束）。
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Index, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from luzback import Base, BigId, BigIdMixin, TimestampMixin, utcnow

STATUS_ON_SALE = "on_sale"
STATUS_OFF_SALE = "off_sale"

KIND_DEDUCT = "deduct"
KIND_RESTORE = "restore"
KIND_LOCK = "lock"
KIND_UNLOCK = "unlock"
KIND_CONFIRM = "confirm"
KIND_ADJUST = "adjust"
KIND_IMPORT = "import"
KIND_SOLD_OUT = "sold_out"
KINDS = (KIND_DEDUCT, KIND_RESTORE, KIND_LOCK, KIND_UNLOCK, KIND_CONFIRM, KIND_ADJUST, KIND_IMPORT, KIND_SOLD_OUT)


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
    status: Mapped[str] = mapped_column(String(16), default=STATUS_ON_SALE)  # on_sale | off_sale（规格组合是否有效）
    version: Mapped[int] = mapped_column(Integer, default=0)
    low_alerted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)  # stock.low 去重窗口


class StockLog(Base, BigIdMixin):
    __tablename__ = "stock_logs"
    __table_args__ = (
        Index("ix_stock_logs_sku_id_id", "sku_id", "id"),
        Index("uq_stock_logs_ref", "ref_type", "ref_id", "sku_id", "kind", unique=True),
    )

    sku_id: Mapped[int] = mapped_column(BigId)
    delta: Mapped[int] = mapped_column(Integer)  # stock 列的变化量；lock / unlock / sold_out 为 0
    kind: Mapped[str] = mapped_column(String(16))
    reason: Mapped[str | None] = mapped_column(String(200), nullable=True)
    ref_type: Mapped[str | None] = mapped_column(String(32), nullable=True)  # order | after_sales | import | product
    ref_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    operator_id: Mapped[int | None] = mapped_column(BigId, nullable=True)
    stock_before: Mapped[int] = mapped_column(Integer)
    stock_after: Mapped[int] = mapped_column(Integer)
    lock_before: Mapped[int] = mapped_column(Integer, default=0)
    lock_after: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
