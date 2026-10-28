"""储值用例：充值单与支付事件、限额策略、FIFO 批次、冲回、按批次退款（七日 / 消费者原因 / 经营者原因 + 利息）、
预扣与结果回写、调整、线下充值、赠送金到期、对账、超时关单、退款超期、事件去重、并发。"""
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

import wallet
from luzback import AppError, Base, ErrorCode, OutboxEvent
from luzback.audit import AuditLog
from luzback.testing import memory_session
from wallet import config as wconfig
from wallet import service as svc
from wallet.models import BatchAllocation, RechargeBatch, WalletAccount, WalletMismatch, WalletRefund
from wallet.prepaid import CONSERVATIVE, custom_policy, set_policy_resolver
from wallet.rules import quote_batch_refund

T0 = datetime(2026, 10, 1, 2, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def fresh_config():
    wconfig.CONFIG = wconfig.WalletConfig()
    set_policy_resolver(lambda store_id: CONSERVATIVE)
    port = wallet.FakePaymentPort()
    wallet.set_payment_port(port)
    yield port
    set_policy_resolver(None)
    wallet.set_payment_port(wallet.WechatPayPort())


@pytest.fixture()
def db():
    session = memory_session(wallet)
    yield session
    session.close()


def topics(db, topic):
    return [json.loads(e.payload_json) for e in db.execute(select(OutboxEvent).where(OutboxEvent.topic == topic)).scalars()]


def plan(db, amount=100000, bonus=20000, **extra):
    data = svc.create_plan(db, 9, {"name": "充1000送200", "amount_cents": amount, "bonus_cents": bonus, **extra})
    svc.set_plan_status(db, 9, data["id"], "on")
    db.commit()
    return data["id"]


def paid(db, user_id=1, amount=100000, bonus=20000, at=T0, **plan_extra):
    plan_id = plan(db, amount, bonus, **plan_extra)
    order = svc.create_recharge_order(db, user_id, None, plan_id, at=at)
    db.commit()
    payload = {"payment_id": order["payment_id"], "biz_type": "wallet_recharge", "biz_id": order["id"],
               "amount_cents": amount, "paid_at": at.isoformat(), "user_id": user_id}
    assert svc.handle_event(db, "payment.succeeded", payload, event_id=f"evt-{order['id']}") == "credited"
    return order["id"]


def balance(db, user_id=1):
    db.expire_all()
    a = db.execute(select(WalletAccount).where(WalletAccount.user_id == user_id)).scalar_one()
    return a.principal_cents, a.bonus_cents, a.frozen_principal_cents, a.frozen_bonus_cents


# ---------------------------------------------------------------- 充值单与支付


def test_recharge_order_creates_payment_with_offline_service(db, fresh_config):
    plan_id = plan(db)
    order = svc.create_recharge_order(db, 1, 5, plan_id, at=T0)
    db.commit()
    call = fresh_config.calls[0]
    assert call["biz_type"] == "wallet_recharge" and call["goods_kind"] == "offline_service"
    assert call["amount_cents"] == 100000 and order["status"] == "pending" and order["pay_params"]
    assert order["invoice"] == {"tax_code": "601", "note": "预付卡销售和充值 不征税", "special_invoice_allowed": False}


def test_payment_succeeded_credits_once_and_dedupes(db):
    batch_id = paid(db)
    assert balance(db) == (100000, 20000, 0, 0)
    payload = {"biz_type": "wallet_recharge", "biz_id": batch_id, "amount_cents": 100000, "payment_id": "x"}
    assert svc.handle_event(db, "payment.succeeded", payload, event_id=f"evt-{batch_id}") == "duplicate"
    assert svc.handle_event(db, "payment.succeeded", payload, event_id="another") == "duplicate"
    assert balance(db) == (100000, 20000, 0, 0)
    assert len(topics(db, "recharge.succeeded")) == 1
    assert svc.ledger_matches_balance(db, 1)


def test_business_type_alias_and_foreign_events_ignored(db):
    plan_id = plan(db)
    order = svc.create_recharge_order(db, 1, None, plan_id, at=T0)
    db.commit()
    assert svc.handle_event(db, "payment.succeeded", {"business_type": "order", "business_id": 1, "amount_cents": 1}) == "ignored"
    assert svc.handle_event(db, "payment.succeeded", {"business_type": "wallet_recharge", "business_id": order["id"],
                                                      "amount_cents": 100000}) == "credited"


def test_amount_mismatch_marks_manual_without_credit(db):
    plan_id = plan(db)
    order = svc.create_recharge_order(db, 1, None, plan_id, at=T0)
    db.commit()
    out = svc.handle_event(db, "payment.succeeded", {"biz_type": "wallet_recharge", "biz_id": order["id"], "amount_cents": 1})
    assert out == "mismatch"
    assert db.get(RechargeBatch, order["id"]).needs_manual is True
    assert balance(db) == (0, 0, 0, 0) and topics(db, "wallet.payment_mismatch")


def test_late_payment_after_close_still_credits(db):
    plan_id = plan(db)
    order = svc.create_recharge_order(db, 1, None, plan_id, at=T0)
    db.commit()
    assert svc.close_expired_orders(db, T0 + timedelta(minutes=16)) == 1
    db.commit()
    assert db.get(RechargeBatch, order["id"]).status == "closed" and topics(db, "wallet.order_closed")
    svc.handle_event(db, "payment.succeeded", {"biz_type": "wallet_recharge", "biz_id": order["id"], "amount_cents": 100000})
    assert balance(db)[:2] == (100000, 20000)


def test_upstream_failure_rolls_back_order(db, fresh_config):
    plan_id = plan(db)
    fresh_config.fail = True
    with pytest.raises(AppError) as exc:
        svc.create_recharge_order(db, 1, None, plan_id, at=T0)
    db.rollback()
    assert exc.value.code == ErrorCode.UPSTREAM_FAILED
    assert db.execute(select(func.count()).select_from(RechargeBatch)).scalar_one() == 0


def test_plan_must_be_on_sale_and_in_window(db):
    data = svc.create_plan(db, 9, {"name": "x", "amount_cents": 1000, "bonus_cents": 0})
    with pytest.raises(AppError) as exc:
        svc.create_recharge_order(db, 1, None, data["id"], at=T0)
    assert exc.value.code == ErrorCode.NOT_FOUND
    svc.update_plan(db, 9, data["id"], {"sale_start_at": T0 + timedelta(days=1)})
    svc.set_plan_status(db, 9, data["id"], "on")
    with pytest.raises(AppError):
        svc.create_recharge_order(db, 1, None, data["id"], at=T0)
    assert svc.create_recharge_order(db, 1, None, data["id"], at=T0 + timedelta(days=2))["status"] == "pending"


def test_plan_daily_limit(db):
    plan_id = plan(db, 1000, 0, per_user_daily_limit=1)
    svc.create_recharge_order(db, 1, None, plan_id, at=T0)
    with pytest.raises(AppError) as exc:
        svc.create_recharge_order(db, 1, None, plan_id, at=T0)
    assert exc.value.code == ErrorCode.LIMIT_REACHED


def test_custom_amount_disabled_by_default(db):
    with pytest.raises(AppError) as exc:
        svc.create_recharge_order(db, 1, None, None, 5000, at=T0)
    assert exc.value.code == ErrorCode.BAD_REQUEST
    wconfig.CONFIG.custom_amount_enabled = True
    assert svc.create_recharge_order(db, 1, None, None, 5000, at=T0)["amount_cents"] == 5000


# ---------------------------------------------------------------- 限额策略


def test_single_recharge_cap_and_gift_ratio_on_plan(db):
    with pytest.raises(AppError) as exc:
        svc.create_plan(db, 9, {"name": "x", "amount_cents": 500001, "bonus_cents": 0})
    assert exc.value.code == ErrorCode.LIMIT_REACHED
    with pytest.raises(AppError) as exc:
        svc.create_plan(db, 9, {"name": "x", "amount_cents": 100000, "bonus_cents": 25001})
    assert exc.value.data["reason"] == "gift_ratio_exceeded"


def test_balance_cap_after_recharge(db):
    paid(db, amount=400000, bonus=0)
    plan_id = plan(db, 100100, 0)
    with pytest.raises(AppError) as exc:
        svc.create_recharge_order(db, 1, None, plan_id, at=T0)
    assert exc.value.data["reason"] == "balance_cap"


def test_member_total_cap_counts_other_providers(db):
    wallet.register_outstanding_provider("test-cards", lambda _db, uid: 1_950_000 if uid == 1 else 0)
    try:
        plan_id = plan(db, 100000, 0)
        with pytest.raises(AppError) as exc:
            svc.create_recharge_order(db, 1, None, plan_id, at=T0)
        assert exc.value.data["reason"] == "member_total_cap"
        assert svc.create_recharge_order(db, 2, None, plan_id, at=T0)["status"] == "pending"
    finally:
        wallet.register_outstanding_provider("test-cards", lambda _db, uid: 0)


def test_policy_presets_and_custom():
    assert wallet.PRESETS["shanghai_fitness_2025"].period_max_days == 730
    assert wallet.PRESETS["shanghai_fitness_draft_2026"].period_max_cents is None
    policy = custom_policy("conservative", stored_value_max_cents=None)
    policy.check_recharge(10_000_000, 0, 0) if policy.stored_value_max_balance_cents is None else None
    with pytest.raises(AppError):
        policy.check_recharge(600000, 0, 0)  # 余额上限仍在


def test_stop_prepaid_switch(db):
    plan_id = plan(db)
    wconfig.CONFIG.stop_prepaid = True
    with pytest.raises(AppError) as exc:
        svc.create_recharge_order(db, 1, None, plan_id, at=T0)
    assert exc.value.data["reason"] == "prepaid_stopped"


# ---------------------------------------------------------------- 消费、FIFO、冲回


def test_consume_fifo_across_batches_and_reverse(db):
    svc.recharge(db, 1, "a", 1000, 0)
    svc.recharge(db, 1, "b", 1000, 0)
    entry = svc.consume(db, 1, "order:1", 1500)
    allocs = db.execute(select(BatchAllocation).where(BatchAllocation.ledger_id == entry.id)
                        .order_by(BatchAllocation.batch_id)).scalars().all()
    assert [(a.principal_cents) for a in allocs] == [1000, 500]
    first = db.execute(select(RechargeBatch).where(RechargeBatch.source_ref == "a")).scalar_one()
    assert first.status == "exhausted"
    svc.reverse(db, 1, "order:1")
    db.commit()
    db.expire_all()
    assert first.status == "paid" and first.principal_remaining_cents == 1000
    assert svc.ledger_matches_balance(db, 1)


def test_consume_idempotent_and_ref_owned(db):
    svc.recharge(db, 1, "a", 1000, 0)
    svc.recharge(db, 2, "b", 1000, 0)
    svc.consume(db, 1, "order:1", 100)
    with pytest.raises(AppError) as exc:
        svc.consume(db, 2, "order:1", 100)
    assert exc.value.code == ErrorCode.CONFLICT
    with pytest.raises(AppError) as exc:
        svc.reverse(db, 2, "order:1")
    assert exc.value.code == ErrorCode.NOT_FOUND


def test_reverse_into_refunding_batch_creates_new_batch(db):
    batch_id = paid(db, amount=1000, bonus=0)
    svc.consume(db, 1, "order:1", 400)
    svc.request_refund(db, batch_id, "customer", "不想要了", "user:1", user_id=1, at=T0 + timedelta(days=10))
    svc.reverse(db, 1, "order:1")
    db.commit()
    assert balance(db) == (400, 0, 600, 0)
    assert db.execute(select(RechargeBatch).where(RechargeBatch.source_ref.like("reverse:%"))).scalar_one().principal_remaining_cents == 400
    assert svc.ledger_matches_balance(db, 1)


def test_concurrent_consume_50_threads_never_negative(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'w.db'}", future=True, connect_args={"check_same_thread": False, "timeout": 30})
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    with factory() as s:
        svc.recharge(s, 1, "p1", 600, 400)
        svc.recharge(s, 1, "p2", 500, 0)
        s.commit()

    def spend(i):
        with factory() as session:
            try:
                svc.consume(session, 1, f"o:{i}", 100)
                session.commit()
                return "ok"
            except AppError as exc:
                session.rollback()
                return exc.code.name

    with ThreadPoolExecutor(max_workers=50) as pool:
        results = list(pool.map(spend, range(50)))
    assert results.count("ok") == 15
    with factory() as s:
        a = s.execute(select(WalletAccount)).scalar_one()
        assert (a.principal_cents, a.bonus_cents) == (0, 0)
        assert svc.account_discrepancy(s, a) == {}
    engine.dispose()


# ---------------------------------------------------------------- 退款公式


def q(**kw):
    base = dict(amount=100000, bonus=20000, principal_remaining=100000, bonus_remaining=20000, cause="customer",
                paid_at=T0, at=T0 + timedelta(days=3), cooling_off_days=7)
    return quote_batch_refund(**(base | kw))


def test_refund_formula_cooling_off_full():
    out = q()
    assert out["rule"] == "cooling_off_full" and out["refund_cents"] == 100000 and out["bonus_forfeit_cents"] == 20000
    assert q(cause="merchant")["refund_cents"] == 100000
    assert q(at=T0 + timedelta(days=7))["rule"] == "customer_original_price"  # 第 7 日满后不再适用


def test_refund_formula_cooling_off_requires_untouched():
    out = q(principal_remaining=50000, bonus_remaining=10000)
    assert out["rule"] == "customer_original_price"


def test_refund_formula_customer_values_used_at_face_value():
    # 已消费 60000（本金 50000 + 赠送 10000）→ 按原价计 60000，可退 40000，剩余本金中 10000 抵扣已用赠送
    out = q(principal_remaining=50000, bonus_remaining=10000, at=T0 + timedelta(days=30))
    assert (out["used_value_cents"], out["refund_cents"], out["retained_cents"]) == (60000, 40000, 10000)
    # 已消费超过实付：不再追要，可退 0
    out = q(principal_remaining=0, bonus_remaining=0, at=T0 + timedelta(days=30))
    assert out["refund_cents"] == 0


def test_refund_formula_merchant_discounted_with_interest():
    out = q(principal_remaining=50000, bonus_remaining=10000, cause="merchant", at=T0 + timedelta(days=365), lpr_bp=300)
    assert out["used_value_cents"] == 50000 and out["refund_cents"] == 50000
    assert out["interest_cents"] == 50000 * 300 * 365 // (10000 * 365) == 1500


def test_refund_formula_fee_only_customer():
    out = q(principal_remaining=80000, bonus_remaining=20000, at=T0 + timedelta(days=30), fee_bp=500)
    assert out["refund_cents"] == 80000 - 4000 and out["fee_cents"] == 4000
    out = q(principal_remaining=80000, bonus_remaining=20000, cause="merchant", at=T0 + timedelta(days=30), fee_bp=500)
    assert out["fee_cents"] == 0


# ---------------------------------------------------------------- 退款流程


def test_refund_request_holds_then_success_event(db):
    batch_id = paid(db)
    svc.consume(db, 1, "o1", 60000)  # 本金 50000 + 赠送 10000
    db.commit()
    refund = svc.request_refund(db, batch_id, "customer", "搬家", "user:1", user_id=1, at=T0 + timedelta(days=30))
    db.commit()
    assert refund["status"] == "pending_approval" and refund["refund_cents"] == 40000
    assert balance(db) == (0, 0, 50000, 10000)
    with pytest.raises(AppError):
        svc.consume(db, 1, "o2", 1)
    db.rollback()
    svc.approve_refund(db, refund["id"], 7)
    db.commit()
    event = topics(db, "wallet.refund_requested")[0]
    assert event["amount_cents"] == 40000 and event["route"] == "original" and event["payment_id"]
    assert svc.handle_event(db, "payment.refunded", {"business_ref": event["business_ref"], "biz_type": "wallet_recharge",
                                                     "amount_cents": 40000}, event_id="r1") == "refunded"
    assert svc.handle_event(db, "payment.refunded", {"business_ref": event["business_ref"]}, event_id="r2") == "duplicate"
    assert balance(db) == (0, 0, 0, 0)
    batch = db.get(RechargeBatch, batch_id)
    assert batch.status == "refunded" and batch.refunded_cents == 40000 and batch.bonus_forfeited_cents == 10000
    assert topics(db, "recharge.refunded")[0]["invoice_action"] == "red_reverse"
    assert svc.ledger_matches_balance(db, 1)


def test_refund_failure_releases_hold(db):
    batch_id = paid(db, amount=1000, bonus=0)
    refund = svc.request_refund(db, batch_id, "merchant", "门店关闭", "staff:7", staff_id=7, at=T0 + timedelta(days=1))
    db.commit()
    assert refund["status"] == "processing"  # 后台发起即审批
    assert db.execute(select(AuditLog).where(AuditLog.action == "wallet.refund.create")).scalar_one()
    ref = topics(db, "wallet.refund_requested")[0]["business_ref"]
    assert svc.handle_event(db, "payment.refund_failed", {"business_ref": ref, "reason": "ABNORMAL"}) == "failed"
    assert balance(db) == (1000, 0, 0, 0) and db.get(RechargeBatch, batch_id).status == "paid"
    assert svc.ledger_matches_balance(db, 1)


def test_refund_reject_restores_and_state_edges(db):
    batch_id = paid(db, amount=1000, bonus=0)
    refund = svc.request_refund(db, batch_id, "customer", "x", "user:1", user_id=1, at=T0 + timedelta(days=1))
    with pytest.raises(AppError) as exc:  # 退款中的批次不能再申请
        svc.request_refund(db, batch_id, "customer", "x", "user:1", user_id=1, at=T0 + timedelta(days=1))
    assert exc.value.code == ErrorCode.CONFLICT
    svc.reject_refund(db, refund["id"], 7, "资料不全")
    db.commit()
    assert balance(db) == (1000, 0, 0, 0)
    with pytest.raises(AppError) as exc:
        svc.approve_refund(db, refund["id"], 7)
    assert exc.value.code == ErrorCode.CONFLICT


def test_refund_ownership_and_customer_cause_only(db):
    batch_id = paid(db, amount=1000, bonus=0)
    with pytest.raises(AppError) as exc:
        svc.quote_refund(db, batch_id, "customer", user_id=2)
    assert exc.value.code == ErrorCode.NOT_FOUND
    with pytest.raises(AppError) as exc:
        svc.request_refund(db, batch_id, "merchant", "x", "user:1", user_id=1)
    assert exc.value.code == ErrorCode.BAD_REQUEST


def test_nothing_refundable(db):
    batch_id = paid(db, amount=1000, bonus=0)
    svc.consume(db, 1, "o", 1000)
    with pytest.raises(AppError) as exc:
        svc.request_refund(db, batch_id, "customer", "x", "user:1", user_id=1, at=T0 + timedelta(days=10))
    assert exc.value.data["reason"] in ("nothing_refundable",) or exc.value.code == ErrorCode.CONFLICT


def test_offline_route_for_offline_batches_and_manual_completion(db):
    svc.offline_recharge(db, 7, 1, 3, 1000, 0, "cash:1", "现金")
    db.commit()
    batch = db.execute(select(RechargeBatch).where(RechargeBatch.source == "offline")).scalar_one()
    refund = svc.request_refund(db, batch.id, "customer", "x", "staff:7", staff_id=7, at=utcnow_plus(0))
    assert refund["route"] == "offline"
    svc.complete_offline_refund(db, refund["id"], 7, True, "现金退回")
    db.commit()
    assert db.get(WalletRefund, refund["id"]).status == "succeeded" and balance(db) == (0, 0, 0, 0)


def utcnow_plus(days):
    return datetime.now(timezone.utc) + timedelta(days=days)


def test_auto_approve_when_configured(db):
    wconfig.CONFIG.refund_requires_approval = False
    batch_id = paid(db, amount=1000, bonus=0)
    refund = svc.request_refund(db, batch_id, "customer", "x", "user:1", user_id=1, at=T0 + timedelta(days=1))
    assert refund["status"] == "processing"


def test_refund_due_dates_follow_policy(db):
    batch_id = paid(db, amount=1000, bonus=0)
    refund = svc.request_refund(db, batch_id, "customer", "x", "user:1", user_id=1, at=T0 + timedelta(days=1))
    assert refund["due_at"] == T0 + timedelta(days=6)  # 七日内全额：5 日内退还
    db.commit()
    assert svc.flag_overdue_refunds(db, T0 + timedelta(days=7)) == 1
    assert svc.flag_overdue_refunds(db, T0 + timedelta(days=8)) == 0
    db.commit()
    assert topics(db, "wallet.refund_overdue")


# ---------------------------------------------------------------- 后台、到期、对账


def test_adjust_rules_and_audit(db):
    with pytest.raises(AppError) as exc:
        svc.adjust(db, 7, 1, 50001, 0, "补偿", "adj:1")
    assert exc.value.code == ErrorCode.LIMIT_REACHED
    with pytest.raises(AppError):
        svc.adjust(db, 7, 1, 100, 0, "  ", "adj:1")
    svc.adjust(db, 7, 1, 1000, 200, "补偿", "adj:1")
    svc.adjust(db, 7, 1, 1000, 200, "补偿", "adj:1")  # 幂等
    svc.adjust(db, 7, 1, -300, 50, "冲正", "adj:2")
    db.commit()
    assert balance(db)[:2] == (700, 250)
    assert db.execute(select(func.count()).select_from(AuditLog).where(AuditLog.action == "wallet.adjust")).scalar_one() == 2
    assert svc.ledger_matches_balance(db, 1)


def test_bonus_expiry_job(db):
    batch_id = paid(db, amount=10000, bonus=2000, bonus_valid_days=30)
    svc.consume(db, 1, "o", 1200)  # 本金 1000 赠送 200
    db.commit()
    assert svc.expire_bonus(db, T0 + timedelta(days=29)) == 0
    assert svc.expire_bonus(db, T0 + timedelta(days=31)) == 1
    assert svc.expire_bonus(db, T0 + timedelta(days=32)) == 0
    db.commit()
    assert balance(db)[:2] == (9000, 0) and db.get(RechargeBatch, batch_id).bonus_expired_cents == 1800
    assert svc.ledger_matches_balance(db, 1)


def test_reconcile_detects_mismatch_once_per_day(db):
    svc.recharge(db, 1, "a", 1000, 0)
    svc.recharge(db, 2, "b", 1000, 0)
    db.commit()
    assert svc.reconcile(db, T0)["mismatched"] == 0
    db.execute(WalletAccount.__table__.update().where(WalletAccount.user_id == 2).values(principal_cents=999))
    db.commit()
    assert svc.reconcile(db, T0) == {"checked": 2, "mismatched": 1, "date": "2026-10-01"}
    svc.reconcile(db, T0)
    db.commit()
    assert db.execute(select(func.count()).select_from(WalletMismatch)).scalar_one() == 1
    assert len(topics(db, "wallet.reconcile_mismatch")) == 1


def test_jobs_registered_and_run(db):
    from wallet import jobs
    names = {j.name for j in jobs.JOBS}
    assert names == {"wallet.close_expired_orders", "wallet.expire_bonus", "wallet.reconcile", "wallet.flag_overdue_refunds"}
    for job in jobs.JOBS:
        assert isinstance(job.fn(db), str)


def test_grant_bonus_not_refundable(db):
    svc.grant_bonus(db, 1, "camp:1", 500)
    svc.grant_bonus(db, 1, "camp:1", 500)
    db.commit()
    assert balance(db)[:2] == (0, 500)
    batch = db.execute(select(RechargeBatch).where(RechargeBatch.source == "gift")).scalar_one()
    with pytest.raises(AppError):
        svc.request_refund(db, batch.id, "customer", "x", "staff:1", staff_id=1)


def test_subscriptions_are_non_committing_and_idempotent(db):
    from wallet.events import SUBSCRIPTIONS
    assert set(SUBSCRIPTIONS) == {"payment.succeeded", "payment.refunded", "payment.refund_failed"}
    plan_id = plan(db)
    order = svc.create_recharge_order(db, 1, None, plan_id, at=T0)
    db.commit()
    payload = {"biz_type": "wallet_recharge", "biz_id": order["id"], "amount_cents": 100000}
    assert SUBSCRIPTIONS["payment.succeeded"](db, payload, "w-1") == "credited"
    db.rollback()
    assert db.execute(select(func.count()).select_from(WalletAccount).where(WalletAccount.principal_cents > 0)).scalar_one() == 0
    assert SUBSCRIPTIONS["payment.succeeded"](db, payload, "w-1") == "credited"
    db.commit()
    assert SUBSCRIPTIONS["payment.succeeded"](db, payload, "w-1") == "duplicate"
    assert SUBSCRIPTIONS["payment.refunded"](db, {"business_ref": "nope"}, "w-2") == "unknown"
