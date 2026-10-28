"""store 数据模型：门店、营业段、节假日例外。

- 门店手机号加密存储（phone_enc）并保存可查询哈希（phone_hash）；响应只给脱敏值。
- geohash 由 lng/lat 在写入时计算（12 位），附近门店按前缀粗筛。
- 履约参数（配送半径、起送、配送费、阶梯、截单、出餐）只存在门店表，范围与费用计算在 06-geo。
"""
from __future__ import annotations

from sqlalchemy import Boolean, Float, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from luzback import Base, BigIdMixin, SoftDeleteMixin, TimestampMixin

STATUS_OPEN = "open"
STATUS_CLOSED = "closed"
STATUS_PAUSED = "paused"
STATUSES = (STATUS_OPEN, STATUS_CLOSED, STATUS_PAUSED)
CHANNELS = ("dine_in", "delivery", "pickup")


class Store(Base, BigIdMixin, TimestampMixin, SoftDeleteMixin):
    __tablename__ = "stores"
    __table_args__ = (Index("ix_stores_geohash", "geohash"),)

    name: Mapped[str] = mapped_column(String(50))
    phone_enc: Mapped[str | None] = mapped_column(String(200), nullable=True)
    phone_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    address: Mapped[str] = mapped_column(String(200), default="")
    lng: Mapped[float | None] = mapped_column(Float, nullable=True)
    lat: Mapped[float | None] = mapped_column(Float, nullable=True)
    geohash: Mapped[str] = mapped_column(String(12), default="")
    province: Mapped[str] = mapped_column(String(32), default="")
    city: Mapped[str] = mapped_column(String(32), default="")
    district: Mapped[str] = mapped_column(String(32), default="")
    logo_url: Mapped[str] = mapped_column(String(500), default="")
    images_json: Mapped[str] = mapped_column(Text, default="[]")
    intro: Mapped[str] = mapped_column(Text, default="")
    timezone: Mapped[str] = mapped_column(String(40), default="Asia/Shanghai")
    status: Mapped[str] = mapped_column(String(16), default=STATUS_OPEN, index=True)
    delivery_radius_m: Mapped[int] = mapped_column(Integer, default=3000)
    min_order_cents: Mapped[int] = mapped_column(Integer, default=0)
    delivery_fee_cents: Mapped[int] = mapped_column(Integer, default=0)
    free_delivery_threshold_cents: Mapped[int] = mapped_column(Integer, default=0)
    fee_tiers_json: Mapped[str] = mapped_column(Text, default="[]")  # [{"up_to_m": 3000, "fee_cents": 0}, ...]
    cutoff_minutes: Mapped[int] = mapped_column(Integer, default=0)
    prep_minutes: Mapped[int] = mapped_column(Integer, default=20)
    dine_in: Mapped[bool] = mapped_column(Boolean, default=True)
    delivery: Mapped[bool] = mapped_column(Boolean, default=True)
    pickup: Mapped[bool] = mapped_column(Boolean, default=True)
    queue_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    reservation_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    auto_accept: Mapped[bool] = mapped_column(Boolean, default=False)
    printer_config_json: Mapped[str] = mapped_column(Text, default="{}")
    version: Mapped[int] = mapped_column(Integer, default=1)  # 每次后台写 +1，缓存 key 带版本

    hours: Mapped[list["StoreHour"]] = relationship(
        "StoreHour", primaryjoin="foreign(StoreHour.store_id) == Store.id", lazy="selectin", viewonly=True,
        order_by="(StoreHour.weekday, StoreHour.seq)",
    )
    holidays: Mapped[list["StoreHoliday"]] = relationship(
        "StoreHoliday", primaryjoin="foreign(StoreHoliday.store_id) == Store.id", lazy="selectin", viewonly=True,
        order_by="StoreHoliday.date",
    )


class StoreHour(Base, BigIdMixin, TimestampMixin):
    __tablename__ = "store_hours"

    store_id: Mapped[int] = mapped_column(Integer, index=True)
    weekday: Mapped[int] = mapped_column(Integer)  # 0 周一 … 6 周日
    open_time: Mapped[str] = mapped_column(String(5))  # "10:30"
    close_time: Mapped[str] = mapped_column(String(5))  # "21:30"；小于 open_time 表示次日
    seq: Mapped[int] = mapped_column(Integer, default=0)  # 同日多段


class StoreHoliday(Base, BigIdMixin, TimestampMixin):
    __tablename__ = "store_holidays"
    __table_args__ = (UniqueConstraint("store_id", "date", name="uq_store_holidays_store_date"),)

    store_id: Mapped[int] = mapped_column(Integer, index=True)
    date: Mapped[str] = mapped_column(String(10))  # YYYY-MM-DD（门店时区）
    closed: Mapped[bool] = mapped_column(Boolean, default=True)
    open_time: Mapped[str | None] = mapped_column(String(5), nullable=True)
    close_time: Mapped[str | None] = mapped_column(String(5), nullable=True)
