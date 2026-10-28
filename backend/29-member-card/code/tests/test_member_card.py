"""会员卡用例：卡种校验与限额、购卡发卡与事件幂等、使用（次卡 / 期限卡 / 时段卡 / 防借卡 / 状态拒绝原因）、冲正、
并发扣次、冻结算术、转让、挂失补办、退卡公式与流程、调整、定时任务、抽奖延期、未兑付价值。"""
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

import member_card
import wallet
from luzback import AppError, Base, ErrorCode, OutboxEvent
from luzback.audit import AuditLog
from luzback.testing import memory_session
from member_card import config as mconfig
from member_card import service as svc
from member_card.models import CardLedger, CardOrder, CardUsage, MemberCard
from member_card.rules import in_windows, local_midnight_after, quote_card_refund

T0 = datetime(2026, 10, 1, 2, 0, tzinfo=timezone.utc)  # 上海 2026-10-01 10:00 周四
DAY = timedelta(days=1)


@pytest.fixture()
def db():
    session = memory_session(wallet, member_card)
    yield session
    session.close()


def events(db, topic):
    db.flush()
    return [json.loads(e.payload_json) for e in db.execute(select(OutboxEvent).where(OutboxEvent.topic == topic)).scalars()]


def product(db, **kw):
    values = {"name": "月卡", "kind": "period", "price_cents": 30000, "period_days": 30, "store_scope": "all"} | kw
    data = svc.create_product(db, 9, values)
    svc.set_product_status(db, 9, data["id"], "on")
    db.commit()
    return data["id"]


def count_product(db, **kw):
    return product(db, **({"name": "10次卡", "kind": "count", "price_cents": 100000, "original_price_cents": 150000,
                           "sessions": 10, "gift_sessions": 2, "period_days": 0} | kw))


def buy(db, product_id, user_id=1, store_id=5, at=T0, event_id=None):
    order = svc.create_order(db, user_id, product_id, store_id, at=at)
    db.commit()
    out = svc.handle_event(db, "payment.succeeded", {"biz_type": "member_card", "biz_id": order["id"],
                                                     "amount_cents": order["amount_cents"], "payment_id": f"pay{order['id']}",
                                                     "paid_at": at.isoformat()}, event_id or f"e{order['id']}")
    assert out == "issued"
    return db.execute(select(MemberCard).where(MemberCard.order_id == order["id"])).scalar_one()


def use(db, card, ref, store_id=5, at=T0):
    out = svc.consume(db, store_id=store_id, ref=ref, card_id=card.id, at=at)
    db.commit()
    return out


def reason_of(fn):
    with pytest.raises(AppError) as exc:
        fn()
    return exc.value.code, (exc.value.data or {}).get("reason")


# ---------------------------------------------------------------- 卡种与限额


def test_product_validation(db):
    for bad in ({"kind": "stored_value"}, {"kind": "count", "sessions": 0, "period_days": 0},
                {"kind": "time_slot"}, {"store_scope": "single"}, {"store_scope": "multi"},
                {"original_price_cents": 100}, {"freeze_min_days": 40, "freeze_max_days": 30}):
        with pytest.raises(AppError) as exc:
            svc.create_product(db, 9, {"name": "x", "kind": "period", "price_cents": 30000, "period_days": 30,
                                       "store_scope": "all"} | bad)
        assert exc.value.code == ErrorCode.BAD_REQUEST, bad


def test_product_prepaid_limits(db):
    assert reason_of(lambda: svc.create_product(db, 9, {"name": "x", "kind": "period", "price_cents": 500001,
                                                        "period_days": 30, "store_scope": "all"})) == \
        (ErrorCode.LIMIT_REACHED, "prepaid_amount_cap")
    assert reason_of(lambda: svc.create_product(db, 9, {"name": "x", "kind": "period", "price_cents": 1000,
                                                        "period_days": 731, "store_scope": "all"}))[1] == "prepaid_units_cap"
    assert reason_of(lambda: svc.create_product(db, 9, {"name": "x", "kind": "count", "price_cents": 1000,
                                                        "sessions": 61, "store_scope": "all"}))[1] == "prepaid_units_cap"
    assert reason_of(lambda: svc.create_product(db, 9, {"name": "x", "kind": "count", "price_cents": 1000, "sessions": 8,
                                                        "gift_sessions": 3, "store_scope": "all"}))[1] == "gift_ratio_exceeded"


def test_product_update_audited_and_offsale(db):
    pid = product(db)
    svc.update_product(db, 9, pid, {"price_cents": 28000})
    svc.set_product_status(db, 9, pid, "off")
    db.commit()
    assert db.execute(select(func.count()).select_from(AuditLog).where(AuditLog.resource == "member_card_product")).scalar_one() == 4
    with pytest.raises(AppError) as exc:
        svc.create_order(db, 1, pid, 5, at=T0)
    assert exc.value.code == ErrorCode.NOT_FOUND


# ---------------------------------------------------------------- 购卡与发卡


def test_purchase_issues_card_on_payment_event_once(db, doubles):
    pid = product(db, gift_days=5)
    card = buy(db, pid)
    call = doubles.calls[0]
    assert call["biz_type"] == "member_card" and call["goods_kind"] == "offline_service"
    assert card.status == "active" and card.total_units == 35 and card.paid_cents == 30000
    # 上海 10-01 算第 1 天，35 天 → 11-05 00:00 上海 = 11-04 16:00 UTC
    assert svc.aware(card.ends_at) == datetime(2026, 11, 4, 16, 0, tzinfo=timezone.utc)
    payload = {"biz_type": "member_card", "biz_id": card.order_id, "amount_cents": 30000}
    assert svc.handle_event(db, "payment.succeeded", payload, f"e{card.order_id}") == "duplicate"
    assert svc.handle_event(db, "payment.succeeded", payload, "other") == "duplicate"
    assert db.execute(select(func.count()).select_from(MemberCard)).scalar_one() == 1
    assert len(events(db, "member_card.issued")) == 1
    assert card.holder_name == "会员1" and card.holder_phone_masked == "138****0001"


def test_payment_mismatch_and_late_payment(db):
    pid = product(db)
    order = svc.create_order(db, 1, pid, 5, at=T0)
    db.commit()
    assert svc.handle_event(db, "payment.succeeded", {"biz_type": "member_card", "biz_id": order["id"], "amount_cents": 1}) == "mismatch"
    assert db.get(CardOrder, order["id"]).needs_manual is True
    assert svc.close_expired_orders(db, T0 + timedelta(minutes=20)) == 1
    db.commit()
    assert svc.handle_event(db, "payment.succeeded", {"biz_type": "member_card", "biz_id": order["id"],
                                                      "amount_cents": 30000}) == "issued"


def test_purchase_rules(db, doubles):
    pid = product(db, per_member_limit=1)
    buy(db, pid)
    assert reason_of(lambda: svc.create_order(db, 1, pid, 5, at=T0))[1] == "per_member_limit"
    single = product(db, store_scope="single", store_id=7)
    assert reason_of(lambda: svc.create_order(db, 1, single, 5, at=T0))[1] == "store_not_applicable"
    mconfig.CONFIG.stop_prepaid = True
    assert reason_of(lambda: svc.create_order(db, 2, single, 7, at=T0))[1] == "prepaid_stopped"
    mconfig.CONFIG.stop_prepaid = False
    doubles.fail = True
    with pytest.raises(AppError) as exc:
        svc.create_order(db, 2, single, 7, at=T0)
    assert exc.value.code == ErrorCode.UPSTREAM_FAILED


def test_member_total_and_renewal_caps(db):
    count = count_product(db, sessions=40, gift_sessions=0, price_cents=1_000_000, original_price_cents=1_000_000)
    buy(db, count)  # 已有 40 次
    assert reason_of(lambda: svc.create_order(db, 1, count, 5, at=T0))[1] == "prepaid_units_cap_after_renewal"
    wallet.recharge(db, 1, "w1", 500000, 0)  # 储值本金 5000 元计入合计
    db.commit()
    small = count_product(db, sessions=5, gift_sessions=0, price_cents=600_000, original_price_cents=600_000)
    assert reason_of(lambda: svc.create_order(db, 1, small, 5, at=T0))[1] == "member_total_cap"
    assert svc.outstanding_cents(db, 1) == 1_000_000


def test_renew_stack_and_not_activated(db):
    pid = product(db)
    first = buy(db, pid)
    second = buy(db, pid, at=T0 + DAY)
    assert svc.aware(second.starts_at) == svc.aware(first.ends_at)
    assert reason_of(lambda: svc.consume(db, store_id=5, ref="x", card_id=second.id, at=T0 + 2 * DAY))[1] == "card_not_activated"
    use(db, first, "y", at=T0 + 2 * DAY)


def test_manual_and_gift_issue(db):
    pid = product(db)
    card = svc.issue_manual(db, 7, pid, 3, 5, "manual", 30000, "线下收款", "m1", at=T0)
    again = svc.issue_manual(db, 7, pid, 3, 5, "manual", 30000, "线下收款", "m1", at=T0)
    assert card["id"] == again["id"]
    assert reason_of(lambda: svc.issue_manual(db, 7, pid, 3, 5, "gift", 100, "x", "g0"))[0] == ErrorCode.BAD_REQUEST
    with pytest.raises(AppError) as exc:
        svc.issue_manual(db, 7, pid, 999, 5, "gift", 0, "x", "g1")
    assert exc.value.code == ErrorCode.NOT_FOUND
    gift = svc.issue_gift(db, 4, pid, "camp-1")
    assert gift.id == svc.issue_gift(db, 4, pid, "camp-1").id and gift.paid_cents == 0
    db.commit()
    assert db.execute(select(AuditLog).where(AuditLog.action == "member_card.issue.manual")).scalars().first()


# ---------------------------------------------------------------- 使用


def test_count_card_consume_idempotent_used_up_and_reverse(db):
    card = buy(db, count_product(db, sessions=2, gift_sessions=0))
    first = use(db, card, "gate-1")
    assert use(db, card, "gate-1")["replayed"] is True
    use(db, card, "gate-2")
    db.refresh(card)
    assert (card.sessions_remaining, card.status) == (0, "used_up")
    assert reason_of(lambda: svc.consume(db, store_id=5, ref="gate-3", card_id=card.id, at=T0)) == \
        (ErrorCode.INSUFFICIENT_BALANCE, "card_used_up")
    svc.reverse_use(db, "gate-2", "闸机未开")
    assert svc.reverse_use(db, "gate-2", "闸机未开")["replayed"] is True
    db.commit()
    db.refresh(card)
    assert (card.sessions_remaining, card.status) == (1, "active") and first["units"] == 1
    kinds = [e.kind for e in db.execute(select(CardLedger).where(CardLedger.card_id == card.id).order_by(CardLedger.id)).scalars()]
    assert kinds == ["issue", "use", "use", "reverse"]


def test_ref_bound_to_card(db):
    pid = count_product(db)
    a, b = buy(db, pid), buy(db, pid, user_id=2)
    use(db, a, "r1")
    with pytest.raises(AppError) as exc:
        svc.consume(db, store_id=5, ref="r1", card_id=b.id, at=T0)
    assert exc.value.code == ErrorCode.CONFLICT
    with pytest.raises(AppError) as exc:
        svc.consume(db, store_id=5, ref="r2", card_id=a.id, user_id=2, at=T0)
    assert exc.value.code == ErrorCode.NOT_FOUND


def test_status_reasons_are_distinct(db):
    pid = product(db)
    card = buy(db, pid)
    cases = {}
    for status in ("frozen", "lost", "disabled", "refunding", "refunded", "expired"):
        db.execute(MemberCard.__table__.update().where(MemberCard.id == card.id).values(status=status))
        db.commit()
        db.refresh(card)
        cases[status] = reason_of(lambda: svc.consume(db, store_id=5, ref=f"r-{status}", card_id=card.id, at=T0))[1]
    assert cases == {"frozen": "card_frozen", "lost": "card_lost", "disabled": "card_disabled",
                     "refunding": "card_refunding", "refunded": "card_refunded", "expired": "card_expired"}
    db.execute(MemberCard.__table__.update().where(MemberCard.id == card.id).values(status="active"))
    db.commit()
    assert reason_of(lambda: svc.consume(db, store_id=5, ref="late", card_id=card.id, at=T0 + 40 * DAY))[1] == "card_expired"


def test_store_scope_and_time_slot_window(db):
    multi = product(db, store_scope="multi", store_ids=[5, 6])
    card = buy(db, multi)
    assert reason_of(lambda: svc.consume(db, store_id=8, ref="s", card_id=card.id, at=T0))[1] == "store_not_applicable"
    use(db, card, "ok6", store_id=6)
    slot = product(db, name="早鸟卡", kind="time_slot", slot_windows=[{"weekdays": [1, 2, 3, 4, 5], "start": "06:00",
                                                                     "end": "09:00"}])
    early = buy(db, slot, user_id=2)
    at_10 = T0  # 上海周四 10:00
    assert reason_of(lambda: svc.consume(db, store_id=5, ref="t1", card_id=early.id, at=at_10)) == \
        (ErrorCode.STORE_CLOSED, "outside_time_slot")
    use(db, early, "t2", at=T0 + DAY - timedelta(hours=3))  # 07:00 周五
    sat = T0 + 2 * DAY - timedelta(hours=3)  # 周六 07:00
    assert reason_of(lambda: svc.consume(db, store_id=5, ref="t3", card_id=early.id, at=sat))[1] == "outside_time_slot"


def test_windows_cross_midnight():
    windows = json.dumps([{"weekdays": [5], "start": "22:00", "end": "02:00"}])
    fri_23 = datetime(2026, 10, 2, 15, 0, tzinfo=timezone.utc)  # 上海周五 23:00
    assert in_windows(fri_23, windows, "Asia/Shanghai")
    assert in_windows(fri_23 + timedelta(hours=2), windows, "Asia/Shanghai")       # 周六 01:00 属周五窗口
    assert not in_windows(fri_23 + timedelta(hours=4), windows, "Asia/Shanghai")   # 周六 03:00
    assert not in_windows(fri_23 - timedelta(days=1), windows, "Asia/Shanghai")    # 周四 23:00


def test_entry_cooldown_prevents_sharing(db):
    card = buy(db, product(db, entry_cooldown_minutes=30))
    use(db, card, "a")
    assert reason_of(lambda: svc.consume(db, store_id=5, ref="b", card_id=card.id, at=T0 + timedelta(minutes=10)))[1] == "entry_cooldown"
    use(db, card, "c", at=T0 + timedelta(minutes=31))


def test_first_use_activation_and_deadline_job(db):
    pid = product(db, activation_mode="on_first_use", activation_deadline_days=30)
    a, b = buy(db, pid), buy(db, pid, user_id=2)
    assert a.status == "inactive" and a.ends_at is None
    use(db, a, "first", at=T0 + 3 * DAY)
    db.refresh(a)
    assert a.status == "active" and svc.aware(a.starts_at) == T0 + 3 * DAY
    assert svc.auto_activate(db, T0 + 29 * DAY) == 0
    assert svc.auto_activate(db, T0 + 31 * DAY) == 1
    db.commit()
    db.refresh(b)
    assert b.status == "active" and svc.aware(b.starts_at) == T0 + 30 * DAY


def test_concurrent_consume_50_threads(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'mc.db'}", future=True, connect_args={"check_same_thread": False, "timeout": 30})
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    with factory() as s:
        data = svc.create_product(s, 9, {"name": "10次", "kind": "count", "price_cents": 10000, "sessions": 10,
                                         "store_scope": "all"})
        card = svc.issue_card(s, s.get(member_card.CardProduct, data["id"]), 1, store_id=5, source="purchase",
                              issue_ref="o1", paid_cents=10000, paid_at=T0, at=T0)
        s.commit()
        card_id = card.id

    def go(i):
        with factory() as session:
            try:
                svc.consume(session, store_id=5, ref=f"g{i}", card_id=card_id, at=T0)
                session.commit()
                return "ok"
            except AppError as exc:
                session.rollback()
                return exc.code.name

    with ThreadPoolExecutor(max_workers=50) as pool:
        results = list(pool.map(go, range(50)))
    assert results.count("ok") == 10
    with factory() as s:
        c = s.get(MemberCard, card_id)
        assert (c.sessions_remaining, c.sessions_used, c.status) == (0, 10, "used_up")
        assert s.execute(select(func.count()).select_from(CardUsage)).scalar_one() == 10
    engine.dispose()


# ---------------------------------------------------------------- 冻结


def test_freeze_extends_and_early_unfreeze_recalculates(db):
    card = buy(db, product(db))
    original = svc.aware(card.ends_at)
    svc.freeze(db, card, 10, "出差", "user:1", at=T0)
    db.commit()
    assert card.status == "frozen" and svc.aware(card.ends_at) == original + 10 * DAY
    assert reason_of(lambda: svc.consume(db, store_id=5, ref="f", card_id=card.id, at=T0 + DAY))[1] == "card_frozen"
    out = svc.unfreeze(db, card, "user:1", at=T0 + timedelta(days=3, hours=12))
    db.commit()
    assert out["actual_days"] == 4 and svc.aware(card.ends_at) == original + 4 * DAY
    assert card.status == "active" and card.frozen_days_total == 4 and card.freeze_count == 1


def test_freeze_limits(db):
    card = buy(db, product(db, freeze_min_days=3, freeze_max_days=10, freeze_max_times=2, freeze_total_max_days=15))
    assert reason_of(lambda: svc.freeze(db, card, 2, "", "u", at=T0))[1] == "freeze_days_out_of_range"
    assert reason_of(lambda: svc.freeze(db, card, 11, "", "u", at=T0))[1] == "freeze_days_out_of_range"
    svc.freeze(db, card, 10, "", "u", at=T0)
    assert reason_of(lambda: svc.freeze(db, card, 3, "", "u", at=T0))[0] == ErrorCode.CONFLICT  # 已冻结
    svc.unfreeze(db, card, "u", at=T0 + 10 * DAY)
    assert reason_of(lambda: svc.freeze(db, card, 6, "", "u", at=T0 + 11 * DAY))[1] == "freeze_days_exceeded"
    svc.freeze(db, card, 5, "", "u", at=T0 + 11 * DAY)
    svc.unfreeze(db, card, "u", at=T0 + 16 * DAY)
    assert reason_of(lambda: svc.freeze(db, card, 3, "", "u", at=T0 + 17 * DAY))[1] == "freeze_times_exceeded"
    no_freeze = buy(db, product(db, freezable=False), user_id=2)
    assert reason_of(lambda: svc.freeze(db, no_freeze, 7, "", "u", at=T0))[1] == "freeze_not_allowed"
    count = buy(db, count_product(db), user_id=3)
    assert reason_of(lambda: svc.freeze(db, count, 7, "", "u", at=T0))[1] == "freeze_not_applicable"


def test_auto_unfreeze_job_keeps_full_extension(db):
    card = buy(db, product(db))
    original = svc.aware(card.ends_at)
    svc.freeze(db, card, 7, "", "u", at=T0)
    db.commit()
    assert svc.auto_unfreeze(db, T0 + 6 * DAY) == 0
    assert svc.auto_unfreeze(db, T0 + 7 * DAY + timedelta(hours=1)) == 1
    assert svc.auto_unfreeze(db, T0 + 8 * DAY) == 0
    db.commit()
    db.refresh(card)
    assert card.status == "active" and svc.aware(card.ends_at) == original + 7 * DAY


# ---------------------------------------------------------------- 转让


def test_transfer_by_phone_and_limits(db):
    card = buy(db, product(db))
    assert reason_of(lambda: svc.transfer(db, card, to_user_id=1, operator="u"))[1] == "transfer_to_self"
    with pytest.raises(AppError) as exc:
        svc.transfer(db, card, to_phone="13900000000", operator="u")
    assert exc.value.code == ErrorCode.NOT_FOUND
    out = svc.transfer(db, card, to_phone="13800000002", operator="user:1", at=T0)
    db.commit()
    assert out["status"] == "transferred" and card.user_id == 2 and card.holder_name == "会员2"
    assert reason_of(lambda: svc.transfer(db, card, to_user_id=3, operator="u", at=T0))[1] == "transfer_times_exceeded"
    assert events(db, "member_card.transferred")[0]["to_user_id"] == 2
    cooling = buy(db, product(db, transfer_cooling_days=30), user_id=4)
    assert reason_of(lambda: svc.transfer(db, cooling, to_user_id=5, operator="u", at=T0 + DAY))[1] == "transfer_cooling"
    locked = buy(db, product(db, transferable=False), user_id=6)
    assert reason_of(lambda: svc.transfer(db, locked, to_user_id=5, operator="u", at=T0))[1] == "transfer_not_allowed"


def test_transfer_with_fee_waits_for_payment(db):
    card = buy(db, product(db, transfer_fee_cents=2000))
    out = svc.transfer(db, card, to_user_id=2, operator="user:1", at=T0)
    db.commit()
    assert out["status"] == "awaiting_fee_payment" and card.user_id == 1
    order = out["order"]
    assert order["invoice"]["tax_code"] is None
    assert svc.handle_event(db, "payment.succeeded", {"biz_type": "member_card", "biz_id": order["id"],
                                                      "amount_cents": 2000}) == "transferred"
    db.refresh(card)
    assert card.user_id == 2
    staff_card = buy(db, product(db, transfer_fee_cents=2000), user_id=3)
    out = svc.transfer(db, staff_card, to_user_id=4, operator="staff:7", by_staff=True, fee_collected_offline=True, at=T0)
    assert out["transfer"]["fee_collected"] == "offline" and out["transfer"]["fee_cents"] == 2000


def test_transfer_fee_paid_but_card_changed_requests_fee_refund(db):
    card = buy(db, product(db, transfer_fee_cents=2000))
    order = svc.transfer(db, card, to_user_id=2, operator="user:1", at=T0)["order"]
    svc.freeze(db, card, 7, "", "u", at=T0)
    db.commit()
    assert svc.handle_event(db, "payment.succeeded", {"biz_type": "member_card", "biz_id": order["id"],
                                                      "amount_cents": 2000}) == "transfer_failed"
    assert db.get(CardOrder, order["id"]).needs_manual and events(db, "member_card.refund_requested")[0]["amount_cents"] == 2000


# ---------------------------------------------------------------- 挂失补办、停用


def test_lost_and_replace(db):
    card = buy(db, product(db))
    old_no = card.card_no
    svc.report_lost(db, card, "user:1")
    assert reason_of(lambda: svc.consume(db, store_id=5, ref="l", card_id=card.id, at=T0))[1] == "card_lost"
    svc.replace_card(db, 7, card, "补卡")
    db.commit()
    assert card.status == "active" and card.card_no != old_no
    assert reason_of(lambda: svc.replace_card(db, 7, card, "x"))[0] == ErrorCode.CONFLICT
    svc.report_lost(db, card, "user:1")
    svc.cancel_lost(db, card, "user:1")
    assert card.status == "active"


def test_disable_enable(db):
    card = buy(db, product(db))
    svc.disable_card(db, 7, card, "违规")
    assert reason_of(lambda: svc.consume(db, store_id=5, ref="d", card_id=card.id, at=T0))[1] == "card_disabled"
    svc.enable_card(db, 7, card, "恢复")
    assert card.status == "active"


# ---------------------------------------------------------------- 退卡公式


def rq(**kw):
    base = dict(paid_cents=100000, original_price_cents=150000, paid_units=10, total_units=12, used_units=3,
                untouched=False, cause="customer", paid_at=T0, at=T0 + 30 * DAY, cooling_off_days=7)
    return quote_card_refund(**(base | kw))


def test_formula_cooling_off_full_principal():
    out = rq(used_units=0, untouched=True, at=T0 + 6 * DAY)
    assert out["rule"] == "cooling_off_full" and out["refund_cents"] == 100000
    assert rq(used_units=1, untouched=False, at=T0 + DAY)["rule"] == "customer_original_price"
    assert rq(used_units=0, untouched=True, at=T0 + 7 * DAY)["rule"] == "customer_original_price"


def test_formula_customer_uses_original_price_capped():
    out = rq()  # 原价 150000 / 10 次 = 15000 每次，用 3 次 = 45000
    assert (out["used_value_cents"], out["refund_cents"]) == (45000, 55000)
    assert rq(used_units=8)["refund_cents"] == 0  # 120000 > 实付，不再追要
    assert rq(fee_bp=1000)["fee_cents"] == 5500


def test_formula_merchant_discounted_with_gift_proration_and_interest():
    out = rq(cause="merchant", lpr_bp=300, at=T0 + 365 * DAY)
    # 每次 = 100000 / 12（含 2 次赠送）；用 3 次 = 25000；可退 75000；利息 75000 × 3% = 2250
    assert (out["used_value_cents"], out["refund_cents"], out["interest_cents"]) == (25000, 75000, 2250)
    assert rq(cause="merchant", fee_bp=1000)["fee_cents"] == 0


def test_period_card_quote_counts_used_days(db):
    card = buy(db, product(db))  # 30 天 30000
    q = svc.quote_refund(db, card, "merchant", at=T0 + 10 * DAY)
    assert q["used_units"] == 10 and q["refund_cents"] == 20000
    q = svc.quote_refund(db, card, "customer", at=T0 + 3 * DAY)
    assert q["rule"] == "cooling_off_full" and q["refund_cents"] == 30000
    use(db, card, "u1", at=T0 + DAY)
    assert svc.quote_refund(db, card, "customer", at=T0 + 3 * DAY)["rule"] == "customer_original_price"


def test_expired_customer_refund_rejected_merchant_allowed(db):
    card = buy(db, product(db))
    svc.expire_cards(db, T0 + 40 * DAY)
    db.commit()
    db.refresh(card)
    assert reason_of(lambda: svc.quote_refund(db, card, "customer", at=T0 + 40 * DAY))[1] == "expired_customer_no_refund"
    assert svc.quote_refund(db, card, "merchant", at=T0 + 40 * DAY)["refund_cents"] == 0


# ---------------------------------------------------------------- 退卡流程


def test_refund_flow_success(db):
    card = buy(db, count_product(db))
    use(db, card, "c1", at=T0 + DAY)
    refund = svc.request_refund(db, card, "customer", "搬家", "user:1", user_id=1, at=T0 + 10 * DAY)
    db.commit()
    assert refund["status"] == "pending_approval" and card.status == "refunding"
    assert reason_of(lambda: svc.consume(db, store_id=5, ref="c2", card_id=card.id, at=T0 + 11 * DAY))[1] == "card_refunding"
    svc.approve_refund(db, refund["id"], 7)
    db.commit()
    req = events(db, "member_card.refund_requested")[0]
    assert req["amount_cents"] == 85000 and req["route"] == "original" and req["payment_id"] == card.payment_id
    assert svc.handle_event(db, "payment.refunded", {"business_ref": req["business_ref"], "biz_type": "member_card"}, "rf1") == "refunded"
    assert svc.handle_event(db, "payment.refunded", {"business_ref": req["business_ref"]}, "rf2") == "duplicate"
    db.refresh(card)
    assert card.status == "refunded" and card.sessions_remaining == 0
    assert events(db, "member_card.refunded")[0]["invoice_action"] == "red_reverse"


def test_refund_reject_and_failure_restore(db):
    card = buy(db, product(db))
    refund = svc.request_refund(db, card, "customer", "x", "user:1", user_id=1, at=T0 + DAY)
    svc.reject_refund(db, refund["id"], 7, "不符合")
    db.commit()
    db.refresh(card)
    assert card.status == "active"
    refund = svc.request_refund(db, card, "merchant", "门店装修", "staff:7", staff_id=7, at=T0 + DAY)
    db.commit()
    assert refund["status"] == "processing"
    ref = events(db, "member_card.refund_requested")[-1]["business_ref"]
    assert svc.handle_event(db, "payment.refund_failed", {"business_ref": ref, "reason": "ABNORMAL"}) == "failed"
    db.refresh(card)
    assert card.status == "active"
    with pytest.raises(AppError):
        svc.request_refund(db, card, "merchant", "x", "user:1", user_id=1)


def test_refund_while_frozen_ends_freeze_first(db):
    card = buy(db, product(db))
    original = svc.aware(card.ends_at)
    svc.freeze(db, card, 10, "", "u", at=T0 + 8 * DAY)
    refund = svc.request_refund(db, card, "customer", "x", "user:1", user_id=1, at=T0 + timedelta(days=9, hours=12))
    db.commit()
    assert card.status == "refunding" and card.prev_status == "active"
    assert svc.aware(card.ends_at) == original + 2 * DAY
    # 实际使用 8 天、冻结 1.5 天按 2 天计；剩余天数向上取整（当天未完不算已用）→ 已用 7 天，偏向消费者
    assert refund["breakdown"]["used_units"] == 7


def test_transferred_card_refunds_offline(db):
    card = buy(db, product(db))
    svc.transfer(db, card, to_user_id=2, operator="u", at=T0)
    refund = svc.request_refund(db, card, "customer", "x", "staff:7", staff_id=7, at=T0 + 10 * DAY)
    assert refund["route"] == "offline"
    svc.complete_offline_refund(db, refund["id"], 7, True, "现金")
    db.commit()
    assert card.status == "refunded"


def test_auto_approve_config(db):
    mconfig.CONFIG.refund_requires_approval = False
    card = buy(db, product(db))
    assert svc.request_refund(db, card, "customer", "x", "user:1", user_id=1, at=T0)["status"] == "processing"


# ---------------------------------------------------------------- 调整


def test_adjust_sessions_and_days(db):
    card = buy(db, count_product(db, sessions=1, gift_sessions=0))
    use(db, card, "a")
    assert card.status == "used_up" or db.get(MemberCard, card.id).status == "used_up"
    svc.adjust(db, 7, card, 2, 0, "补偿", "adj1", at=T0)
    assert card.status == "active" and card.sessions_remaining == 2 and card.gift_units == 2
    assert svc.adjust(db, 7, card, 2, 0, "补偿", "adj1", at=T0)["sessions_remaining"] == 2  # 幂等
    assert reason_of(lambda: svc.adjust(db, 7, card, -3, 0, "x", "adj2", at=T0))[1] == "sessions_negative"
    assert reason_of(lambda: svc.adjust(db, 7, card, 51, 0, "x", "adj3", at=T0))[1] == "adjust_cap"
    period = buy(db, product(db), user_id=2)
    svc.expire_cards(db, T0 + 31 * DAY)
    db.refresh(period)
    assert period.status == "expired"
    svc.adjust(db, 7, period, 0, 5, "延期", "adj4", at=T0 + 31 * DAY)
    assert period.status == "active"
    db.commit()
    assert db.execute(select(func.count()).select_from(AuditLog).where(AuditLog.action == "member_card.adjust")).scalar_one() == 2


# ---------------------------------------------------------------- 定时任务


def test_expiry_and_reminders(db):
    card = buy(db, product(db, period_days=10))
    ends = svc.aware(card.ends_at)
    assert svc.remind_expiring(db, ends - 8 * DAY) == 0
    assert svc.remind_expiring(db, ends - 7 * DAY) == 1
    assert svc.remind_expiring(db, ends - 6 * DAY) == 0
    assert svc.remind_expiring(db, ends - 2 * DAY) == 1   # 3 天档
    assert svc.remind_expiring(db, ends - timedelta(hours=5)) == 1  # 1 天档
    assert svc.remind_expiring(db, ends - timedelta(hours=4)) == 0
    db.commit()
    assert [e["tier"] for e in events(db, "member_card.expiring")] == [7, 3, 1]
    assert svc.expire_cards(db, ends - timedelta(seconds=1)) == 0
    assert svc.expire_cards(db, ends) == 1
    assert svc.expire_cards(db, ends) == 0
    db.commit()
    assert events(db, "member_card.expired")


def test_overdue_refund_flag_and_jobs_registered(db):
    card = buy(db, product(db))
    svc.request_refund(db, card, "customer", "x", "user:1", user_id=1, at=T0 + DAY)
    db.commit()
    assert svc.flag_overdue_refunds(db, T0 + 5 * DAY) == 0
    assert svc.flag_overdue_refunds(db, T0 + 7 * DAY) == 1
    assert svc.flag_overdue_refunds(db, T0 + 8 * DAY) == 0
    from member_card import jobs
    assert isinstance(jobs.JOBS, tuple) and len(jobs.JOBS) == 6
    for job in jobs.JOBS:
        assert job.name.startswith("member_card.") and isinstance(job.fn(db), str)


# ---------------------------------------------------------------- 事件订阅与抽奖延期


def test_subscriptions_do_not_commit_and_dedupe(db):
    from member_card.events import SUBSCRIPTIONS
    assert set(SUBSCRIPTIONS) == {"payment.succeeded", "payment.refunded", "payment.refund_failed", "lottery.prize_won"}
    pid = product(db)
    order = svc.create_order(db, 1, pid, 5, at=T0)
    db.commit()
    payload = {"biz_type": "member_card", "biz_id": order["id"], "amount_cents": 30000}
    assert SUBSCRIPTIONS["payment.succeeded"](db, payload, "ev-1") == "issued"
    db.rollback()  # worker 失败回滚：什么都不留下
    assert db.execute(select(func.count()).select_from(MemberCard)).scalar_one() == 0
    assert SUBSCRIPTIONS["payment.succeeded"](db, payload, "ev-1") == "issued"
    db.commit()
    assert SUBSCRIPTIONS["payment.succeeded"](db, payload, "ev-1") == "duplicate"


def test_lottery_prize_extends_card_once(db, monkeypatch):
    import sys
    import types
    monkeypatch.setitem(sys.modules, "lottery", types.SimpleNamespace())  # 无 ack_delivery → 走事件回执
    card = buy(db, product(db))
    ends = svc.aware(card.ends_at)
    prize = {"draw_id": 11, "member_id": 1, "prize_kind": "member_card_days", "quantity": 7, "ref_id": None,
             "delivery_key": "draw:11:1", "prize_name": "7 天"}
    assert svc.on_prize_won(db, prize, "lp1") == "extended"
    assert svc.on_prize_won(db, prize, "lp2") == "duplicate"
    db.commit()
    db.refresh(card)
    assert svc.aware(card.ends_at) == ends + 7 * DAY and card.gift_units == 7
    results = events(db, "lottery.delivery_result")
    assert results[0] == {"draw_id": 11, "delivery_key": "draw:11:1", "ok": True, "ref": str(card.id), "error": None}
    other = dict(prize, member_id=2, delivery_key="draw:12:1", draw_id=12)
    assert svc.on_prize_won(db, other, "lp3") == "no_card"
    db.commit()
    assert events(db, "lottery.delivery_result")[-1]["error"] == "no_eligible_card"
    assert svc.on_prize_won(db, dict(prize, prize_kind="coupon"), "lp4") == "ignored"


def test_lottery_prize_acks_via_lottery_when_available(db, monkeypatch):
    import sys
    import types
    calls = []
    monkeypatch.setitem(sys.modules, "lottery", types.SimpleNamespace(ack_delivery=lambda *a: calls.append(a)))
    card = buy(db, product(db))
    prize = {"draw_id": 21, "member_id": 1, "prize_kind": "member_card_days", "quantity": 3, "ref_id": card.id,
             "delivery_key": "draw:21:1"}
    assert svc.on_prize_won(db, prize, "lq1") == "extended"
    assert calls == [(db, 21, True, str(card.id), None)]
    assert svc.on_prize_won(db, dict(prize, ref_id=card.id + 999, delivery_key="draw:22:1", draw_id=22), "lq2") == "no_card"
    assert calls[-1][2] is False
