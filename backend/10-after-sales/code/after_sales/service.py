"""after_sales 用例：申请（按行数量 / 金额）、审核、退货寄回与收货、原路退款、退款结果、人工处理、超时任务、查询。

金额口径：本行可退上限 = 订单行实付分摊（08 order_items.paid_cents，活动与券已按最大余数法分摊）按数量累计公式计算；
会员卡行再取 29-member-card 退卡试算（法释〔2025〕4号）为上限。额度在订单行上用条件 UPDATE 占用，
所以并发申请的退款合计永远不超过实付。

事务约定：用例不 commit（接口层 run_idempotent / 任务 / 事件 worker 提交）；`submit_refund` 需要调用支付渠道，
会先提交会话再调用 09（SPEC §6.10）。
"""
from __future__ import annotations

import json
import logging
import secrets
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

import order as order_mod
from luzback import AppError, ErrorCode, Field, ListSpec, enqueue_event, utcnow, write_audit

from . import ports
from .config import get_config
from .models import AfterSale, AfterSaleLine, AfterSaleLog
from .rules import (
    AFTER_SALE_FLOW, AWAITING_RETURN, CANCELLED, CLOSED, COMPLETED, OPEN, PENDING_REVIEW, REASON_NO_REASON, REASONS,
    REFUND_ABNORMAL, REFUND_FAILED, REFUND_ONLY, REFUNDING, REJECTED, RELEASES_QUOTA, RETURN_AND_REFUND, RETURN_SHIPPED,
    ROUTE_BALANCE, ROUTE_WECHAT, STATUSES, TABS, TYPES, aware, window_deadline, window_open,
)

log = logging.getLogger("luz.after_sales")

PERMISSIONS = {
    "after_sale:read": "查看售后单",
    "after_sale:export": "导出售后单",
    "after_sale:create": "代客发起售后",
    "after_sale:approve": "审核售后（同意 / 驳回，需二次验证）",
    "after_sale:receive": "确认收到退货",
    "after_sale:refund": "退款失败 / 异常的人工处理（重试、线下结清、关闭，需二次验证）",
}


def _now(now: datetime | None) -> datetime:
    return aware(now) or utcnow()


def _rule(message: str, reason: str, **data: Any) -> AppError:
    return AppError(ErrorCode.BUSINESS_RULE, message, data={"reason": reason, **data})


def business_ref(case_id: int) -> str:
    """09 退款单的 business_ref（09 按前缀 after_sales 路由钩子）。"""
    return f"after_sales:{case_id}"


def _new_no(now: datetime) -> str:
    local = now.astimezone(ZoneInfo("Asia/Shanghai"))
    return f"AS{local.strftime('%y%m%d%H%M%S')}{secrets.randbelow(10**6):06d}"


def _log(db: Session, case_id: int, source: str | None, target: str, actor: tuple[str, Any], note: str | None,
         now: datetime) -> None:
    db.add(AfterSaleLog(after_sale_id=case_id, from_status=source, to_status=target, actor_type=actor[0],
                        actor_id=str(actor[1] or ""), note=note, created_at=now))


def _event(db: Session, topic: str, case: AfterSale, **extra: Any) -> None:
    payload = {"after_sale_id": case.id, "after_sale_no": case.after_sale_no, "order_id": case.order_id,
               "user_id": case.user_id, "store_id": case.store_id, "type": case.type, "status": case.status,
               "amount_cents": case.amount_cents, **extra}
    enqueue_event(db, topic, payload, "after_sale", case.id)


def _lines(db: Session, case_id: int) -> list[AfterSaleLine]:
    return list(db.execute(select(AfterSaleLine).where(AfterSaleLine.after_sale_id == case_id)
                           .order_by(AfterSaleLine.id)).scalars())


def _move(db: Session, case: AfterSale, target: str, actor: tuple[str, Any], note: str | None, now: datetime,
          **values: Any) -> AfterSale:
    source = case.status
    AFTER_SALE_FLOW.advance(db, AfterSale, case.id, source, target, version=AfterSale.version + 1, updated_at=now,
                            **values)
    _log(db, case.id, source, target, actor, note, now)
    db.refresh(case)
    if target in RELEASES_QUOTA:
        order_mod.release_refund(db, case.order_id, [(ln.order_item_id, ln.qty, ln.amount_cents) for ln in _lines(db, case.id)])
    return case


def get_case(db: Session, case_id: int, user_id: int | None = None, store_ids: list[int] | None = None) -> AfterSale:
    case = db.get(AfterSale, case_id)
    if case is None or (user_id is not None and case.user_id != user_id) or (
            store_ids is not None and case.store_id not in store_ids):
        raise AppError(ErrorCode.NOT_FOUND, "after-sale not found")
    return case


# ================================================================ 申请


def refundable(db: Session, user_id: int | None, order_id: int, now: datetime | None = None,
               store_ids: list[int] | None = None) -> dict[str, Any]:
    """申请页：每行可退数量 / 金额（剩余全部退时的上限）、是否适用七日无理由、窗口截止时间。"""
    now = _now(now)
    cfg = get_config()
    order = order_mod.get_owned(db, user_id, order_id) if user_id is not None else order_mod.admin_get(db, order_id, store_ids)
    items = order_mod.order_view(order, _order_items(db, order.id))["items"]
    lines = []
    for it in items:
        quote = order_mod.refund_quote(db, order.id, it["id"], it["qty"] - it["refund_reserved_qty"], now)
        lines.append({"item_id": it["id"], "kind": it["kind"], "name": it["name"], "qty": it["qty"],
                      "paid_cents": it["paid_cents"], "refundable_qty": quote["refundable_qty"],
                      "refundable_cents": quote["max_cents"], "returnable": it["returnable"],
                      "fulfil_status": it["fulfil_status"]})
    fulfilled_at = aware(order.fulfilled_at)
    return {
        "order_id": order.id, "order_no": order.order_no, "status": order.status, "pay_method": order.pay_method,
        "lines": lines, "types": list(TYPES), "reasons": list(REASONS),
        "no_reason_deadline": window_deadline(fulfilled_at, cfg.no_reason_days).isoformat() if fulfilled_at else None,
        "deadline": window_deadline(fulfilled_at, cfg.window_days).isoformat() if fulfilled_at else None,
        "open": order.status in order_mod.ACTIVE_AFTER_PAY and window_open(fulfilled_at, now, cfg.window_days),
    }


def _order_items(db: Session, order_id: int) -> list[Any]:
    return list(db.execute(select(order_mod.OrderItem).where(order_mod.OrderItem.order_id == order_id)
                           .order_by(order_mod.OrderItem.id)).scalars())


def apply(db: Session, user_id: int, body: dict[str, Any], now: datetime | None = None, *,
          staff_id: int | None = None, store_ids: list[int] | None = None) -> dict[str, Any]:
    """申请售后。body = {order_id, type, reason_code, reason_text?, evidence?: [media_id], lines: [{item_id, qty, amount_cents?}]}。

    user_id：顾客本人（他人订单 4040）；staff_id 不为空表示员工代客发起（之后审批人不能是同一员工）。不 commit。
    """
    now = _now(now)
    cfg = get_config()
    kind = body.get("type")
    reason = body.get("reason_code")
    if kind not in TYPES or reason not in REASONS:
        raise AppError(ErrorCode.BAD_REQUEST, "invalid type or reason_code")
    raw_lines = body.get("lines") or []
    if not raw_lines or len(raw_lines) > 50:
        raise AppError(ErrorCode.BAD_REQUEST, "lines required")
    evidence = [int(m) for m in body.get("evidence") or []]
    if len(evidence) > cfg.max_evidence or any(m <= 0 for m in evidence):
        raise AppError(ErrorCode.BAD_REQUEST, f"at most {cfg.max_evidence} evidence images")
    order = (order_mod.admin_get(db, int(body["order_id"]), store_ids) if staff_id is not None
             else order_mod.get_owned(db, user_id, int(body["order_id"])))
    user_id = order.user_id
    if order.status not in order_mod.ACTIVE_AFTER_PAY:
        raise AppError(ErrorCode.CONFLICT, "order is not eligible for after-sales", data={"status": order.status})
    if order.pay_method not in (order_mod.PAY_WECHAT, order_mod.PAY_BALANCE):
        raise _rule("this order has no refundable payment", "no_payment")
    days = cfg.no_reason_days if reason == REASON_NO_REASON else cfg.window_days
    if not window_open(order.fulfilled_at, now, days):
        raise _rule("after-sale window closed", "window_closed", days=days)
    ports.check_evidence(db, user_id, evidence)
    items = {it.id: it for it in _order_items(db, order.id)}
    open_items = set(db.execute(select(AfterSaleLine.order_item_id).join(AfterSale, AfterSale.id == AfterSaleLine.after_sale_id)
                                .where(AfterSale.order_id == order.id, AfterSale.status.in_(OPEN))).scalars())
    planned: list[tuple[Any, int, int]] = []
    seen: set[int] = set()
    for raw in raw_lines:
        item_id, qty = int(raw.get("item_id") or 0), int(raw.get("qty") or 0)
        item = items.get(item_id)
        if item is None or item_id in seen:
            raise AppError(ErrorCode.NOT_FOUND, "order line not found", data={"item_id": item_id})
        seen.add(item_id)
        if item_id in open_items:
            raise AppError(ErrorCode.CONFLICT, "this line already has an after-sale in progress",
                           data={"reason": "after_sale_open", "item_id": item_id})
        if reason == REASON_NO_REASON and not item.returnable:
            raise _rule("this product is not eligible for no-reason returns", "not_returnable", item_id=item_id)
        if kind == RETURN_AND_REFUND and (item.kind != "goods" or qty <= 0):
            raise _rule("return requires physical goods and a quantity", "return_not_applicable", item_id=item_id)
        if qty == 0 and raw.get("amount_cents") is None:
            raise AppError(ErrorCode.BAD_REQUEST, "amount_cents required when qty is 0")
        quote = order_mod.refund_quote(db, order.id, item_id, qty, now)
        amount = quote["max_cents"] if raw.get("amount_cents") is None else int(raw["amount_cents"])
        if amount <= 0 or amount > quote["max_cents"]:
            raise _rule("amount exceeds refundable", "exceeds_refundable", item_id=item_id, max_cents=quote["max_cents"])
        planned.append((item, qty, amount))
    order_mod.reserve_refund(db, order.id, [(it.id, qty, amount) for it, qty, amount in planned])
    total = sum(a for _i, _q, a in planned)
    due = now + timedelta(days=cfg.auto_approve_days) if cfg.auto_approve_days else None
    case = AfterSale(after_sale_no=_new_no(now), order_id=order.id, order_no=order.order_no, user_id=user_id,
                     store_id=order.store_id, type=kind, status=PENDING_REVIEW, reason_code=reason,
                     reason_text=str(body.get("reason_text") or "")[:200], evidence_json=json.dumps(evidence),
                     amount_cents=total, route=ROUTE_BALANCE if order.pay_method == order_mod.PAY_BALANCE else ROUTE_WECHAT,
                     applied_by="staff" if staff_id is not None else "user", applicant_staff_id=staff_id, due_at=due,
                     created_at=now, updated_at=now)
    db.add(case)
    db.flush()
    for item, qty, amount in planned:
        db.add(AfterSaleLine(after_sale_id=case.id, order_item_id=item.id, kind=item.kind, name=item.name, qty=qty,
                             amount_cents=amount, fulfil_ref=item.fulfil_ref))
    _log(db, case.id, None, PENDING_REVIEW, ("staff", staff_id) if staff_id is not None else ("user", user_id),
         case.reason_text or None, now)
    if staff_id is not None:
        write_audit(db, "staff", staff_id, "after_sale.create", "after_sale", case.id,
                    after={"order_id": order.id, "amount_cents": total, "type": kind})
    _event(db, "after_sale.applied", case, reason_code=reason)
    db.flush()
    return case_view(db, case)


# ================================================================ 买家动作


def cancel(db: Session, user_id: int, case_id: int, now: datetime | None = None) -> dict[str, Any]:
    now = _now(now)
    case = get_case(db, case_id, user_id)
    if case.status not in (PENDING_REVIEW, AWAITING_RETURN):
        raise AppError(ErrorCode.CONFLICT, "after-sale cannot be cancelled now", data={"status": case.status})
    _move(db, case, CANCELLED, ("user", user_id), None, now, closed_at=now)
    _event(db, "after_sale.cancelled", case)
    return case_view(db, case)


def ship_return(db: Session, user_id: int, case_id: int, company: str, tracking_no: str,
                now: datetime | None = None) -> dict[str, Any]:
    now = _now(now)
    if not company.strip() or not tracking_no.strip():
        raise AppError(ErrorCode.BAD_REQUEST, "company and tracking_no required")
    case = get_case(db, case_id, user_id)
    if case.status != AWAITING_RETURN:
        raise AppError(ErrorCode.CONFLICT, "return is not expected now", data={"status": case.status})
    _move(db, case, RETURN_SHIPPED, ("user", user_id), None, now, return_company=company.strip()[:64],
          return_tracking_no=tracking_no.strip()[:64], return_shipped_at=now, due_at=None)
    _event(db, "after_sale.return_shipped", case, tracking_no=case.return_tracking_no)
    return case_view(db, case)


# ================================================================ 审核 / 收货


def _restock_flags(db: Session, case: AfterSale, received: bool) -> None:
    """退货退款收到货 → 回库；仅退款且订单还没发货（paid）→ 货没出库，也回库。"""
    order = order_mod.admin_get(db, case.order_id)
    for ln in _lines(db, case.id):
        restock = ln.kind == "goods" and ln.qty > 0 and (received or order.status == order_mod.PAID)
        db.execute(update(AfterSaleLine).where(AfterSaleLine.id == ln.id).values(restock=restock))


def approve(db: Session, staff_id: int | None, case_id: int, now: datetime | None = None, *,
            store_ids: list[int] | None = None, note: str | None = None) -> dict[str, Any]:
    """同意：仅退款 → 进入退款；退货退款 → 等待买家寄回（截止 AFTER_SALE_RETURN_SHIP_DAYS 天）。

    staff_id=None 表示系统自动同意（超时任务）。职责分离：代客发起的员工不能审批自己的申请。不 commit。
    """
    now = _now(now)
    case = get_case(db, case_id, store_ids=store_ids)
    if case.status != PENDING_REVIEW:
        raise AppError(ErrorCode.CONFLICT, "after-sale is not pending review", data={"status": case.status})
    if staff_id is not None and case.applicant_staff_id is not None and case.applicant_staff_id == staff_id:
        raise AppError(ErrorCode.FORBIDDEN, "separation of duties: you cannot approve your own request",
                       data={"reason": "separation_of_duties"})
    actor = ("staff", staff_id) if staff_id is not None else ("system", "auto_approve")
    review = dict(reviewed_by=staff_id, reviewed_at=now)
    if case.type == REFUND_ONLY:
        _restock_flags(db, case, received=False)
        _move(db, case, REFUNDING, actor, note, now, due_at=now, **review)
    else:
        _move(db, case, AWAITING_RETURN, actor, note, now, due_at=now + timedelta(days=get_config().return_ship_days),
              **review)
    if staff_id is not None:
        write_audit(db, "staff", staff_id, "after_sale.approve", "after_sale", case.id,
                    before={"status": PENDING_REVIEW}, after={"status": case.status, "amount_cents": case.amount_cents})
    _event(db, "after_sale.approved", case, auto=staff_id is None)
    return case_view(db, case)


def reject(db: Session, staff_id: int, case_id: int, reason: str, now: datetime | None = None, *,
           store_ids: list[int] | None = None) -> dict[str, Any]:
    now = _now(now)
    reason = (reason or "").strip()
    if not 2 <= len(reason) <= 200:
        raise AppError(ErrorCode.BAD_REQUEST, "reject reason required (2-200 chars)")
    case = get_case(db, case_id, store_ids=store_ids)
    if case.status not in (PENDING_REVIEW, RETURN_SHIPPED):
        raise AppError(ErrorCode.CONFLICT, "after-sale cannot be rejected now", data={"status": case.status})
    before = case.status
    _move(db, case, REJECTED, ("staff", staff_id), reason, now, reject_reason=reason, reviewed_by=staff_id,
          reviewed_at=now, closed_at=now, due_at=None)
    write_audit(db, "staff", staff_id, "after_sale.reject", "after_sale", case.id, before={"status": before},
                after={"status": REJECTED}, reason=reason)
    _event(db, "after_sale.rejected", case, reason=reason)
    return case_view(db, case)


def receive_return(db: Session, staff_id: int, case_id: int, now: datetime | None = None, *,
                   store_ids: list[int] | None = None) -> dict[str, Any]:
    """确认收到退货 → 立即进入退款（七日办法第十三条：收到退货之日起七日内返还价款）。不 commit。"""
    now = _now(now)
    case = get_case(db, case_id, store_ids=store_ids)
    if case.status != RETURN_SHIPPED:
        raise AppError(ErrorCode.CONFLICT, "return has not been shipped", data={"status": case.status})
    _restock_flags(db, case, received=True)
    _move(db, case, REFUNDING, ("staff", staff_id), None, now, return_received_at=now, received_by=staff_id, due_at=now)
    write_audit(db, "staff", staff_id, "after_sale.receive", "after_sale", case.id, after={"status": REFUNDING})
    _event(db, "after_sale.return_received", case)
    return case_view(db, case)


# ================================================================ 退款


def _complete(db: Session, case: AfterSale, actor: tuple[str, Any], note: str | None, now: datetime,
              refunded_at: datetime | None = None) -> AfterSale:
    """退款成功：订单计入已退（回库存 / 终止权益 / 按券策略退券 / 全退则订单 refunded）→ 售后完成 → after_sale.completed。"""
    lines = _lines(db, case.id)
    _move(db, case, COMPLETED, actor, note, now, refunded_at=refunded_at or now, closed_at=now, due_at=None)
    result = order_mod.settle_refund(db, case.order_id,
                                     [(ln.order_item_id, ln.qty, ln.amount_cents, ln.restock) for ln in lines],
                                     business_ref(case.id), now)
    _event(db, "after_sale.completed", case, route=case.route, order_full_refund=result["full"],
           lines=[{"order_item_id": ln.order_item_id, "kind": ln.kind, "qty": ln.qty, "amount_cents": ln.amount_cents,
                   "restock": ln.restock, "fulfil_ref": ln.fulfil_ref} for ln in lines])
    return case


def submit_refund(db: Session, case_id: int, now: datetime | None = None) -> str:
    """提交原路退款（会提交会话）：余额 → 按消费流水冲回并同事务完成；微信 → 09 create_refund（结果走 refund.* 事件）。

    返回 submitted / completed / scheduled / failed / skipped。可重复调用：已有有效退款单时直接采纳，不会重复退款。
    """
    now = _now(now)
    cfg = get_config()
    case = db.get(AfterSale, case_id)
    if case is None or case.status != REFUNDING or case.refund_id is not None:
        return "skipped"
    order = order_mod.admin_get(db, case.order_id)
    if case.route == ROUTE_BALANCE:
        order_mod.refund_balance(db, order.id, [(ln.order_item_id, ln.amount_cents) for ln in _lines(db, case.id)],
                                 ref=business_ref(case.id))
        _complete(db, case, ("system", "balance"), "balance reversed", now)
        db.commit()
        return "completed"
    gateway = ports.refund_gateway()
    ref = business_ref(case.id)
    found = gateway.existing(db, ref)
    if found is None:
        db.commit()  # 渠道调用前结束事务
        try:
            found = gateway.create(db, int(order.payment_id), case.amount_cents, f"售后 {case.after_sale_no}", ref, now)
        except AppError as exc:
            db.rollback()
            data = exc.data if isinstance(exc.data, dict) else {}
            retryable = exc.code >= 5000 or data.get("reason") == "refund_interval"
            if retryable:
                delay = int(data.get("retry_after_s") or cfg.refund_retry_seconds)
                db.execute(update(AfterSale).where(AfterSale.id == case.id, AfterSale.status == REFUNDING,
                                                   AfterSale.refund_id.is_(None))
                           .values(due_at=now + timedelta(seconds=delay), refund_attempts=AfterSale.refund_attempts + 1,
                                   refund_error=f"{int(exc.code)} {exc.message}"[:200]))
                db.commit()
                return "scheduled"
            case = db.get(AfterSale, case_id)
            db.refresh(case)
            if case.status == REFUNDING:
                _move(db, case, REFUND_FAILED, ("system", "refund"), exc.message, now,
                      refund_error=f"{int(exc.code)} {exc.message}"[:200], refund_attempts=case.refund_attempts + 1,
                      due_at=None)
                _event(db, "after_sale.refund_failed", case, error=exc.message)
            db.commit()
            return "failed"
    db.execute(update(AfterSale).where(AfterSale.id == case_id, AfterSale.refund_id.is_(None))
               .values(refund_id=found["refund_id"], out_refund_no=found["out_refund_no"], due_at=None,
                       refund_attempts=AfterSale.refund_attempts + 1, refund_error=None))
    db.commit()
    return "submitted"


def on_refund_result(db: Session, payload: dict[str, Any], status: str, now: datetime | None = None) -> str:
    """09 退款结果事件：succeeded → 完成；failed → 人工；abnormal → 异常（09 后台发起异常退款后再 succeeded）。不 commit。"""
    now = _now(now)
    ref = str(payload.get("business_ref") or "")
    if not ref.startswith("after_sales:"):
        return "ignored"
    case = db.get(AfterSale, int(ref.split(":", 1)[1]))
    if case is None:
        return "unknown"
    refund_id = payload.get("refund_id")
    if case.refund_id is not None and refund_id is not None and int(refund_id) != case.refund_id:
        return "stale"  # 旧的一次退款尝试
    link = {} if case.refund_id is not None else {"refund_id": refund_id, "out_refund_no": payload.get("out_refund_no")}
    if status == "succeeded":
        if case.status not in (REFUNDING, REFUND_ABNORMAL, REFUND_FAILED):
            return "duplicate"
        if case.status == REFUND_FAILED:
            _move(db, case, REFUNDING, ("payment", refund_id), "late success", now, **link)
            link = {}
        if link:
            db.execute(update(AfterSale).where(AfterSale.id == case.id).values(**link))
        _complete(db, case, ("payment", refund_id), None, now)
        return "completed"
    if status == "abnormal":
        if case.status != REFUNDING:
            return "duplicate"
        _move(db, case, REFUND_ABNORMAL, ("payment", refund_id), "refund abnormal", now, **link)
        _event(db, "after_sale.refund_abnormal", case, refund_id=refund_id)
        return "abnormal"
    if case.status not in (REFUNDING, REFUND_ABNORMAL):
        return "duplicate"
    _move(db, case, REFUND_FAILED, ("payment", refund_id), str(payload.get("reason") or "failed")[:200], now,
          refund_error=str(payload.get("reason") or "failed")[:200], **link)
    _event(db, "after_sale.refund_failed", case, refund_id=refund_id, error=payload.get("reason"))
    return "failed"


def retry_refund(db: Session, staff_id: int, case_id: int, now: datetime | None = None, *,
                 store_ids: list[int] | None = None) -> dict[str, Any]:
    """退款失败后重新发起（09 会生成新的退款单号；失败的那笔额度已释放）。不 commit；接口层提交后调用 submit_refund。"""
    now = _now(now)
    case = get_case(db, case_id, store_ids=store_ids)
    if case.status != REFUND_FAILED:
        raise AppError(ErrorCode.CONFLICT, "only failed refunds can be retried", data={"status": case.status})
    _move(db, case, REFUNDING, ("staff", staff_id), "retry", now, refund_id=None, out_refund_no=None, due_at=now)
    write_audit(db, "staff", staff_id, "after_sale.retry_refund", "after_sale", case.id, after={"status": REFUNDING})
    return case_view(db, case)


def manual_settle(db: Session, staff_id: int, case_id: int, note: str, now: datetime | None = None, *,
                  store_ids: list[int] | None = None) -> dict[str, Any]:
    """原路无法退回时人工结清（须与消费者约定且不得附加条件，运营规范 §2.14 第 6 条）：记录说明并完成。不 commit。"""
    now = _now(now)
    note = (note or "").strip()
    if not 4 <= len(note) <= 200:
        raise AppError(ErrorCode.BAD_REQUEST, "note required (4-200 chars)")
    case = get_case(db, case_id, store_ids=store_ids)
    if case.status not in (REFUND_FAILED, REFUND_ABNORMAL):
        raise AppError(ErrorCode.CONFLICT, "only failed or abnormal refunds can be settled manually",
                       data={"status": case.status})
    db.execute(update(AfterSale).where(AfterSale.id == case.id).values(settle_note=note))
    _complete(db, case, ("staff", staff_id), note, now)
    write_audit(db, "staff", staff_id, "after_sale.manual_settle", "after_sale", case.id, after={"status": COMPLETED},
                reason=note)
    return case_view(db, case)


def admin_close(db: Session, staff_id: int, case_id: int, reason: str, now: datetime | None = None, *,
                store_ids: list[int] | None = None) -> dict[str, Any]:
    """退款失败且双方同意不再退款：关闭并释放额度（原因必填，审计）。"""
    now = _now(now)
    reason = (reason or "").strip()
    if not 2 <= len(reason) <= 200:
        raise AppError(ErrorCode.BAD_REQUEST, "reason required")
    case = get_case(db, case_id, store_ids=store_ids)
    if case.status != REFUND_FAILED:
        raise AppError(ErrorCode.CONFLICT, "only failed refunds can be closed", data={"status": case.status})
    _move(db, case, CLOSED, ("staff", staff_id), reason, now, closed_at=now, due_at=None)
    write_audit(db, "staff", staff_id, "after_sale.close", "after_sale", case.id, after={"status": CLOSED}, reason=reason)
    _event(db, "after_sale.closed", case, reason=reason)
    return case_view(db, case)


# ================================================================ 任务


def auto_approve(db: Session, now: datetime | None = None) -> list[int]:
    """商家超时未处理自动同意（AFTER_SALE_AUTO_APPROVE_DAYS > 0 时启用）。逐单提交。"""
    now = _now(now)
    if not get_config().auto_approve_days:
        return []
    ids = db.execute(select(AfterSale.id).where(AfterSale.status == PENDING_REVIEW, AfterSale.due_at <= now)
                     .order_by(AfterSale.id).limit(get_config().job_batch)).scalars().all()
    done = []
    for case_id in ids:
        try:
            approve(db, None, case_id, now, note="auto approved: merchant did not respond")
            db.commit()
            done.append(case_id)
        except AppError:
            db.rollback()
    return done


def close_unshipped(db: Session, now: datetime | None = None) -> list[int]:
    """买家超过 AFTER_SALE_RETURN_SHIP_DAYS 天未寄回 → 关闭并释放额度。逐单提交。"""
    now = _now(now)
    ids = db.execute(select(AfterSale.id).where(AfterSale.status == AWAITING_RETURN, AfterSale.due_at <= now)
                     .order_by(AfterSale.id).limit(get_config().job_batch)).scalars().all()
    done = []
    for case_id in ids:
        case = db.get(AfterSale, case_id)
        try:
            _move(db, case, CLOSED, ("system", "return_timeout"), "buyer did not ship the return in time", now,
                  closed_at=now, due_at=None)
            _event(db, "after_sale.closed", case, reason="return_timeout")
            db.commit()
            done.append(case_id)
        except AppError:
            db.rollback()
    return done


def submit_due_refunds(db: Session, now: datetime | None = None) -> dict[str, int]:
    """提交到期的退款（审批 / 收货后首次提交失败、1 分钟间隔限制、渠道不可用、进程崩溃的补偿）。"""
    now = _now(now)
    ids = db.execute(select(AfterSale.id).where(AfterSale.status == REFUNDING, AfterSale.refund_id.is_(None),
                                                AfterSale.due_at <= now)
                     .order_by(AfterSale.due_at, AfterSale.id).limit(get_config().job_batch)).scalars().all()
    counts: dict[str, int] = {}
    for case_id in ids:
        try:
            outcome = submit_refund(db, case_id, now)
        except Exception:  # noqa: BLE001 - 单个失败不影响其他
            db.rollback()
            log.exception("after_sale refund submit failed %s", case_id)
            outcome = "error"
        counts[outcome] = counts.get(outcome, 0) + 1
    return counts


# ================================================================ 查询


def _iso(value: datetime | None) -> str | None:
    value = aware(value)
    return value.isoformat() if value else None


def case_row(case: AfterSale) -> dict[str, Any]:
    return {"id": case.id, "after_sale_no": case.after_sale_no, "order_id": case.order_id, "order_no": case.order_no,
            "store_id": case.store_id, "type": case.type, "status": case.status, "reason_code": case.reason_code,
            "amount_cents": case.amount_cents, "route": case.route, "applied_by": case.applied_by,
            "created_at": _iso(case.created_at), "refunded_at": _iso(case.refunded_at)}


def case_view(db: Session, case: AfterSale, *, admin: bool = False) -> dict[str, Any]:
    data = case_row(case)
    data.update(reason_text=case.reason_text, evidence=json.loads(case.evidence_json or "[]"),
                reject_reason=case.reject_reason, reviewed_at=_iso(case.reviewed_at), due_at=_iso(case.due_at),
                return_company=case.return_company, return_tracking_no=case.return_tracking_no,
                return_shipped_at=_iso(case.return_shipped_at), return_received_at=_iso(case.return_received_at),
                out_refund_no=case.out_refund_no, closed_at=_iso(case.closed_at),
                lines=[{"order_item_id": ln.order_item_id, "kind": ln.kind, "name": ln.name, "qty": ln.qty,
                        "amount_cents": ln.amount_cents, "restock": ln.restock} for ln in _lines(db, case.id)],
                timeline=[{"from": lg.from_status, "to": lg.to_status, "actor_type": lg.actor_type,
                           "note": lg.note if admin else None, "at": _iso(lg.created_at)}
                          for lg in db.execute(select(AfterSaleLog).where(AfterSaleLog.after_sale_id == case.id)
                                               .order_by(AfterSaleLog.id)).scalars()])
    if admin:
        data.update(user_id=case.user_id, applicant_staff_id=case.applicant_staff_id, reviewed_by=case.reviewed_by,
                    received_by=case.received_by, refund_id=case.refund_id, refund_attempts=case.refund_attempts,
                    refund_error=case.refund_error, settle_note=case.settle_note)
    return data


APP_LIST = ListSpec(
    fields={"created_at": Field(AfterSale.created_at, kind="datetime", ops=("range",), sortable=True)},
    id_column=AfterSale.id, default_sort="-created_at", extra_params=frozenset({"tab"}),
)


def app_list(db: Session, user_id: int, params: dict[str, Any], secret: str) -> dict[str, Any]:
    tab = params.get("tab") or "all"
    if tab not in TABS:
        raise AppError(ErrorCode.BAD_REQUEST, f"tab must be one of {', '.join(TABS)}")
    stmt = select(AfterSale).where(AfterSale.user_id == user_id, AfterSale.status.in_(TABS[tab]))
    page = APP_LIST.cursor(db, stmt, params, secret, serialize=case_row)
    rows = dict(db.execute(select(AfterSale.status, func.count()).where(AfterSale.user_id == user_id)
                           .group_by(AfterSale.status)).all())
    page["counts"] = {t: sum(rows.get(s, 0) for s in sts) for t, sts in TABS.items()}
    return page


ADMIN_LIST = ListSpec(
    fields={
        "status": Field(AfterSale.status, ops=("eq", "in"), choices=frozenset(STATUSES)),
        "type": Field(AfterSale.type, ops=("eq", "in"), choices=frozenset(TYPES)),
        "reason_code": Field(AfterSale.reason_code, ops=("eq", "in"), choices=frozenset(REASONS)),
        "route": Field(AfterSale.route, ops=("eq", "in"), choices=frozenset((ROUTE_WECHAT, ROUTE_BALANCE))),
        "store_id": Field(AfterSale.store_id, kind="int", ops=("eq", "in")),
        "order_id": Field(AfterSale.order_id, kind="int"),
        "user_id": Field(AfterSale.user_id, kind="int"),
        "amount_cents": Field(AfterSale.amount_cents, kind="int", ops=("range",), sortable=True),
        "created_at": Field(AfterSale.created_at, kind="datetime", ops=("range",), sortable=True),
        "refunded_at": Field(AfterSale.refunded_at, kind="datetime", ops=("range", "null"), sortable=True),
    },
    id_column=AfterSale.id,
    keyword_columns=(AfterSale.after_sale_no, AfterSale.order_no, AfterSale.return_tracking_no, AfterSale.out_refund_no),
    default_sort="-created_at",
)


def admin_query(store_ids: list[int] | None):
    stmt = select(AfterSale)
    if store_ids is not None:
        stmt = stmt.where(AfterSale.store_id.in_(store_ids))
    return stmt


def admin_list(db: Session, params: dict[str, Any], store_ids: list[int] | None) -> dict[str, Any]:
    return ADMIN_LIST.page(db, admin_query(store_ids), params, serialize=case_row)


EXPORT_HEADERS = ["售后ID", "售后单号", "订单号", "门店", "类型", "原因", "状态", "金额(元)", "退款路径", "申请时间", "退款时间"]


def export_row(c: AfterSale) -> list[Any]:
    from luzback import cents_to_yuan
    return [c.id, c.after_sale_no, c.order_no, c.store_id, c.type, c.reason_code, c.status,
            cents_to_yuan(c.amount_cents), c.route, aware(c.created_at), aware(c.refunded_at)]
