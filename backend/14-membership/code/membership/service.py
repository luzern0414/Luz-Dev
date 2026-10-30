"""membership 用例：积分入账 / 扣减 / 冲回 / 追回（欠分）/ 赠送 / 过期、对账、会员画像、订单与抽奖事件。

不变量（每日对账核对）：
- points_accounts.balance == Σ points_ledger.delta（按会员）；
- balance == Σ remaining（入账批次尚未用掉的积分，FIFO 扣减与过期）；
- balance ≥ 0（条件 UPDATE），追回不足的差额记 debt，之后的入账先抵销欠分。
幂等：每笔变动一个 ref_key（唯一约束兜底）；同一 ref_key 重复调用返回 False，不再变动。调用方 commit。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from luzback import AppError, ErrorCode, current_config, enqueue_event, utcnow
from luzback.outbox import mark_processed

from . import rules
from .models import (
    KIND_ADJUST, KIND_CLAWBACK, KIND_DEBT, KIND_EARN, KIND_EXPIRE, KIND_GIFT, KIND_LOTTERY, KIND_REDEEM, KIND_REVERT,
    MemberLevel, Member, PointsAccount, PointsLedger, PointsMismatch,
)

CREDIT_KINDS = (KIND_EARN, KIND_GIFT, KIND_LOTTERY, KIND_REVERT, KIND_ADJUST, "signin")
PRIZE_KIND_POINTS = "points"


def _aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _positive(amount: int) -> int:
    if not isinstance(amount, int) or isinstance(amount, bool) or amount <= 0:
        raise AppError(ErrorCode.BAD_REQUEST, "amount must be positive")
    return amount


def _account(db: Session, user_id: int) -> PointsAccount:
    """取（或建）积分账户；并发建户由唯一索引裁决（保存点内重试读取）。"""
    acct = db.execute(select(PointsAccount).where(PointsAccount.user_id == user_id)).scalar_one_or_none()
    if acct is not None:
        return acct
    try:
        with db.begin_nested():
            db.add(PointsAccount(user_id=user_id, balance=0, debt=0))
    except IntegrityError:
        pass
    return db.execute(select(PointsAccount).where(PointsAccount.user_id == user_id)).scalar_one()


def _applied(db: Session, ref_key: str) -> PointsLedger | None:
    return db.execute(select(PointsLedger).where(PointsLedger.ref_key == ref_key)).scalar_one_or_none()


def _write(db: Session, acct: PointsAccount, user_id: int, delta: int, kind: str, ref_key: str, *, remaining: int = 0,
           expires_at: datetime | None = None, note: str | None = None, operator_id: int | None = None) -> PointsLedger:
    db.refresh(acct)
    entry = PointsLedger(user_id=user_id, delta=delta, kind=kind, ref_key=ref_key[:96], balance_after=acct.balance,
                         remaining=remaining, expires_at=expires_at, note=(note or None) and note[:200],
                         operator_id=operator_id)
    db.add(entry)
    try:
        db.flush()
    except IntegrityError as exc:  # 同 ref 并发：整笔回滚，重试时走幂等返回
        raise AppError(ErrorCode.DUPLICATE_REQUEST, "same points operation is being processed", retry_after=1) from exc
    return entry


def points_valid_days() -> int:
    """入账批次有效期（天）：运行配置 [membership] points_valid_days，默认 365（12 个月）；0 = 不过期。"""
    return current_config().membership.points_valid_days


def _default_expiry(now: datetime) -> datetime | None:
    days = points_valid_days()
    return now + timedelta(days=days) if days > 0 else None


def credit(db: Session, user_id: int, amount: int, kind: str, ref_key: str, *, note: str | None = None,
           expires_at: datetime | None = None, operator_id: int | None = None, now: datetime | None = None) -> bool:
    """入账（先抵销欠分，余下进余额并成为一个可过期批次）。重复 ref_key 返回 False。"""
    _positive(amount)
    if _applied(db, ref_key) is not None:
        return False
    now = now or utcnow()
    acct = _account(db, user_id)
    db.refresh(acct)
    used, credited = rules.split_debt(amount, acct.debt)
    result = db.execute(update(PointsAccount).where(PointsAccount.id == acct.id, PointsAccount.debt >= used)
                        .values(balance=PointsAccount.balance + credited, debt=PointsAccount.debt - used,
                                version=PointsAccount.version + 1))
    if result.rowcount != 1:
        raise AppError(ErrorCode.CONFLICT, "points account changed concurrently, retry")
    expiry = expires_at if expires_at is not None else _default_expiry(now)
    note = f"{note or ''}{' ' if note else ''}(抵销欠分 {used})" if used else note
    _write(db, acct, user_id, credited, kind, ref_key, remaining=credited, expires_at=expiry, note=note,
           operator_id=operator_id)
    enqueue_event(db, "points.changed", {"user_id": user_id, "delta": credited, "kind": kind, "ref": ref_key,
                                         "debt_offset": used}, "points_account", acct.id)
    return True


def _consume_batches(db: Session, user_id: int, amount: int) -> None:
    """按 FIFO（先到期先用，不过期的最后）扣减入账批次的 remaining。须在余额条件 UPDATE 之后调用。"""
    left = amount
    rows = db.execute(select(PointsLedger.id, PointsLedger.remaining).where(
        PointsLedger.user_id == user_id, PointsLedger.remaining > 0)
        .order_by(PointsLedger.expires_at.is_(None), PointsLedger.expires_at, PointsLedger.id)).all()
    for ledger_id, remaining in rows:
        if left <= 0:
            break
        take = min(left, remaining)
        if db.execute(update(PointsLedger).where(PointsLedger.id == ledger_id, PointsLedger.remaining >= take)
                      .values(remaining=PointsLedger.remaining - take)).rowcount != 1:
            raise AppError(ErrorCode.CONFLICT, "points batch changed concurrently, retry")
        left -= take
    if left > 0:  # 不变量被破坏（对账会报）：拒绝而不是静默
        raise AppError(ErrorCode.CONFLICT, "points batches do not cover balance", data={"missing": left})


def debit(db: Session, user_id: int, amount: int, kind: str, ref_key: str, *, note: str | None = None,
          operator_id: int | None = None) -> bool:
    """扣减（抵扣订单等）：余额不足 4220；重复 ref_key 返回 False。"""
    _positive(amount)
    if _applied(db, ref_key) is not None:
        return False
    acct = _account(db, user_id)
    result = db.execute(update(PointsAccount).where(PointsAccount.id == acct.id, PointsAccount.balance >= amount)
                        .values(balance=PointsAccount.balance - amount, version=PointsAccount.version + 1))
    if result.rowcount != 1:
        raise AppError(ErrorCode.BUSINESS_RULE, "insufficient points", data={"reason": "insufficient_points"})
    _consume_batches(db, user_id, amount)
    _write(db, acct, user_id, -amount, kind, ref_key, note=note, operator_id=operator_id)
    enqueue_event(db, "points.changed", {"user_id": user_id, "delta": -amount, "kind": kind, "ref": ref_key},
                  "points_account", acct.id)
    return True


# ---------------------------------------------------------------- 对外用例（兼容旧签名）


def earn(db: Session, user_id: int, amount: int, reason: str, ref_key: str, expires_at: datetime | None = None) -> bool:
    """入账积分（消费返积分等）。返回 False 表示该 ref_key 已入账过。调用方 commit。"""
    return credit(db, user_id, amount, KIND_EARN, ref_key, note=reason, expires_at=expires_at)


def spend(db: Session, user_id: int, amount: int, reason: str, ref_key: str) -> bool:
    """扣减积分（如抵扣订单）。余额不足 4220；重复 ref_key 返回 False。调用方 commit。"""
    return debit(db, user_id, amount, KIND_REDEEM, ref_key, note=reason)


def gift_points(db: Session, user_id: int, points: int, ref: str, reason: str = "",
                expires_at: datetime | None = None) -> bool:
    """赠送积分（活动 / 补偿 / 生日礼）：幂等键 ref；返回 False 表示已赠送过。调用方 commit。"""
    return credit(db, user_id, points, KIND_GIFT, ref, note=reason or "gift", expires_at=expires_at)


def revert_spend(db: Session, user_id: int, spend_ref: str, ref_key: str | None = None) -> bool:
    """退回一笔扣减（订单取消 / 退款时返还抵扣的积分）：按原扣减数量入账（新批次）。幂等。"""
    original = _applied(db, spend_ref)
    if original is None or original.user_id != user_id or original.delta >= 0:
        raise AppError(ErrorCode.NOT_FOUND, "points spend not found")
    return credit(db, user_id, -original.delta, KIND_REVERT, ref_key or f"{spend_ref}:revert", note=f"revert {spend_ref}")


def clawback(db: Session, user_id: int, points: int, ref_key: str, reason: str = "") -> int:
    """追回（退款后收回已发积分）：余额够则扣，不够扣到 0，差额记欠分（之后入账先抵销）。返回记入欠分的数量。"""
    _positive(points)
    if _applied(db, ref_key) is not None:
        return 0
    acct = _account(db, user_id)
    db.refresh(acct)
    take = min(points, acct.balance)
    owed = points - take
    result = db.execute(update(PointsAccount).where(PointsAccount.id == acct.id, PointsAccount.balance >= take)
                        .values(balance=PointsAccount.balance - take, debt=PointsAccount.debt + owed,
                                version=PointsAccount.version + 1))
    if result.rowcount != 1:
        raise AppError(ErrorCode.CONFLICT, "points account changed concurrently, retry")
    if take:
        _consume_batches(db, user_id, take)
    _write(db, acct, user_id, -take, KIND_CLAWBACK if not owed else KIND_DEBT, ref_key,
           note=f"{reason or 'clawback'} {points}（欠分 {owed}）" if owed else (reason or "clawback"))
    enqueue_event(db, "points.changed", {"user_id": user_id, "delta": -take, "kind": KIND_CLAWBACK, "ref": ref_key,
                                         "debt_added": owed}, "points_account", acct.id)
    return owed


def balance(db: Session, user_id: int) -> int:
    row = db.execute(select(PointsAccount.balance).where(PointsAccount.user_id == user_id)).scalar_one_or_none()
    return int(row or 0)


def account_dict(db: Session, user_id: int) -> dict:
    acct = db.execute(select(PointsAccount).where(PointsAccount.user_id == user_id)).scalar_one_or_none()
    return {"user_id": user_id, "balance": acct.balance if acct else 0, "debt": acct.debt if acct else 0}


def ledger_dict(e: PointsLedger) -> dict:
    return {"id": e.id, "delta": e.delta, "kind": e.kind, "reason": e.note, "ref": e.ref_key,
            "balance_after": e.balance_after, "expires_at": _aware(e.expires_at), "created_at": _aware(e.created_at)}


# ---------------------------------------------------------------- 过期与对账（jobs.py）


def expire_points(db: Session, at: datetime | None = None, limit: int = 500) -> int:
    """到期批次清零：余额 −remaining（条件 UPDATE），写 expire 流水 ref = expire:{批次}。逐批幂等，可重跑。"""
    at = at or utcnow()
    rows = db.execute(select(PointsLedger).where(PointsLedger.remaining > 0, PointsLedger.expires_at.is_not(None),
                                                 PointsLedger.expires_at <= at)
                      .order_by(PointsLedger.expires_at, PointsLedger.id).limit(limit)).scalars().all()
    done = 0
    for batch in rows:
        amount = batch.remaining
        if db.execute(update(PointsLedger).where(PointsLedger.id == batch.id, PointsLedger.remaining == amount)
                      .values(remaining=0)).rowcount != 1:
            continue
        acct = _account(db, batch.user_id)
        if db.execute(update(PointsAccount).where(PointsAccount.id == acct.id, PointsAccount.balance >= amount)
                      .values(balance=PointsAccount.balance - amount, version=PointsAccount.version + 1)).rowcount != 1:
            raise AppError(ErrorCode.CONFLICT, "points balance below expiring batch", data={"user_id": batch.user_id})
        _write(db, acct, batch.user_id, -amount, KIND_EXPIRE, f"expire:{batch.id}", note=f"batch {batch.id} expired")
        enqueue_event(db, "points.expired", {"user_id": batch.user_id, "points": amount, "batch_id": batch.id},
                      "points_account", acct.id)
        done += 1
    return done


def reconcile(db: Session, at: datetime | None = None) -> int:
    """每日对账：balance != Σdelta 或 != Σremaining 的会员写 points_mismatches（每天每人一行）。返回差异数。"""
    day = (at or utcnow()).date().isoformat()
    sums = dict(db.execute(select(PointsLedger.user_id, func.coalesce(func.sum(PointsLedger.delta), 0))
                           .group_by(PointsLedger.user_id)).all())
    rem = dict(db.execute(select(PointsLedger.user_id, func.coalesce(func.sum(PointsLedger.remaining), 0))
                          .group_by(PointsLedger.user_id)).all())
    found = 0
    for acct in db.execute(select(PointsAccount)).scalars():
        total, left = int(sums.get(acct.user_id, 0)), int(rem.get(acct.user_id, 0))
        if acct.balance == total == left:
            continue
        found += 1
        exists = db.execute(select(PointsMismatch.id).where(PointsMismatch.user_id == acct.user_id,
                                                            PointsMismatch.checked_on == day)).scalar_one_or_none()
        if exists is None:
            db.add(PointsMismatch(user_id=acct.user_id, balance=acct.balance, ledger_sum=total, checked_on=day))
            enqueue_event(db, "points.reconcile_mismatch", {"user_id": acct.user_id, "balance": acct.balance,
                                                            "ledger_sum": total, "remaining_sum": left}, "points_account",
                          acct.id)
    db.flush()
    return found


# ---------------------------------------------------------------- 会员画像（16-coupon 人群）


@dataclass(frozen=True)
class _Profile:
    registered_at: datetime | None = None
    level: int | None = None
    tags: frozenset[str] = frozenset()


def member_profile(db: Session, user_id: int) -> Any:
    """16-coupon 的 ProfileProvider：注册时间（01-identity）、当前等级（members）、标签（v1 无标签模型 → 空集）。

    返回 coupon.MemberProfile（可导入时）或同字段的本地数据类；会员不存在（identity 无此人且无会员行）返回 None。
    """
    member = db.execute(select(Member).where(Member.user_id == user_id)).scalar_one_or_none()
    registered: datetime | None = None
    known = member is not None
    try:
        import identity  # noqa: PLC0415

        user = db.get(identity.User, user_id)
        if user is not None and getattr(user, "deleted_at", None) is None:
            registered, known = _aware(user.created_at), True
        elif user is None and member is None:
            return None
    except ImportError:  # pragma: no cover - 未装配 identity
        pass
    if not known:
        return None
    if registered is None and member is not None:
        registered = _aware(member.joined_at)
    level = member.level if member is not None else 1
    try:
        from coupon import MemberProfile  # noqa: PLC0415

        return MemberProfile(registered_at=registered, level=level, tags=frozenset())
    except ImportError:
        return _Profile(registered_at=registered, level=level, tags=frozenset())


# ---------------------------------------------------------------- 事件（events.py 装配；不提交）


def _multiplier(db: Session, user_id: int) -> int:
    member = db.execute(select(Member).where(Member.user_id == user_id)).scalar_one_or_none()
    if member is None:
        return 10000
    lv = db.execute(select(MemberLevel).where(MemberLevel.level == member.level, MemberLevel.store_id.is_(None),
                                              MemberLevel.deleted_at.is_(None))).scalars().first()
    return int(lv.points_multiplier_bp) if lv is not None else 10000


def _refunded_marker(db: Session, order_id: int) -> int:
    """订单完成前已发生的退款（order.refunded 时记录的 0 积分标记）里最大的累计退款额。"""
    notes = db.execute(select(PointsLedger.note).where(PointsLedger.ref_key.like(f"order:{order_id}:refunded:%"))).scalars()
    best = 0
    for note in notes:
        try:
            best = max(best, int((note or "0").split()[0]))
        except ValueError:
            continue
    return best


def on_order_completed(db: Session, payload: dict, event_id: str) -> str:
    """order.completed → 按实付返积分（规则 1，等级倍率）：ref = order:{id}:earn；已发生的退款先扣除。"""
    if not mark_processed(db, "membership.order_completed", event_id):
        return "duplicate"
    order_id, user_id = int(payload["order_id"]), int(payload["user_id"])
    base = int(payload.get("total_cents") or 0) - _refunded_marker(db, order_id)
    points = rules.points_for_order(base, current_config().membership.points_per_yuan, _multiplier(db, user_id))
    if points <= 0:
        return "no_points"
    return "earned" if earn(db, user_id, points, "order", f"order:{order_id}:earn") else "duplicate"


def on_order_refunded(db: Session, payload: dict, event_id: str) -> str:
    """order.refunded → 按累计退款比例追回订单返的积分（规则 3，向上取整）；未返积分时只记标记。"""
    if not mark_processed(db, "membership.order_refunded", event_id):
        return "duplicate"
    order_id, user_id = int(payload["order_id"]), int(payload["user_id"])
    refunded, total = int(payload.get("refunded_cents") or 0), int(payload.get("total_cents") or 0)
    ref = str(payload.get("ref") or event_id)
    earned = _applied(db, f"order:{order_id}:earn")
    if earned is None:  # 订单还没完成（未返积分）：记 0 积分标记，完成时按扣除退款后的金额返
        acct = _account(db, user_id)
        if _applied(db, f"order:{order_id}:refunded:{ref}") is None:
            _write(db, acct, user_id, 0, KIND_CLAWBACK, f"order:{order_id}:refunded:{ref}", note=f"{refunded} refunded")
        return "marked"
    gross = earned.delta + _debt_offset(earned)
    due = rules.clawback_due(gross, refunded, total)
    already = -sum(int(d) for d in db.execute(select(PointsLedger.delta).where(
        PointsLedger.ref_key.like(f"order:{order_id}:clawback:%"))).scalars())
    owed_before = _owed_recorded(db, order_id)
    take = due - already - owed_before
    if take <= 0:
        return "nothing"
    clawback(db, user_id, take, f"order:{order_id}:clawback:{ref}", reason=f"order {order_id} refund")
    return "clawed_back"


def _debt_offset(entry: PointsLedger) -> int:
    note = entry.note or ""
    if "抵销欠分" not in note:
        return 0
    try:
        return int(note.rsplit("抵销欠分", 1)[1].strip(" )）"))
    except ValueError:
        return 0


def _owed_recorded(db: Session, order_id: int) -> int:
    """该订单追回时记入欠分的数量（流水 note “（欠分 n）”）。"""
    total = 0
    for note in db.execute(select(PointsLedger.note).where(PointsLedger.ref_key.like(f"order:{order_id}:clawback:%"),
                                                           PointsLedger.kind == KIND_DEBT)).scalars():
        try:
            total += int((note or "").rsplit("欠分", 1)[1].strip(" )）"))
        except (IndexError, ValueError):
            continue
    return total


def _ack_prize(db: Session, payload: dict, ok_: bool, ref: str | None, error: str | None) -> None:
    """抽奖发奖回执：18-lottery 可导入时直接 ack_delivery（同事务），否则发 lottery.delivery_result 事件。"""
    try:
        import lottery  # noqa: PLC0415
        ack = getattr(lottery, "ack_delivery")
    except (ImportError, AttributeError):
        ack = None
    if ack is not None:
        ack(db, payload.get("draw_id"), ok_, ref, error)
        return
    enqueue_event(db, "lottery.delivery_result", {"draw_id": payload.get("draw_id"),
                                                  "delivery_key": payload.get("delivery_key"), "ok": ok_, "ref": ref,
                                                  "error": error}, "lottery_draw", payload.get("draw_id") or "")


def on_prize_won(db: Session, payload: dict, event_id: str) -> str:
    """lottery.prize_won（prize_kind=points）：赠送 quantity 积分，ref = delivery_key（重投 / 重发奖只入账一次），回执抽奖。"""
    if payload.get("prize_kind") != PRIZE_KIND_POINTS:
        return "ignored"
    key = str(payload.get("delivery_key") or "")
    if not key:
        return "ignored"
    done = _applied(db, key)
    if done is not None:
        _ack_prize(db, payload, True, str(done.id), None)
        return "duplicate"
    if not mark_processed(db, "membership.prize_won", event_id):
        return "duplicate"
    points = int(payload.get("quantity") or 0)
    member_id = int(payload.get("member_id") or 0)
    if points <= 0 or member_id <= 0:
        _ack_prize(db, payload, False, None, "invalid_quantity" if points <= 0 else "invalid_member")
        return "invalid"
    credit(db, member_id, points, KIND_LOTTERY, key, note=f"draw {payload.get('draw_id')} {payload.get('prize_name') or ''}")
    _ack_prize(db, payload, True, str(_applied(db, key).id), None)
    return "granted"
