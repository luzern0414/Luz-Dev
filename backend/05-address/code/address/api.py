"""地址接口（FastAPI 路由）。"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import select

from address import Address, create_address, delete_address, set_default
from identity.api import current_user
from luzback import get_db, ok

router = APIRouter(prefix="/api/v1/app/addresses", tags=["addresses"])


class AddressIn(BaseModel):
    contact: str = Field(min_length=1, max_length=32)
    phone: str = Field(min_length=11, max_length=16)
    province: str = Field(min_length=1, max_length=32)
    city: str = Field(min_length=1, max_length=32)
    district: str = Field(min_length=1, max_length=32)
    detail: str = Field(min_length=1, max_length=128)
    house_no: str = Field(default="", max_length=32)
    tag: str = Field(default="", max_length=16)
    is_default: bool = False


def _dict(a: Address) -> dict:
    return {"id": a.id, "contact": a.contact, "phone": a.phone[:3] + "****" + a.phone[-4:],
            "province": a.province, "city": a.city, "district": a.district, "detail": a.detail,
            "house_no": a.house_no, "tag": a.tag, "is_default": a.is_default}


@router.get("")
def list_addresses(user_id: int = Depends(current_user), db=Depends(get_db)):
    rows = db.execute(
        select(Address).where(Address.user_id == user_id).order_by(Address.is_default.desc(), Address.id.desc())
    ).scalars().all()
    return ok({"items": [_dict(a) for a in rows]})


@router.post("")
def create(body: AddressIn, user_id: int = Depends(current_user), db=Depends(get_db)):
    return ok(_dict(create_address(db, user_id, body.model_dump())))


@router.post("/{address_id}/default")
def make_default(address_id: int, user_id: int = Depends(current_user), db=Depends(get_db)):
    return ok(_dict(set_default(db, user_id, address_id)))


@router.delete("/{address_id}")
def remove(address_id: int, user_id: int = Depends(current_user), db=Depends(get_db)):
    delete_address(db, user_id, address_id)
    return ok(None)
