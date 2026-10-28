"""测试工具（只用于测试与本地联调）：建库、造商品、造券、员工身份、把发件箱事件投递给订阅者。

    from order.testing import order_db, add_sku, give_coupon, staff, drain_events
"""
from __future__ import annotations

import importlib
from collections.abc import Iterable
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from luzback import OutboxEvent, dispatch_pending, utcnow
from luzback.db import get_session_factory
from luzback.testing import memory_session

MODULES = ("identity", "address", "inventory", "inventory.reservation", "coupon", "wallet", "wechat_pay", "order")
SUBSCRIBERS = ("order.events", "wechat_pay.events")


def order_db(*extra: str, url: str = "sqlite://") -> Session:
    """建好订单相关模块的表并返回会话（extra 可再加 'after_sales' 等）。"""
    return memory_session(*MODULES, *extra, url=url)


def add_sku(db: Session, sku_id: int, price_cents: int, stock: int = 10, name: str | None = None) -> None:
    import inventory
    db.add(inventory.Sku(id=sku_id, name=name or f"商品{sku_id}", price_cents=price_cents, stock=stock))
    db.commit()


def sku_stock(db: Session, sku_id: int) -> int:
    import inventory
    db.expire_all()
    return db.get(inventory.Sku, sku_id).stock


def give_coupon(db: Session, user_id: int, now: datetime, staff_id: int = 1, **template: Any) -> int:
    """建一张已发布模板并发给 user_id，返回 user_coupon_id。默认：满 100 减 20、30 天有效。"""
    from coupon import service
    data = dict(name="满100减20", type="amount_off", amount_cents=2000, threshold_cents=10000, valid_type="relative",
                valid_days=30, channels=["claim_center", "admin", "event", "lottery", "compensation", "code"],
                per_member_limit=0)
    data.update(template)
    t = service.create_template(db, staff_id, data, now=now)
    service.publish_template(db, staff_id, t.id, now=now)
    c = service.claim(db, user_id, t.id, now=now)
    db.commit()
    return c.id


def coupon_status(db: Session, coupon_id: int) -> str:
    from coupon import UserCoupon
    db.expire_all()
    return db.get(UserCoupon, coupon_id).status


def staff(permissions: Iterable[str], *, staff_id: int = 1, store_ids: tuple[int, ...] | None = None,
          fresh: bool = True) -> Any:
    """构造员工身份（admin_rbac.Staff），配合 app.dependency_overrides[authenticated_staff] 使用。"""
    from admin_rbac import Staff
    from datetime import timedelta
    return Staff(id=staff_id, username=f"staff{staff_id}", permissions=frozenset(permissions),
                 all_stores=store_ids is None, store_ids=tuple(store_ids or ()),
                 auth_at=utcnow() if fresh else utcnow() - timedelta(days=1), mfa_enabled=True)


def as_staff(app: Any, staff_obj: Any) -> None:
    from admin_rbac.api import authenticated_staff
    app.dependency_overrides[authenticated_staff] = lambda: staff_obj


def topics(db: Session) -> list[str]:
    return list(db.execute(select(OutboxEvent.topic).order_by(OutboxEvent.id)).scalars())


def events(db: Session, topic: str) -> list[dict]:
    import json
    rows = db.execute(select(OutboxEvent.payload_json).where(OutboxEvent.topic == topic).order_by(OutboxEvent.id)).scalars()
    return [json.loads(r) for r in rows]


def drain_events(db: Session, modules: Iterable[str] = SUBSCRIBERS, rounds: int = 5) -> list[str]:
    """把待投递的发件箱事件交给各模块 SUBSCRIPTIONS（与 25-jobs worker 相同：每个订阅者独立会话、成功提交）。"""
    routes: dict[str, list] = {}
    for name in modules:
        module = importlib.import_module(name)
        for topic, handler in getattr(module, "SUBSCRIPTIONS", {}).items():
            routes.setdefault(topic, []).append(handler)
    factory = get_session_factory()
    handled: list[str] = []
    errors: list[str] = []

    def route(topic: str, payload: dict, event: Any = None) -> None:
        for handler in routes.get(topic, []):
            session = factory()
            try:
                handler(session, payload, event.event_id if event is not None else "")
                session.commit()
            except Exception as exc:
                session.rollback()
                errors.append(f"{topic}: {type(exc).__name__}: {exc}")
                raise
            finally:
                session.close()
        if topic in routes:
            handled.append(topic)

    db.commit()
    far = datetime(2100, 1, 1, tzinfo=timezone.utc)
    for _ in range(rounds):
        counts = dispatch_pending(db, route, now=far, limit=500)
        if not any(counts.values()):
            break
    db.expire_all()
    if errors:
        raise AssertionError("subscriber failed: " + "; ".join(errors))
    return handled
