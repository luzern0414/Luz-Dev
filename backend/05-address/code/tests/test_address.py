"""地址测试：手机号校验、默认地址唯一、数量上限、删除默认后的补位。"""
import pytest

import address
from address import MAX_ADDRESSES_PER_USER, Address, create_address, delete_address, set_default
from luzback import AppError, ErrorCode
from luzback.testing import memory_session


@pytest.fixture()
def db():
    session = memory_session(address)
    yield session
    session.close()


def payload(detail="南京西路 1788 号", phone="13800000000", **extra):
    return {"contact": "王", "phone": phone, "province": "上海", "city": "上海", "district": "静安",
            "detail": detail, "house_no": "3F", **extra}


def defaults(db, user_id=1):
    return [a.id for a in db.query(Address).filter_by(user_id=user_id, is_default=True).all()]


def test_first_address_becomes_default(db):
    a = create_address(db, 1, payload())
    assert a.is_default and defaults(db) == [a.id]


def test_only_one_default_per_user(db):
    a = create_address(db, 1, payload())
    b = create_address(db, 1, payload(detail="漕溪北路 300 号", is_default=True))
    assert defaults(db) == [b.id]
    set_default(db, 1, a.id)
    assert defaults(db) == [a.id]


def test_invalid_phone_rejected(db):
    with pytest.raises(AppError) as exc:
        create_address(db, 1, payload(phone="12345"))
    assert exc.value.code == ErrorCode.BAD_REQUEST


def test_other_users_address_is_not_visible(db):
    a = create_address(db, 1, payload())
    with pytest.raises(AppError) as exc:
        set_default(db, 2, a.id)
    assert exc.value.code == ErrorCode.NOT_FOUND


def test_limit_per_user(db):
    for i in range(MAX_ADDRESSES_PER_USER):
        create_address(db, 1, payload(detail=f"路 {i} 号"))
    with pytest.raises(AppError) as exc:
        create_address(db, 1, payload(detail="超出"))
    assert exc.value.code == ErrorCode.LIMIT_REACHED and exc.value.status == 422  # 4225：数量上限


def test_deleting_default_promotes_latest_remaining(db):
    a = create_address(db, 1, payload())
    b = create_address(db, 1, payload(detail="B"))
    c = create_address(db, 1, payload(detail="C"))
    delete_address(db, 1, a.id)
    assert defaults(db) == [c.id]
    assert b.id != c.id
