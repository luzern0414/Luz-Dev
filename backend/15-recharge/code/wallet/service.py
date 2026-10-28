"""储值用例：充值单 → 支付 → 批次入账；消费（按比例拆分 + FIFO 批次归属）与冲回；按批次退款（预扣 → 审批 →
原路退款事件 → 结果回写）；后台调整、线下充值、赠送；赠送金到期、超时关单、对账、退款超期提醒。

事务约定：本文件的用例只 flush 不 commit——接口层经 run_idempotent 提交；事件消费 `handle_event` 与定时任务
（jobs.py）是各自的事务边界，自己提交。所有余额与批次变动都是带条件的 UPDATE + rowcount 检查。
"""
from __future__ import annotations

import json
import secrets
from datetime import datetime, timedelta, timezone
from sqlalchemy import func, or_, select
from sqlalchemy import update as _sa_update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from luzback import AppError, ErrorCode, enqueue_event, utcnow, write_audit

from . import config as _config
from .models import (
    ACCOUNT_ACTIVE, ACCOUNT_CLOSED, BATCH_CLOSED, BATCH_EXHAUSTED, BATCH_PAID, BATCH_PENDING, BATCH_REFUNDED,
    BATCH_REFUNDING, L_ADJUST, L_BONUS_EXPIRE, L_CARD_REFUND, L_CONSUME, L_GIFT, L_RECHARGE, L_REFUND_DONE,
    L_REFUND_HOLD, L_REFUND_RELEASE, L_REVERSE, REFUND_FAILED, REFUND_PENDING, REFUND_PROCESSING, REFUND_REJECTED,
    REFUND_SUCCEEDED, SOURCE_ADJUST, SOURCE_DIRECT, SOURCE_GIFT, SOURCE_OFFLINE, SOURCE_PAYMENT, BatchAllocation,
    RechargeBatch, RechargePlan, WalletAccount, WalletLedger, WalletMismatch, WalletRefund,
)
from .ports import GOODS_OFFLINE_SERVICE, PaidEvent, RefundResultEvent, parse_paid, parse_refund_result, payment_port
from .prepaid import policy_for, register_outstanding_provider
from .rules import CAUSE_CUSTOMER, CAUSES, RULE_COOLING_OFF, allocate_fifo, quote_batch_refund, split

BIZ_TYPE = "wallet_recharge"
CONSUMER = "wallet"
REFUND_TOPIC = "wallet.refund_requested"


def update(model):
    """ORM UPDATE 不在会话内求值同步（SQLite 读回的时间是 naive），调用方需要时显式 refresh。"""
    return _sa_update(model).execution_options(synchronize_session=False)


def cfg() -> _config.WalletConfig:
    return _config.CONFIG


def aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _no(prefix: str) -> str:
    return f"{prefix}{utcnow():%y%m%d%H%M%S}{secrets.token_hex(4).upper()}"


# ================================================================ 账户与流水底层


def get_account(db: Session, user_id: int, create: bool = False) -> WalletAccount:
    account = db.execute(select(WalletAccount).where(WalletAccount.user_id == user_id)).scalar_one_or_none()
    if account is None:
        if not create:
            raise AppError(ErrorCode.NOT_FOUND, "wallet not found")
        account = WalletAccount(user_id=user_id)
        db.add(account)
        try:
            db.flush()
        except IntegrityError as exc:  # 并发首次开户
            raise AppError(ErrorCode.DUPLICATE_REQUEST, "wallet is being created, retry", retry_after=1) from exc
    return account


def _require_active(account: WalletAccount) -> None:
    if account.status != ACCOUNT_ACTIVE:
        raise AppError(ErrorCode.BUSINESS_RULE, "wallet closed", {"reason": "wallet_closed"})


def _existing(db: Session, ref: str, kind: str) -> WalletLedger | None:
    return db.execute(select(WalletLedger).where(WalletLedger.ref == ref, WalletLedger.kind == kind)).scalar_one_or_none()


def _move(db: Session, account: WalletAccount, kind: str, ref: str, principal: int = 0, bonus: int = 0,
          frozen_principal: int = 0, frozen_bonus: int = 0, batch_id: int | None = None, store_id: int | None = None,
          operator_id: int | None = None, note: str | None = None, allow_closed: bool = False) -> WalletLedger:
    """账户变动 + 流水。变化量可正可负；条件保证四个余额都不为负。"""
    conditions = [WalletAccount.id == account.id,
                  WalletAccount.principal_cents + principal >= 0, WalletAccount.bonus_cents + bonus >= 0,
                  WalletAccount.frozen_principal_cents + frozen_principal >= 0,
                  WalletAccount.frozen_bonus_cents + frozen_bonus >= 0]
    if not allow_closed:
        conditions.append(WalletAccount.status == ACCOUNT_ACTIVE)
    result = db.execute(update(WalletAccount).where(*conditions).values(
        principal_cents=WalletAccount.principal_cents + principal, bonus_cents=WalletAccount.bonus_cents + bonus,
        frozen_principal_cents=WalletAccount.frozen_principal_cents + frozen_principal,
        frozen_bonus_cents=WalletAccount.frozen_bonus_cents + frozen_bonus,
        version=WalletAccount.version + 1, updated_at=utcnow()))
    if result.rowcount != 1:
        db.refresh(account)
        if account.status != ACCOUNT_ACTIVE and not allow_closed:
            raise AppError(ErrorCode.BUSINESS_RULE, "wallet closed", {"reason": "wallet_closed"})
        raise AppError(ErrorCode.INSUFFICIENT_BALANCE, "insufficient balance")
    after = db.execute(select(WalletAccount.principal_cents, WalletAccount.bonus_cents)
                       .where(WalletAccount.id == account.id)).one()
    entry = WalletLedger(account_id=account.id, user_id=account.user_id, kind=kind, principal_delta=principal,
                         bonus_delta=bonus, frozen_principal_delta=frozen_principal, frozen_bonus_delta=frozen_bonus,
                         principal_after=after[0], bonus_after=after[1], ref=ref, batch_id=batch_id, store_id=store_id,
                         operator_id=operator_id, note=note)
    db.add(entry)
    try:
        db.flush()
    except IntegrityError as exc:  # 同 ref 并发：整笔回滚，调用方重试时走幂等返回
        raise AppError(ErrorCode.DUPLICATE_REQUEST, "same ref is being processed", retry_after=1) from exc
    db.refresh(account)
    return entry


def _debit_batches(db: Session, ledger: WalletLedger, user_id: int, principal: int, bonus: int) -> list[BatchAllocation]:
    """把一次扣减按 FIFO（先入账先用）分配到批次。须在账户条件 UPDATE 之后调用（账户行锁串行化同一会员）。"""
    batches = db.execute(select(RechargeBatch.id, RechargeBatch.principal_remaining_cents, RechargeBatch.bonus_remaining_cents)
                         .where(RechargeBatch.user_id == user_id, RechargeBatch.status == BATCH_PAID)
                         .order_by(RechargeBatch.paid_at, RechargeBatch.id)).all()
    take_p = dict(allocate_fifo(principal, [(b[0], b[1]) for b in batches])) if principal > 0 else {}
    take_b = dict(allocate_fifo(bonus, [(b[0], b[2]) for b in batches])) if bonus > 0 else {}
    allocations = []
    for batch_id in sorted(set(take_p) | set(take_b)):
        p, b = take_p.get(batch_id, 0), take_b.get(batch_id, 0)
        result = db.execute(update(RechargeBatch).where(
            RechargeBatch.id == batch_id, RechargeBatch.status == BATCH_PAID,
            RechargeBatch.principal_remaining_cents >= p, RechargeBatch.bonus_remaining_cents >= b,
        ).values(principal_remaining_cents=RechargeBatch.principal_remaining_cents - p,
                 bonus_remaining_cents=RechargeBatch.bonus_remaining_cents - b, updated_at=utcnow()))
        if result.rowcount != 1:
            raise AppError(ErrorCode.CONFLICT, "batch changed concurrently, retry")
        allocation = BatchAllocation(ledger_id=ledger.id, batch_id=batch_id, principal_cents=p, bonus_cents=b)
        db.add(allocation)
        allocations.append(allocation)
    db.execute(update(RechargeBatch).where(RechargeBatch.id.in_(list(set(take_p) | set(take_b))), RechargeBatch.status == BATCH_PAID,
                                           RechargeBatch.principal_remaining_cents == 0, RechargeBatch.bonus_remaining_cents == 0)
               .values(status=BATCH_EXHAUSTED))
    db.flush()
    return allocations


def _new_batch(db: Session, user_id: int, source: str, amount: int, bonus: int, *, store_id: int | None = None,
               plan_id: int | None = None, source_ref: str | None = None, status: str = BATCH_PAID,
               paid_at: datetime | None = None, bonus_valid_days: int | None = None, operator_id: int | None = None,
               note: str | None = None, terms: str = "") -> RechargeBatch:
    paid = BATCH_PAID == status
    at = paid_at or utcnow()
    batch = RechargeBatch(order_no=_no("W"), user_id=user_id, store_id=store_id, plan_id=plan_id, source=source,
                          source_ref=source_ref, amount_cents=amount, bonus_cents=bonus,
                          principal_remaining_cents=amount if paid else 0, bonus_remaining_cents=bonus if paid else 0,
                          status=status, paid_at=at if paid else None, goods_kind=GOODS_OFFLINE_SERVICE,
                          bonus_expires_at=(at + timedelta(days=bonus_valid_days)) if paid and bonus_valid_days and bonus else None,
                          lpr_bp=cfg().lpr_1y_bp, operator_id=operator_id, note=note, terms_snapshot=terms)
    db.add(batch)
    try:
        db.flush()
    except IntegrityError as exc:
        raise AppError(ErrorCode.DUPLICATE_REQUEST, "same ref is being processed", retry_after=1) from exc
    return batch


# ================================================================ 直接入账 / 消费 / 冲回（供其他模块调用）


def recharge(db: Session, user_id: int, ref: str, principal_cents: int, bonus_cents: int = 0, *,
             store_id: int | None = None, source: str = SOURCE_DIRECT, bonus_valid_days: int | None = None,
             operator_id: int | None = None, note: str | None = None) -> WalletLedger:
    """直接入账一个批次（支付已在别处完成，ref 用业务单号）。幂等：同 ref 返回原流水。"""
    if principal_cents < 0 or bonus_cents < 0 or principal_cents + bonus_cents <= 0:
        raise AppError(ErrorCode.BAD_REQUEST, "invalid recharge amount")
    found = _existing(db, ref, L_RECHARGE)
    if found is not None:
        return found
    account = get_account(db, user_id, create=True)
    _require_active(account)
    batch = _new_batch(db, user_id, source, principal_cents, bonus_cents, store_id=store_id, source_ref=ref,
                       bonus_valid_days=bonus_valid_days, operator_id=operator_id, note=note)
    kind = L_GIFT if source == SOURCE_GIFT else L_RECHARGE
    entry = _move(db, account, kind, ref, principal_cents, bonus_cents, batch_id=batch.id, store_id=store_id,
                  operator_id=operator_id, note=note)
    enqueue_event(db, "wallet.recharged", {"user_id": user_id, "batch_id": batch.id, "amount_cents": principal_cents,
                                           "bonus_cents": bonus_cents, "source": source}, "wallet_batch", batch.id)
    return entry


def grant_bonus(db: Session, user_id: int, ref: str, bonus_cents: int, valid_days: int | None = None,
                store_id: int | None = None) -> WalletLedger:
    """活动赠送金（只有赠送金，不可退）。幂等。"""
    found = _existing(db, ref, L_GIFT)
    if found is not None:
        return found
    return recharge(db, user_id, ref, 0, bonus_cents, store_id=store_id, source=SOURCE_GIFT, bonus_valid_days=valid_days)


def consume(db: Session, user_id: int, ref: str, amount_cents: int, policy: str | None = None,
            store_id: int | None = None) -> WalletLedger:
    """余额支付：按策略拆分本金 / 赠送金，按 FIFO 归属批次。幂等：同 ref 返回原流水。余额不足 4224。"""
    found = _existing(db, ref, L_CONSUME)
    if found is not None:
        if found.user_id != user_id:
            raise AppError(ErrorCode.CONFLICT, "ref used by another wallet")
        return found
    account = get_account(db, user_id)
    _require_active(account)
    principal, bonus = split(amount_cents, account.principal_cents, account.bonus_cents, policy or cfg().split_policy)
    entry = _move(db, account, L_CONSUME, ref, -principal, -bonus, store_id=store_id)
    _debit_batches(db, entry, user_id, principal, bonus)
    enqueue_event(db, "balance.consumed", {"user_id": user_id, "ref": ref, "amount_cents": amount_cents,
                                           "principal_cents": principal, "bonus_cents": bonus}, "wallet", account.id)
    return entry


def reverse(db: Session, user_id: int, ref: str) -> WalletLedger:
    """冲回（订单退款 / 核销失败）：按原消费的拆分与批次原路加回。幂等。"""
    found = _existing(db, ref, L_REVERSE)
    if found is not None:
        return found
    original = _existing(db, ref, L_CONSUME)
    if original is None or original.user_id != user_id:
        raise AppError(ErrorCode.NOT_FOUND, "consume record not found")
    account = get_account(db, user_id)
    entry = _move(db, account, L_REVERSE, ref, -original.principal_delta, -original.bonus_delta, store_id=original.store_id)
    allocations = db.execute(select(BatchAllocation).where(BatchAllocation.ledger_id == original.id)
                             .order_by(BatchAllocation.id)).scalars().all()
    for alloc in allocations:
        result = db.execute(update(RechargeBatch).where(
            RechargeBatch.id == alloc.batch_id, RechargeBatch.status.in_((BATCH_PAID, BATCH_EXHAUSTED)),
        ).values(principal_remaining_cents=RechargeBatch.principal_remaining_cents + alloc.principal_cents,
                 bonus_remaining_cents=RechargeBatch.bonus_remaining_cents + alloc.bonus_cents,
                 status=BATCH_PAID, updated_at=utcnow()))
        if result.rowcount != 1:
            # 原批次已在退款中或已退款：冲回金额进入一个新的直接入账批次（线下退款路径）
            _new_batch(db, user_id, SOURCE_DIRECT, alloc.principal_cents, alloc.bonus_cents,
                       source_ref=f"reverse:{ref}:{alloc.id}", note=f"reverse of batch {alloc.batch_id}")
        db.add(BatchAllocation(ledger_id=entry.id, batch_id=alloc.batch_id, principal_cents=-alloc.principal_cents,
                               bonus_cents=-alloc.bonus_cents))
    db.flush()
    enqueue_event(db, "balance.reverted", {"user_id": user_id, "ref": ref, "principal_cents": -original.principal_delta,
                                           "bonus_cents": -original.bonus_delta}, "wallet", account.id)
    return entry


def refund_card(db: Session, user_id: int, ref: str) -> int:
    """整户退卡（旧接口，线下办理）：退还剩余本金（返回金额），赠送金作废，账户关闭。幂等。

    按批次原路退款请用 request_refund。
    """
    found = _existing(db, ref, L_CARD_REFUND)
    if found is not None:
        return -found.principal_delta
    account = get_account(db, user_id)
    _require_active(account)
    if account.frozen_principal_cents or account.frozen_bonus_cents:
        raise AppError(ErrorCode.CONFLICT, "refund in progress", {"reason": "refund_in_progress"})
    principal, bonus = account.principal_cents, account.bonus_cents
    entry = _move(db, account, L_CARD_REFUND, ref, -principal, -bonus)
    _debit_batches(db, entry, user_id, principal, bonus)
    db.execute(update(WalletAccount).where(WalletAccount.id == account.id).values(status=ACCOUNT_CLOSED))
    enqueue_event(db, "wallet.closed", {"user_id": user_id, "refund_cents": principal, "bonus_forfeit_cents": bonus},
                  "wallet", account.id)
    return principal


def ledger_matches_balance(db: Session, user_id: int) -> bool:
    return not account_discrepancy(db, get_account(db, user_id))


def account_discrepancy(db: Session, account: WalletAccount) -> dict:
    """返回差异（空 = 一致）：余额 = Σ流水；冻结 = Σ冻结流水；可用余额 = Σ可用批次剩余。"""
    sums = db.execute(select(func.coalesce(func.sum(WalletLedger.principal_delta), 0),
                             func.coalesce(func.sum(WalletLedger.bonus_delta), 0),
                             func.coalesce(func.sum(WalletLedger.frozen_principal_delta), 0),
                             func.coalesce(func.sum(WalletLedger.frozen_bonus_delta), 0))
                      .where(WalletLedger.account_id == account.id)).one()
    batches = db.execute(select(func.coalesce(func.sum(RechargeBatch.principal_remaining_cents), 0),
                                func.coalesce(func.sum(RechargeBatch.bonus_remaining_cents), 0))
                         .where(RechargeBatch.user_id == account.user_id,
                                RechargeBatch.status.in_((BATCH_PAID, BATCH_EXHAUSTED)))).one()
    actual = (account.principal_cents, account.bonus_cents, account.frozen_principal_cents, account.frozen_bonus_cents)
    diff = {}
    for name, a, e in zip(("principal", "bonus", "frozen_principal", "frozen_bonus"), actual, tuple(sums)):
        if int(a) != int(e):
            diff[name] = {"balance": int(a), "ledger": int(e)}
    if account.status == ACCOUNT_ACTIVE:
        for name, a, e in zip(("batch_principal", "batch_bonus"), actual[:2], tuple(batches)):
            if int(a) != int(e):
                diff[name] = {"balance": int(a), "batches": int(e)}
    return diff


def outstanding_cents(db: Session, user_id: int) -> int:
    """未兑付预付款（本金 + 退款预扣中的本金），供 PrepaidPolicy 合计上限使用。"""
    row = db.execute(select(WalletAccount.principal_cents, WalletAccount.frozen_principal_cents)
                     .where(WalletAccount.user_id == user_id)).one_or_none()
    return int(row[0] + row[1]) if row else 0


register_outstanding_provider("wallet", outstanding_cents)


# ================================================================ 充值档位


PLAN_FIELDS = ("store_id", "name", "amount_cents", "bonus_cents", "bonus_valid_days", "sort", "sale_start_at",
               "sale_end_at", "per_user_daily_limit", "terms_text")


def plan_dict(plan: RechargePlan) -> dict:
    return {"id": plan.id, "store_id": plan.store_id, "name": plan.name, "amount_cents": plan.amount_cents,
            "bonus_cents": plan.bonus_cents, "bonus_valid_days": plan.bonus_valid_days, "status": plan.status,
            "sort": plan.sort, "sale_start_at": aware(plan.sale_start_at), "sale_end_at": aware(plan.sale_end_at),
            "per_user_daily_limit": plan.per_user_daily_limit, "terms_text": plan.terms_text,
            "goods_kind": GOODS_OFFLINE_SERVICE, "credited_cents": plan.amount_cents + plan.bonus_cents}


def _validate_plan(values: dict) -> None:
    if values.get("amount_cents", 1) <= 0 or values.get("bonus_cents", 0) < 0:
        raise AppError(ErrorCode.BAD_REQUEST, "invalid plan amount")
    start, end = values.get("sale_start_at"), values.get("sale_end_at")
    if start and end and aware(start) >= aware(end):
        raise AppError(ErrorCode.BAD_REQUEST, "sale_start_at must be earlier than sale_end_at")
    policy = policy_for(values.get("store_id"))
    if policy.stored_value_max_cents is not None and values.get("amount_cents", 0) > policy.stored_value_max_cents:
        raise AppError(ErrorCode.LIMIT_REACHED, "单笔充值超过上限", {"reason": "recharge_amount_cap"})
    policy.check_gift(values.get("amount_cents", 0), values.get("bonus_cents", 0))


def create_plan(db: Session, staff_id: int, values: dict) -> dict:
    _validate_plan(values)
    plan = RechargePlan(**{k: values.get(k) for k in PLAN_FIELDS if k in values}, status="off", created_by=staff_id,
                        updated_by=staff_id)
    db.add(plan)
    db.flush()
    write_audit(db, "staff", staff_id, "wallet.plan.create", "wallet_plan", plan.id, after=plan_dict(plan))
    return plan_dict(plan)


def _plan(db: Session, plan_id: int) -> RechargePlan:
    plan = db.get(RechargePlan, plan_id)
    if plan is None or plan.deleted_at is not None:
        raise AppError(ErrorCode.NOT_FOUND, "plan not found")
    return plan


def update_plan(db: Session, staff_id: int, plan_id: int, values: dict) -> dict:
    plan = _plan(db, plan_id)
    before = plan_dict(plan)
    merged = {k: getattr(plan, k) for k in PLAN_FIELDS} | {k: v for k, v in values.items() if k in PLAN_FIELDS}
    _validate_plan(merged)
    for key, value in values.items():
        if key in PLAN_FIELDS:
            setattr(plan, key, value)
    plan.updated_by = staff_id
    db.flush()
    write_audit(db, "staff", staff_id, "wallet.plan.update", "wallet_plan", plan.id, before=before, after=plan_dict(plan))
    return plan_dict(plan)


def set_plan_status(db: Session, staff_id: int, plan_id: int, status: str) -> dict:
    if status not in ("on", "off"):
        raise AppError(ErrorCode.BAD_REQUEST, "status must be on or off")
    plan = _plan(db, plan_id)
    before = plan.status
    db.execute(update(RechargePlan).where(RechargePlan.id == plan_id).values(status=status, updated_by=staff_id))
    db.refresh(plan)
    write_audit(db, "staff", staff_id, f"wallet.plan.{'on' if status == 'on' else 'off'}", "wallet_plan", plan_id,
                before={"status": before}, after={"status": status})
    return plan_dict(plan)


def delete_plan(db: Session, staff_id: int, plan_id: int) -> dict:
    plan = _plan(db, plan_id)
    db.execute(update(RechargePlan).where(RechargePlan.id == plan_id).values(deleted_at=utcnow(), status="off"))
    write_audit(db, "staff", staff_id, "wallet.plan.delete", "wallet_plan", plan_id, before=plan_dict(plan))
    return {"id": plan_id, "deleted": True}


def plans_on_sale_stmt(store_id: int | None, at: datetime):
    stmt = select(RechargePlan).where(RechargePlan.deleted_at.is_(None), RechargePlan.status == "on",
                                      or_(RechargePlan.sale_start_at.is_(None), RechargePlan.sale_start_at <= at),
                                      or_(RechargePlan.sale_end_at.is_(None), RechargePlan.sale_end_at > at))
    if store_id is not None:
        stmt = stmt.where(or_(RechargePlan.store_id.is_(None), RechargePlan.store_id == store_id))
    return stmt


# ================================================================ 充值单（线上支付）


def batch_dict(batch: RechargeBatch, with_pay: dict | None = None) -> dict:
    data = {"id": batch.id, "order_no": batch.order_no, "user_id": batch.user_id, "store_id": batch.store_id,
            "plan_id": batch.plan_id, "source": batch.source, "amount_cents": batch.amount_cents,
            "bonus_cents": batch.bonus_cents, "principal_remaining_cents": batch.principal_remaining_cents,
            "bonus_remaining_cents": batch.bonus_remaining_cents, "refunded_cents": batch.refunded_cents,
            "bonus_forfeited_cents": batch.bonus_forfeited_cents, "bonus_expired_cents": batch.bonus_expired_cents,
            "status": batch.status, "payment_id": batch.payment_id, "paid_at": aware(batch.paid_at),
            "expires_at": aware(batch.expires_at), "bonus_expires_at": aware(batch.bonus_expires_at),
            "needs_manual": batch.needs_manual, "goods_kind": batch.goods_kind,
            "invoice": {"tax_code": batch.invoice_tax_code, "note": batch.invoice_note, "special_invoice_allowed": False},
            "created_at": aware(batch.created_at)}
    if with_pay is not None:
        data["pay_params"] = with_pay
    return data


def create_recharge_order(db: Session, user_id: int, store_id: int | None, plan_id: int | None = None,
                          amount_cents: int | None = None, at: datetime | None = None) -> dict:
    at = at or utcnow()
    if cfg().stop_prepaid:
        raise AppError(ErrorCode.BUSINESS_RULE, "暂停收取预付款", {"reason": "prepaid_stopped"})
    plan = None
    if plan_id is not None:
        plan = db.execute(plans_on_sale_stmt(store_id, at).where(RechargePlan.id == plan_id)).scalar_one_or_none()
        if plan is None:
            raise AppError(ErrorCode.NOT_FOUND, "plan not found or not on sale")
        amount, bonus = plan.amount_cents, plan.bonus_cents
        if plan.per_user_daily_limit:
            since = at - timedelta(days=1)
            count = db.execute(select(func.count()).select_from(RechargeBatch).where(
                RechargeBatch.user_id == user_id, RechargeBatch.plan_id == plan.id, RechargeBatch.created_at >= since,
                RechargeBatch.status.in_((BATCH_PENDING, BATCH_PAID, BATCH_EXHAUSTED, BATCH_REFUNDING)))).scalar_one()
            if count >= plan.per_user_daily_limit:
                raise AppError(ErrorCode.LIMIT_REACHED, "今日该档位充值次数已达上限", {"reason": "daily_limit"})
    else:
        if not cfg().custom_amount_enabled:
            raise AppError(ErrorCode.BAD_REQUEST, "plan_id is required")
        if amount_cents is None or amount_cents < cfg().custom_amount_min_cents:
            raise AppError(ErrorCode.BAD_REQUEST, "amount_cents too small")
        amount, bonus = amount_cents, 0
    account = get_account(db, user_id, create=True)
    _require_active(account)
    policy = policy_for(store_id)
    policy.check_recharge(amount, bonus, account.principal_cents + account.bonus_cents)
    policy.check_member_total(db, user_id, amount)
    batch = _new_batch(db, user_id, SOURCE_PAYMENT, amount, bonus, store_id=store_id, plan_id=plan.id if plan else None,
                       status=BATCH_PENDING, terms=(plan.terms_text if plan else ""))
    batch.expires_at = at + timedelta(minutes=cfg().pay_timeout_minutes)
    pay = payment_port().create_payment(db, biz_type=BIZ_TYPE, biz_id=batch.id, user_id=user_id, store_id=store_id,
                                        amount_cents=amount, description=f"储值充值 {amount / 100:.2f} 元",
                                        goods_kind=GOODS_OFFLINE_SERVICE, expires_at=batch.expires_at)
    batch.payment_id = pay.get("payment_id") or None
    batch.out_trade_no = pay.get("out_trade_no")
    db.flush()
    return batch_dict(batch, pay.get("pay_params") or {})


def on_payment_succeeded(db: Session, event: PaidEvent) -> str:
    """支付成功 → 批次入账。幂等：已入账返回 duplicate；金额不符 → needs_manual，不入账。"""
    if event.biz_type != BIZ_TYPE:
        return "ignored"
    batch = db.get(RechargeBatch, event.biz_id)
    if batch is None:
        return "unknown"
    if batch.status not in (BATCH_PENDING, BATCH_CLOSED):
        return "duplicate"
    if event.amount_cents != batch.amount_cents:
        db.execute(update(RechargeBatch).where(RechargeBatch.id == batch.id).values(needs_manual=True))
        enqueue_event(db, "wallet.payment_mismatch", {"batch_id": batch.id, "expected_cents": batch.amount_cents,
                                                      "paid_cents": event.amount_cents, "payment_id": event.payment_id},
                      "wallet_batch", batch.id)
        return "mismatch"
    plan = db.get(RechargePlan, batch.plan_id) if batch.plan_id else None
    paid_at = event.paid_at
    expires = paid_at + timedelta(days=plan.bonus_valid_days) if plan and plan.bonus_valid_days and batch.bonus_cents else None
    result = db.execute(update(RechargeBatch).where(RechargeBatch.id == batch.id,
                                                    RechargeBatch.status.in_((BATCH_PENDING, BATCH_CLOSED)))
                        .values(status=BATCH_PAID, paid_at=paid_at, principal_remaining_cents=batch.amount_cents,
                                bonus_remaining_cents=batch.bonus_cents, bonus_expires_at=expires,
                                payment_id=event.payment_id or batch.payment_id,
                                out_trade_no=event.out_trade_no or batch.out_trade_no, updated_at=utcnow()))
    if result.rowcount != 1:
        return "duplicate"
    account = get_account(db, batch.user_id, create=True)
    if account.status != ACCOUNT_ACTIVE:  # 已注销的钱包重新启用（支付事实优先）
        db.execute(update(WalletAccount).where(WalletAccount.id == account.id).values(status=ACCOUNT_ACTIVE))
        db.refresh(account)
    _move(db, account, L_RECHARGE, f"recharge:{batch.id}", batch.amount_cents, batch.bonus_cents, batch_id=batch.id,
          store_id=batch.store_id)
    enqueue_event(db, "recharge.succeeded", {"user_id": batch.user_id, "batch_id": batch.id, "store_id": batch.store_id,
                                             "amount_cents": batch.amount_cents, "bonus_cents": batch.bonus_cents,
                                             "payment_id": event.payment_id}, "wallet_batch", batch.id)
    return "credited"


def first_time(db: Session, consumer: str, event_id: str | None) -> bool:
    """事件去重（不回滚会话）：已处理返回 False；否则登记并返回 True。并发重复由唯一约束裁决（抛错 → worker 重试）。"""
    if not event_id:
        return True
    from luzback.outbox import ProcessedEvent  # noqa: PLC0415
    seen = db.execute(select(ProcessedEvent.id).where(ProcessedEvent.consumer == consumer,
                                                      ProcessedEvent.event_id == event_id)).scalar_one_or_none()
    if seen is not None:
        return False
    db.add(ProcessedEvent(consumer=consumer, event_id=event_id))
    db.flush()
    return True


def on_payment_event(db: Session, payload: dict, event_id: str | None = None) -> str:
    """订阅 payment.succeeded（不提交）。"""
    paid = parse_paid(payload)
    if paid is None or paid.biz_type != BIZ_TYPE:
        return "ignored"
    if not first_time(db, f"{CONSUMER}.paid", event_id):
        return "duplicate"
    return on_payment_succeeded(db, paid)


def _on_refund_event(topic: str):
    def handler(db: Session, payload: dict, event_id: str | None = None) -> str:
        result = parse_refund_result(topic, payload)
        if result is None or (result.biz_type and result.biz_type != BIZ_TYPE):
            return "ignored"
        if not first_time(db, f"{CONSUMER}.refund", event_id):
            return "duplicate"
        return on_refund_result(db, result)
    return handler


on_refunded_event = _on_refund_event("payment.refunded")
on_refund_failed_event = _on_refund_event("payment.refund_failed")
EVENT_HANDLERS = {"payment.succeeded": on_payment_event, "payment.refunded": on_refunded_event,
                  "payment.refund_failed": on_refund_failed_event}


def handle_event(db: Session, topic: str, payload: dict, event_id: str | None = None) -> str:
    """便捷入口（测试 / 手工重放）：分发到订阅处理函数并提交事务。worker 走 events.SUBSCRIPTIONS（不提交）。"""
    handler = EVENT_HANDLERS.get(topic)
    if handler is None:
        return "ignored"
    try:
        outcome = handler(db, payload, event_id)
        db.commit()
        return outcome
    except Exception:
        db.rollback()
        raise


def close_expired_orders(db: Session, at: datetime | None = None, limit: int = 500) -> int:
    at = at or utcnow()
    ids = db.execute(select(RechargeBatch.id).where(RechargeBatch.status == BATCH_PENDING, RechargeBatch.expires_at <= at)
                     .order_by(RechargeBatch.id).limit(limit)).scalars().all()
    closed = 0
    for batch_id in ids:
        result = db.execute(update(RechargeBatch).where(RechargeBatch.id == batch_id, RechargeBatch.status == BATCH_PENDING)
                            .values(status=BATCH_CLOSED, closed_at=at))
        if result.rowcount == 1:
            closed += 1
            enqueue_event(db, "wallet.order_closed", {"batch_id": batch_id, "biz_type": BIZ_TYPE}, "wallet_batch", batch_id)
    return closed


# ================================================================ 退款（按批次）


def _batch_for(db: Session, batch_id: int, user_id: int | None) -> RechargeBatch:
    batch = db.get(RechargeBatch, batch_id)
    if batch is None or (user_id is not None and batch.user_id != user_id):
        raise AppError(ErrorCode.NOT_FOUND, "recharge order not found")
    return batch


def _route(batch: RechargeBatch, at: datetime) -> str:
    if batch.source != SOURCE_PAYMENT or not batch.payment_id:
        return "offline"
    paid_at = aware(batch.paid_at)
    if paid_at and (at - paid_at).days >= cfg().original_refund_max_days:
        return "offline"  # 微信支付 365 天外不能原路退
    return "original"


def quote_refund(db: Session, batch_id: int, cause: str = CAUSE_CUSTOMER, user_id: int | None = None,
                 at: datetime | None = None) -> dict:
    at = at or utcnow()
    batch = _batch_for(db, batch_id, user_id)
    if batch.status not in (BATCH_PAID, BATCH_EXHAUSTED):
        raise AppError(ErrorCode.CONFLICT, "recharge order is not refundable in its current status",
                       {"reason": "batch_status", "status": batch.status})
    policy = policy_for(batch.store_id)
    quote = quote_batch_refund(amount=batch.amount_cents, bonus=batch.bonus_cents,
                               principal_remaining=batch.principal_remaining_cents,
                               bonus_remaining=batch.bonus_remaining_cents, cause=cause, paid_at=aware(batch.paid_at),
                               at=at, cooling_off_days=policy.cooling_off_days,
                               fee_bp=cfg().customer_refund_fee_bp if cause == CAUSE_CUSTOMER else 0, lpr_bp=batch.lpr_bp)
    quote["batch_id"] = batch.id
    quote["route"] = _route(batch, at)
    return quote


def refund_dict(refund: WalletRefund) -> dict:
    return {"id": refund.id, "refund_no": refund.refund_no, "user_id": refund.user_id, "batch_id": refund.batch_id,
            "cause": refund.cause, "rule": refund.rule, "refund_cents": refund.refund_cents,
            "interest_cents": refund.interest_cents, "fee_cents": refund.fee_cents,
            "bonus_forfeit_cents": refund.bonus_forfeit_cents, "retained_cents": refund.retained_cents,
            "route": refund.route, "status": refund.status, "reason": refund.reason, "requested_by": refund.requested_by,
            "approved_by": refund.approved_by, "due_at": aware(refund.due_at), "finished_at": aware(refund.finished_at),
            "fail_reason": refund.fail_reason, "breakdown": json.loads(refund.breakdown_json or "{}"),
            "created_at": aware(refund.created_at)}


def request_refund(db: Session, batch_id: int, cause: str, reason: str, actor: str, *, user_id: int | None = None,
                   staff_id: int | None = None, at: datetime | None = None) -> dict:
    """申请按批次退款：计算 → 预扣该批剩余本金与赠送金（冻结）→ 待审批；后台发起或免审批时直接进入处理。"""
    at = at or utcnow()
    if cause not in CAUSES:
        raise AppError(ErrorCode.BAD_REQUEST, "cause must be customer or merchant")
    if user_id is not None and cause != CAUSE_CUSTOMER:
        raise AppError(ErrorCode.BAD_REQUEST, "customers can only request customer-caused refunds")
    quote = quote_refund(db, batch_id, cause, user_id, at)
    if quote["refund_cents"] <= 0 and quote["interest_cents"] <= 0:
        raise AppError(ErrorCode.BUSINESS_RULE, "nothing refundable", {"reason": "nothing_refundable", "quote": quote})
    batch = db.get(RechargeBatch, batch_id)
    hold_p, hold_b = quote["hold_principal_cents"], quote["hold_bonus_cents"]
    result = db.execute(update(RechargeBatch).where(
        RechargeBatch.id == batch.id, RechargeBatch.status.in_((BATCH_PAID, BATCH_EXHAUSTED)),
        RechargeBatch.principal_remaining_cents == hold_p, RechargeBatch.bonus_remaining_cents == hold_b,
    ).values(status=BATCH_REFUNDING, principal_remaining_cents=0, bonus_remaining_cents=0, updated_at=utcnow()))
    if result.rowcount != 1:
        raise AppError(ErrorCode.CONFLICT, "recharge order changed, quote again")
    refund = WalletRefund(refund_no=_no("WR"), user_id=batch.user_id, batch_id=batch.id, cause=cause, rule=quote["rule"],
                          hold_principal_cents=hold_p, hold_bonus_cents=hold_b, refund_cents=quote["refund_cents"],
                          interest_cents=quote["interest_cents"], fee_cents=quote["fee_cents"],
                          bonus_forfeit_cents=quote["bonus_forfeit_cents"], retained_cents=quote["retained_cents"],
                          route=quote["route"], status=REFUND_PENDING, breakdown_json=json.dumps(quote, default=str),
                          reason=reason[:200], requested_by=actor)
    policy = policy_for(batch.store_id)
    days = policy.cooling_off_refund_days if quote["rule"] == RULE_COOLING_OFF else policy.refund_deadline_days
    refund.due_at = at + timedelta(days=days)
    db.add(refund)
    db.flush()
    account = get_account(db, batch.user_id)
    _move(db, account, L_REFUND_HOLD, refund.refund_no, -hold_p, -hold_b, hold_p, hold_b, batch_id=batch.id,
          store_id=batch.store_id, operator_id=staff_id, note=reason[:200], allow_closed=True)
    enqueue_event(db, "wallet.refund_created", {"refund_id": refund.id, "user_id": refund.user_id, "batch_id": batch.id,
                                                "refund_cents": refund.refund_cents}, "wallet_refund", refund.id)
    if staff_id is not None:
        write_audit(db, "staff", staff_id, "wallet.refund.create", "wallet_refund", refund.id, after=refund_dict(refund),
                    reason=reason)
        return approve_refund(db, refund.id, staff_id, audit=False)
    if not cfg().refund_requires_approval:
        return _start_refund(db, refund, None)
    return refund_dict(refund)


def _start_refund(db: Session, refund: WalletRefund, staff_id: int | None) -> dict:
    result = db.execute(update(WalletRefund).where(WalletRefund.id == refund.id, WalletRefund.status == REFUND_PENDING)
                        .values(status=REFUND_PROCESSING, approved_by=staff_id, updated_at=utcnow()))
    if result.rowcount != 1:
        raise AppError(ErrorCode.CONFLICT, "refund status changed", {"status": refund.status})
    db.refresh(refund)
    batch = db.get(RechargeBatch, refund.batch_id)
    enqueue_event(db, REFUND_TOPIC, {
        "business_ref": refund.refund_no, "biz_type": BIZ_TYPE, "biz_id": batch.id, "refund_id": refund.id,
        "payment_id": batch.payment_id, "out_trade_no": batch.out_trade_no, "amount_cents": refund.refund_cents,
        "interest_cents": refund.interest_cents, "route": refund.route, "reason": refund.reason,
        "user_id": refund.user_id}, "wallet_refund", refund.id)
    return refund_dict(refund)


def approve_refund(db: Session, refund_id: int, staff_id: int, audit: bool = True) -> dict:
    refund = db.get(WalletRefund, refund_id)
    if refund is None:
        raise AppError(ErrorCode.NOT_FOUND, "refund not found")
    data = _start_refund(db, refund, staff_id)
    if audit:
        write_audit(db, "staff", staff_id, "wallet.refund.approve", "wallet_refund", refund_id,
                    before={"status": REFUND_PENDING}, after={"status": REFUND_PROCESSING})
    return data


def _release(db: Session, refund: WalletRefund, kind: str = L_REFUND_RELEASE) -> None:
    db.execute(update(RechargeBatch).where(RechargeBatch.id == refund.batch_id, RechargeBatch.status == BATCH_REFUNDING)
               .values(status=BATCH_PAID, principal_remaining_cents=refund.hold_principal_cents,
                       bonus_remaining_cents=refund.hold_bonus_cents, updated_at=utcnow()))
    account = get_account(db, refund.user_id)
    _move(db, account, kind, refund.refund_no, refund.hold_principal_cents, refund.hold_bonus_cents,
          -refund.hold_principal_cents, -refund.hold_bonus_cents, batch_id=refund.batch_id, allow_closed=True)


def reject_refund(db: Session, refund_id: int, staff_id: int, reason: str) -> dict:
    refund = db.get(WalletRefund, refund_id)
    if refund is None:
        raise AppError(ErrorCode.NOT_FOUND, "refund not found")
    result = db.execute(update(WalletRefund).where(WalletRefund.id == refund_id, WalletRefund.status == REFUND_PENDING)
                        .values(status=REFUND_REJECTED, fail_reason=reason[:200], approved_by=staff_id,
                                finished_at=utcnow()))
    if result.rowcount != 1:
        raise AppError(ErrorCode.CONFLICT, "refund status changed")
    db.refresh(refund)
    _release(db, refund)
    write_audit(db, "staff", staff_id, "wallet.refund.reject", "wallet_refund", refund_id,
                before={"status": REFUND_PENDING}, after={"status": REFUND_REJECTED}, reason=reason)
    enqueue_event(db, "wallet.refund_rejected", {"refund_id": refund_id, "user_id": refund.user_id}, "wallet_refund", refund_id)
    return refund_dict(refund)


def _finish(db: Session, refund: WalletRefund, success: bool, reason: str | None, staff_id: int | None = None) -> str:
    target = REFUND_SUCCEEDED if success else REFUND_FAILED
    result = db.execute(update(WalletRefund).where(WalletRefund.id == refund.id, WalletRefund.status == REFUND_PROCESSING)
                        .values(status=target, finished_at=utcnow(), fail_reason=None if success else (reason or "")[:200]))
    if result.rowcount != 1:
        return "duplicate"
    db.refresh(refund)
    if success:
        db.execute(update(RechargeBatch).where(RechargeBatch.id == refund.batch_id, RechargeBatch.status == BATCH_REFUNDING)
                   .values(status=BATCH_REFUNDED, refunded_cents=RechargeBatch.refunded_cents + refund.refund_cents,
                           bonus_forfeited_cents=RechargeBatch.bonus_forfeited_cents + refund.hold_bonus_cents))
        account = get_account(db, refund.user_id)
        _move(db, account, L_REFUND_DONE, refund.refund_no, 0, 0, -refund.hold_principal_cents, -refund.hold_bonus_cents,
              batch_id=refund.batch_id, operator_id=staff_id, allow_closed=True)
        enqueue_event(db, "recharge.refunded", {"refund_id": refund.id, "user_id": refund.user_id, "batch_id": refund.batch_id,
                                                "refund_cents": refund.refund_cents, "interest_cents": refund.interest_cents,
                                                "invoice_action": "red_reverse"}, "wallet_refund", refund.id)
        return "refunded"
    _release(db, refund)
    enqueue_event(db, "wallet.refund_failed", {"refund_id": refund.id, "user_id": refund.user_id, "reason": reason},
                  "wallet_refund", refund.id)
    return "failed"


def on_refund_result(db: Session, event: RefundResultEvent) -> str:
    refund = db.execute(select(WalletRefund).where(WalletRefund.refund_no == event.business_ref)).scalar_one_or_none()
    if refund is None:
        return "unknown"
    return _finish(db, refund, event.success, event.reason)


def complete_offline_refund(db: Session, refund_id: int, staff_id: int, success: bool = True, note: str = "") -> dict:
    """线下退款（非原路）由财务确认结果。"""
    refund = db.get(WalletRefund, refund_id)
    if refund is None:
        raise AppError(ErrorCode.NOT_FOUND, "refund not found")
    if refund.route != "offline":
        raise AppError(ErrorCode.CONFLICT, "only offline refunds can be confirmed manually")
    outcome = _finish(db, refund, success, note or None, staff_id)
    if outcome == "duplicate":
        raise AppError(ErrorCode.CONFLICT, "refund status changed")
    write_audit(db, "staff", staff_id, "wallet.refund.offline_complete", "wallet_refund", refund_id,
                after={"status": refund.status}, reason=note or None)
    return refund_dict(refund)


# ================================================================ 后台：调整、线下充值、门店扣款


def adjust(db: Session, staff_id: int, user_id: int, principal_delta: int, bonus_delta: int, reason: str, ref: str) -> dict:
    if not reason.strip():
        raise AppError(ErrorCode.BAD_REQUEST, "reason is required")
    if principal_delta == 0 and bonus_delta == 0:
        raise AppError(ErrorCode.BAD_REQUEST, "nothing to adjust")
    limit = cfg().adjust_max_cents
    if abs(principal_delta) > limit or abs(bonus_delta) > limit:
        raise AppError(ErrorCode.LIMIT_REACHED, "单次调整超过上限", {"reason": "adjust_cap", "max_cents": limit})
    found = _existing(db, ref, L_ADJUST)
    if found is not None:
        return ledger_dict(found)
    account = get_account(db, user_id, create=True)
    _require_active(account)
    before = {"principal_cents": account.principal_cents, "bonus_cents": account.bonus_cents}
    if principal_delta < 0 or bonus_delta < 0:
        entry = _move(db, account, L_ADJUST, ref, min(principal_delta, 0), min(bonus_delta, 0), operator_id=staff_id,
                      note=reason[:200])
        _debit_batches(db, entry, user_id, -min(principal_delta, 0), -min(bonus_delta, 0))
        if principal_delta > 0 or bonus_delta > 0:  # 一正一负：正数部分另起一个批次
            batch = _new_batch(db, user_id, SOURCE_ADJUST, max(principal_delta, 0), max(bonus_delta, 0),
                               source_ref=f"{ref}:plus", operator_id=staff_id, note=reason[:200])
            entry = _move(db, account, L_ADJUST, f"{ref}:plus", max(principal_delta, 0), max(bonus_delta, 0),
                          batch_id=batch.id, operator_id=staff_id, note=reason[:200])
    else:
        batch = _new_batch(db, user_id, SOURCE_ADJUST, principal_delta, bonus_delta, source_ref=ref, operator_id=staff_id,
                           note=reason[:200])
        entry = _move(db, account, L_ADJUST, ref, principal_delta, bonus_delta, batch_id=batch.id, operator_id=staff_id,
                      note=reason[:200])
    write_audit(db, "staff", staff_id, "wallet.adjust", "wallet", account.id, before=before,
                after={"principal_cents": account.principal_cents, "bonus_cents": account.bonus_cents}, reason=reason)
    enqueue_event(db, "wallet.adjusted", {"user_id": user_id, "principal_delta": principal_delta,
                                          "bonus_delta": bonus_delta}, "wallet", account.id)
    return ledger_dict(entry)


def offline_recharge(db: Session, staff_id: int, user_id: int, store_id: int | None, amount_cents: int, bonus_cents: int,
                     ref: str, note: str = "") -> dict:
    """门店线下收款充值（现金 / 扫码）：同样受限额约束；退款走线下。"""
    if amount_cents <= 0 or bonus_cents < 0:
        raise AppError(ErrorCode.BAD_REQUEST, "invalid amount")
    if cfg().stop_prepaid:
        raise AppError(ErrorCode.BUSINESS_RULE, "暂停收取预付款", {"reason": "prepaid_stopped"})
    found = _existing(db, ref, L_RECHARGE)
    if found is not None:
        return ledger_dict(found)
    account = get_account(db, user_id, create=True)
    _require_active(account)
    policy = policy_for(store_id)
    policy.check_recharge(amount_cents, bonus_cents, account.principal_cents + account.bonus_cents)
    policy.check_member_total(db, user_id, amount_cents)
    entry = recharge(db, user_id, ref, amount_cents, bonus_cents, store_id=store_id, source=SOURCE_OFFLINE,
                     operator_id=staff_id, note=note[:200] or None)
    write_audit(db, "staff", staff_id, "wallet.offline_recharge", "wallet", account.id,
                after={"amount_cents": amount_cents, "bonus_cents": bonus_cents, "store_id": store_id}, reason=note or None)
    return ledger_dict(entry)


def counter_consume(db: Session, staff_id: int, user_id: int, store_id: int | None, amount_cents: int, ref: str,
                    note: str = "") -> dict:
    entry = consume(db, user_id, ref, amount_cents, store_id=store_id)
    write_audit(db, "staff", staff_id, "wallet.consume", "wallet", entry.account_id,
                after={"amount_cents": amount_cents, "ref": ref, "store_id": store_id}, reason=note or None)
    return ledger_dict(entry)


def counter_reverse(db: Session, staff_id: int, user_id: int, ref: str, reason: str) -> dict:
    entry = reverse(db, user_id, ref)
    write_audit(db, "staff", staff_id, "wallet.reverse", "wallet", entry.account_id, after={"ref": ref}, reason=reason)
    return ledger_dict(entry)


# ================================================================ 定时任务主体


def expire_bonus(db: Session, at: datetime | None = None, limit: int = 500) -> int:
    """赠送金按批次到期：扣减该批剩余赠送金（本金不过期）。幂等：按批次流水 ref 去重。"""
    at = at or utcnow()
    rows = db.execute(select(RechargeBatch.id, RechargeBatch.user_id, RechargeBatch.bonus_remaining_cents)
                      .where(RechargeBatch.status == BATCH_PAID, RechargeBatch.bonus_remaining_cents > 0,
                             RechargeBatch.bonus_expires_at.is_not(None), RechargeBatch.bonus_expires_at <= at)
                      .order_by(RechargeBatch.id).limit(limit)).all()
    done = 0
    for batch_id, user_id, bonus in rows:
        ref = f"bonus_expire:{batch_id}"
        if _existing(db, ref, L_BONUS_EXPIRE) is not None:
            continue
        result = db.execute(update(RechargeBatch).where(RechargeBatch.id == batch_id, RechargeBatch.status == BATCH_PAID,
                                                        RechargeBatch.bonus_remaining_cents == bonus)
                            .values(bonus_remaining_cents=0, bonus_expired_cents=RechargeBatch.bonus_expired_cents + bonus))
        if result.rowcount != 1:
            continue
        account = get_account(db, user_id)
        _move(db, account, L_BONUS_EXPIRE, ref, 0, -bonus, batch_id=batch_id, allow_closed=True)
        db.execute(update(RechargeBatch).where(RechargeBatch.id == batch_id, RechargeBatch.principal_remaining_cents == 0,
                                               RechargeBatch.bonus_remaining_cents == 0, RechargeBatch.status == BATCH_PAID)
                   .values(status=BATCH_EXHAUSTED))
        enqueue_event(db, "wallet.bonus_expired", {"user_id": user_id, "batch_id": batch_id, "bonus_cents": bonus},
                      "wallet_batch", batch_id)
        done += 1
    return done


def reconcile(db: Session, at: datetime | None = None) -> dict:
    """对账：逐账户校验 余额 = Σ流水、冻结 = Σ冻结流水、余额 = Σ批次剩余；不一致写 wallet_mismatches 并发告警事件。"""
    at = at or utcnow()
    day = at.date().isoformat()
    checked = mismatched = 0
    last_id = 0
    while True:
        accounts = db.execute(select(WalletAccount).where(WalletAccount.id > last_id).order_by(WalletAccount.id)
                              .limit(500)).scalars().all()
        if not accounts:
            break
        for account in accounts:
            last_id = account.id
            checked += 1
            diff = account_discrepancy(db, account)
            if not diff:
                continue
            mismatched += 1
            exists = db.execute(select(WalletMismatch.id).where(WalletMismatch.account_id == account.id,
                                                                WalletMismatch.check_date == day)).scalar_one_or_none()
            if exists is None:
                db.add(WalletMismatch(account_id=account.id, user_id=account.user_id, check_date=day,
                                      detail_json=json.dumps(diff)))
                enqueue_event(db, "wallet.reconcile_mismatch", {"account_id": account.id, "user_id": account.user_id,
                                                                "date": day, "diff": diff}, "wallet", account.id)
        db.flush()
    return {"checked": checked, "mismatched": mismatched, "date": day}


def flag_overdue_refunds(db: Session, at: datetime | None = None) -> int:
    at = at or utcnow()
    ids = db.execute(select(WalletRefund.id).where(WalletRefund.status.in_((REFUND_PENDING, REFUND_PROCESSING)),
                                                   WalletRefund.due_at <= at, WalletRefund.overdue_notified.is_(False))
                     .order_by(WalletRefund.id).limit(500)).scalars().all()
    for refund_id in ids:
        result = db.execute(update(WalletRefund).where(WalletRefund.id == refund_id, WalletRefund.overdue_notified.is_(False))
                            .values(overdue_notified=True))
        if result.rowcount == 1:
            enqueue_event(db, "wallet.refund_overdue", {"refund_id": refund_id}, "wallet_refund", refund_id)
    return len(ids)


# ================================================================ 查询与序列化


def ledger_dict(entry: WalletLedger) -> dict:
    return {"id": entry.id, "user_id": entry.user_id, "kind": entry.kind, "principal_delta": entry.principal_delta,
            "bonus_delta": entry.bonus_delta, "frozen_principal_delta": entry.frozen_principal_delta,
            "frozen_bonus_delta": entry.frozen_bonus_delta, "principal_after": entry.principal_after,
            "bonus_after": entry.bonus_after, "ref": entry.ref, "batch_id": entry.batch_id, "store_id": entry.store_id,
            "operator_id": entry.operator_id, "note": entry.note, "created_at": aware(entry.created_at)}


def account_dict(db: Session, user_id: int) -> dict:
    account = db.execute(select(WalletAccount).where(WalletAccount.user_id == user_id)).scalar_one_or_none()
    if account is None:
        return {"user_id": user_id, "principal_cents": 0, "bonus_cents": 0, "balance_cents": 0,
                "frozen_principal_cents": 0, "frozen_bonus_cents": 0, "status": ACCOUNT_ACTIVE}
    return {"user_id": user_id, "principal_cents": account.principal_cents, "bonus_cents": account.bonus_cents,
            "balance_cents": account.principal_cents + account.bonus_cents,
            "frozen_principal_cents": account.frozen_principal_cents, "frozen_bonus_cents": account.frozen_bonus_cents,
            "status": account.status, "version": account.version}


def account_row_dict(account: WalletAccount) -> dict:
    return {"id": account.id, "user_id": account.user_id, "principal_cents": account.principal_cents,
            "bonus_cents": account.bonus_cents, "frozen_principal_cents": account.frozen_principal_cents,
            "frozen_bonus_cents": account.frozen_bonus_cents, "status": account.status,
            "updated_at": aware(account.updated_at)}

