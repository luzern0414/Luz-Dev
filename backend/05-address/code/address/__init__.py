"""地址模块：收货地址增删改、默认地址唯一性、数量上限、手机号校验。

默认地址唯一性用“同一事务内先清后设”实现；数据库层用部分唯一索引兜底（见 MODULE.md §2）。
"""
from __future__ import annotations

import re
from datetime import datetime, timezone

from sqlalchemy import Boolean, Float, Integer, String, func, select, update
from sqlalchemy.orm import Mapped, Session, mapped_column

from luzback import AppError, Base, ErrorCode

MAX_ADDRESSES_PER_USER = 20
PHONE_RE = re.compile(r"^1[3-9]\d{9}$")


class Address(Base):
    __tablename__ = "addresses"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer, index=True)
    contact: Mapped[str] = mapped_column(String(32))
    phone: Mapped[str] = mapped_column(String(16))
    province: Mapped[str] = mapped_column(String(32))
    city: Mapped[str] = mapped_column(String(32))
    district: Mapped[str] = mapped_column(String(32))
    detail: Mapped[str] = mapped_column(String(128))
    house_no: Mapped[str] = mapped_column(String(32), default="")
    lat: Mapped[float | None] = mapped_column(Float, nullable=True)
    lng: Mapped[float | None] = mapped_column(Float, nullable=True)
    tag: Mapped[str] = mapped_column(String(16), default="")  # 家 | 公司 | 学校 | ""
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(default=lambda: datetime.now(timezone.utc))


def validate_phone(phone: str) -> None:
    if not PHONE_RE.match(phone or ""):
        raise AppError(ErrorCode.BAD_REQUEST, "invalid phone number")


def _count(db: Session, user_id: int) -> int:
    return db.execute(select(func.count()).select_from(Address).where(Address.user_id == user_id)).scalar_one()


def create_address(db: Session, user_id: int, data: dict) -> Address:
    validate_phone(data.get("phone", ""))
    if _count(db, user_id) >= MAX_ADDRESSES_PER_USER:
        raise AppError(ErrorCode.LIMIT_REACHED, f"at most {MAX_ADDRESSES_PER_USER} addresses")  # 数量上限 4225（SPEC §4）
    is_default = bool(data.get("is_default")) or _count(db, user_id) == 0  # 第一个地址自动设为默认
    if is_default:
        _clear_default(db, user_id)
    address = Address(user_id=user_id, is_default=is_default, **{k: v for k, v in data.items() if k != "is_default"})
    db.add(address)
    db.commit()
    return address


def set_default(db: Session, user_id: int, address_id: int) -> Address:
    address = db.get(Address, address_id)
    if address is None or address.user_id != user_id:
        raise AppError(ErrorCode.NOT_FOUND, "address not found")
    _clear_default(db, user_id)
    address.is_default = True
    db.commit()
    return address


def delete_address(db: Session, user_id: int, address_id: int) -> None:
    address = db.get(Address, address_id)
    if address is None or address.user_id != user_id:
        raise AppError(ErrorCode.NOT_FOUND, "address not found")
    was_default = address.is_default
    db.delete(address)
    db.flush()
    if was_default:
        # 删除默认地址后，自动把最近创建的一条设为默认
        replacement = db.execute(
            select(Address).where(Address.user_id == user_id).order_by(Address.id.desc()).limit(1)
        ).scalar_one_or_none()
        if replacement is not None:
            replacement.is_default = True
    db.commit()


def _clear_default(db: Session, user_id: int) -> None:
    db.execute(update(Address).where(Address.user_id == user_id, Address.is_default.is_(True)).values(is_default=False))
