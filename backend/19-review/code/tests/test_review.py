"""评价测试：仅本人已完成订单可评价、每单一次、评分与长度校验、敏感词转待审。"""
import pytest

import inventory
import order
import review
from inventory import Sku
from luzback import AppError, ErrorCode
from luzback.testing import memory_session
from order import COMPLETED, Order, create_order
from review import create_review


@pytest.fixture()
def db():
    session = memory_session(inventory, order, review)
    session.add(Sku(id=1, name="鱼头", price_cents=8800, stock=9))
    session.commit()
    created = create_order(session, 7, [(1, 1)], idempotency_key="rv")
    o = session.get(Order, created["order_id"])
    o.status = COMPLETED  # 直接置为已完成（测试数据）
    session.commit()
    yield session, created["order_id"]
    session.close()


def test_owner_can_review_completed_order(db):
    session, oid = db
    r = create_review(session, 7, oid, 5, "很好吃")
    assert r.status == "visible"


def test_other_user_cannot_review(db):
    session, oid = db
    with pytest.raises(AppError) as exc:
        create_review(session, 8, oid, 5, "冒充")
    assert exc.value.code == ErrorCode.FORBIDDEN


def test_uncompleted_order_cannot_be_reviewed(db):
    session, oid = db
    session.get(Order, oid).status = "paid"
    session.commit()
    with pytest.raises(AppError) as exc:
        create_review(session, 7, oid, 5, "未完成")
    assert exc.value.code == ErrorCode.BUSINESS_RULE


def test_one_review_per_order(db):
    session, oid = db
    create_review(session, 7, oid, 4, "first")
    with pytest.raises(AppError) as exc:
        create_review(session, 7, oid, 5, "second")
    assert exc.value.code == ErrorCode.CONFLICT


@pytest.mark.parametrize("rating", [0, 6])
def test_rating_range(db, rating):
    session, oid = db
    with pytest.raises(AppError):
        create_review(session, 7, oid, rating, "x")


def test_sensitive_word_goes_to_pending(db):
    session, oid = db
    r = create_review(session, 7, oid, 1, "加我微信号")
    assert r.status == "pending"
