"""购买规则：起售、单笔上限、每人限购（件数 / 次数，按周期）、新客专享、需绑定手机号。

规则跟着商品走（商品、SKU 或活动在后台维护，行种类解析时放进 ResolvedLine.purchase_rule），订单模块只负责校验与计数：

- 计数行 `order_purchase_quotas`：唯一 (subject, counter_key)。subject 是 `user:<会员>` 或 `phone:<手机号哈希>`；
  counter_key = `<规则键>|<all 或 store:<门店>>|<周期键>`。下单同事务条件自增（qty + n ≤ 件数上限、orders + 1 ≤ 次数上限），
  首行靠唯一约束裁决并发；取消 / 超时关单原数退回；退款按规则决定是否退回（release_on_refund）。
- 周期按订单模块时区计算：all 不分周期（活动期内用活动自己的规则键区分）、day、week（ISO 周）、month。
  订单行快照记下当时计入的计数键与主体，退回时回到原来的周期，跨天支付或关单也不会算错周期。
- 新客专享：下单前查“此前是否有已支付（或保留中的）订单”，并在同一事务里给主体的 `newcomer` 计数行加 1 单（上限 1），
  同一个人并发提交两单新客商品也只成一单。
- identity=member_phone 时同时按绑定手机号计数，一人多号也只能买到上限；未绑定手机号的不能购买。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from luzback import AppError, ErrorCode

from .models import Order, OrderItem, PurchaseQuota

WINDOWS = ("all", "day", "week", "month")
SCOPES = ("chain", "store")
IDENTITIES = ("member", "member_phone")
NEWCOMER_BASES = ("never_paid", "no_kept_order")
NEWCOMER_KEY = "newcomer|all|all"


@dataclass(frozen=True)
class PurchaseRule:
    """一个商品（或一组共享限额的商品）的购买规则；字段全空表示不限。"""

    key: str = ""                          # 计数键：默认 kind:product_id；同一活动多个商品共用限额时填活动键，如 campaign:12
    min_qty: int | None = None             # 起售：本单该规则下至少几件
    max_per_order: int | None = None       # 单笔上限：本单该规则下最多几件
    limit_qty: int | None = None           # 每人在统计周期内最多几件
    limit_orders: int | None = None        # 每人在统计周期内最多下几单（“只能购买一次”= 1）
    window: str = "all"                    # all、day、week、month
    scope: str = "chain"                   # chain 全部门店合计；store 按门店分别计
    identity: str = "member"               # member 按会员；member_phone 同时按绑定手机号计（防一人多号）
    release_on_refund: bool = True         # 退款后退回名额（行全部退完时退回次数）
    new_customer_only: bool = False        # 新客专享
    new_customer_basis: str = "never_paid"  # never_paid 从未支付过订单；no_kept_order 没有保留中的订单（全额退款的不算）
    requires_phone: bool = False           # 需绑定手机号才能购买

    @property
    def counts(self) -> bool:
        return bool(self.limit_qty or self.limit_orders)

    @property
    def needs_phone(self) -> bool:
        return self.requires_phone or self.identity == "member_phone"

    def view(self) -> dict[str, Any]:
        return {k: v for k, v in self.__dict__.items() if v not in (None, False, "") or k in ("window", "scope")}

    @classmethod
    def parse(cls, data: dict[str, Any] | None, *, default_key: str, legacy_limit: int | None = None) -> PurchaseRule | None:
        """从商品数据解析；兼容旧字段 limit_per_user（= 不分周期的每人件数上限）。数据不合法时报 BAD_REQUEST，不静默放行。"""
        data = dict(data or {})
        if legacy_limit and not data.get("limit_qty"):
            data["limit_qty"] = legacy_limit
        if not any(data.get(k) for k in ("min_qty", "max_per_order", "limit_qty", "limit_orders", "new_customer_only",
                                          "requires_phone")) and data.get("identity", "member") == "member":
            return None
        rule = cls(key=str(data.get("key") or default_key)[:96],
                   min_qty=_positive(data.get("min_qty")), max_per_order=_positive(data.get("max_per_order")),
                   limit_qty=_positive(data.get("limit_qty")), limit_orders=_positive(data.get("limit_orders")),
                   window=str(data.get("window") or "all"), scope=str(data.get("scope") or "chain"),
                   identity=str(data.get("identity") or "member"),
                   release_on_refund=bool(data.get("release_on_refund", True)),
                   new_customer_only=bool(data.get("new_customer_only", False)),
                   new_customer_basis=str(data.get("new_customer_basis") or "never_paid"),
                   requires_phone=bool(data.get("requires_phone", False)))
        bad = [name for name, value, allowed in (("window", rule.window, WINDOWS), ("scope", rule.scope, SCOPES),
                                                 ("identity", rule.identity, IDENTITIES),
                                                 ("new_customer_basis", rule.new_customer_basis, NEWCOMER_BASES))
               if value not in allowed]
        if rule.min_qty and rule.max_per_order and rule.min_qty > rule.max_per_order:
            bad.append("min_qty>max_per_order")
        if rule.min_qty and rule.limit_qty and rule.min_qty > rule.limit_qty:
            bad.append("min_qty>limit_qty")
        if bad:
            raise AppError(ErrorCode.BAD_REQUEST, "invalid purchase rule", data={"reason": "invalid_purchase_rule",
                                                                                "key": rule.key, "fields": bad})
        return rule


def _positive(value: Any) -> int | None:
    if value in (None, "", 0, "0"):
        return None
    number = int(value)
    if number < 1:
        raise AppError(ErrorCode.BAD_REQUEST, "purchase rule numbers must be positive",
                       data={"reason": "invalid_purchase_rule", "value": value})
    return number


def window_key(window: str, now: datetime, tz: str) -> tuple[str, datetime | None]:
    """(周期键, 周期结束时刻)；all 不分周期。"""
    if window == "all":
        return "all", None
    local = now.astimezone(ZoneInfo(tz))
    start = local.replace(hour=0, minute=0, second=0, microsecond=0)
    if window == "day":
        return "d" + start.strftime("%Y%m%d"), start + timedelta(days=1)
    if window == "week":
        year, week, weekday = start.isocalendar()
        monday = start - timedelta(days=weekday - 1)
        return f"w{year}{week:02d}", monday + timedelta(days=7)
    first = start.replace(day=1)
    following = (first + timedelta(days=32)).replace(day=1)
    return "m" + first.strftime("%Y%m"), following


@dataclass
class Group:
    """本单中共用一条规则（同一计数键）的行。"""

    rule: PurchaseRule
    counter_key: str
    window_end_at: datetime | None
    qty: int = 0
    product_ids: list[int] = field(default_factory=list)
    lines: list[Any] = field(default_factory=list, repr=False)


def group_lines(lines: list[Any], store_id: int | None, now: datetime, tz: str) -> dict[str, Group]:
    groups: dict[str, Group] = {}
    for ln in lines:
        rule = ln.rule
        if rule is None:
            continue
        scope = f"store:{store_id}" if rule.scope == "store" and store_id else "all"
        wkey, wend = window_key(rule.window, now, tz)
        counter_key = f"{rule.key}|{scope}|{wkey}"
        group = groups.get(counter_key)
        if group is None:
            group = groups[counter_key] = Group(rule=rule, counter_key=counter_key, window_end_at=wend)
        elif group.rule != rule:
            raise AppError(ErrorCode.BAD_REQUEST, "lines sharing a purchase rule key disagree",
                           data={"reason": "invalid_purchase_rule", "key": rule.key})
        group.qty += ln.qty
        group.product_ids.append(ln.product_id)
        group.lines.append(ln)
    return groups


def subjects(user_id: int, phone_hash: str | None, rule: PurchaseRule) -> list[str]:
    found = [f"user:{user_id}"]
    if rule.identity == "member_phone" and phone_hash:
        found.append(f"phone:{phone_hash}")
    return found


def usage(db: Session, keys: list[tuple[str, str]]) -> dict[tuple[str, str], tuple[int, int]]:
    """{(subject, counter_key): (qty, orders)}。"""
    if not keys:
        return {}
    subs = sorted({s for s, _ in keys})
    cks = sorted({k for _, k in keys})
    rows = db.execute(select(PurchaseQuota.subject, PurchaseQuota.counter_key, PurchaseQuota.qty, PurchaseQuota.orders)
                      .where(PurchaseQuota.subject.in_(subs), PurchaseQuota.counter_key.in_(cks))).all()
    return {(s, k): (q, o) for s, k, q, o in rows}


def _has_prior_order(db: Session, user_id: int, phone_hash: str | None, basis: str,
                     exclude_order_id: int | None = None) -> bool:
    who = [Order.user_id == user_id]
    if phone_hash:
        who.append(Order.buyer_phone_hash == phone_hash)
    query = select(Order.id).where(or_(*who), Order.paid_at.is_not(None))
    if basis == "no_kept_order":
        query = query.where(Order.refund_status != "refunded")
    if exclude_order_id:
        query = query.where(Order.id != exclude_order_id)
    return db.scalar(query.limit(1)) is not None


def check(db: Session, user_id: int, phone_hash: str | None, groups: dict[str, Group]) -> tuple[list[dict], dict[str, dict]]:
    """返回 (问题清单, 每个计数键的用量视图)。问题的 reason 是稳定代码，前端按它取文案键。"""
    issues: list[dict[str, Any]] = []
    views: dict[str, dict[str, Any]] = {}
    keys = [(s, g.counter_key) for g in groups.values() for s in subjects(user_id, phone_hash, g.rule)]
    used = usage(db, keys)
    newcomer_checked: dict[str, bool] = {}
    for ck, g in groups.items():
        rule = g.rule
        base = {"key": rule.key, "product_ids": g.product_ids, "window": rule.window}
        subs = subjects(user_id, phone_hash, rule)
        bought_qty = max((used.get((s, ck), (0, 0))[0] for s in subs), default=0)
        bought_orders = max((used.get((s, ck), (0, 0))[1] for s in subs), default=0)
        views[ck] = {**base, **rule.view(), "bought_qty": bought_qty, "bought_orders": bought_orders,
                     "remaining_qty": max(0, rule.limit_qty - bought_qty) if rule.limit_qty else None,
                     "remaining_orders": max(0, rule.limit_orders - bought_orders) if rule.limit_orders else None}
        if rule.needs_phone and not phone_hash:
            issues.append({**base, "reason": "phone_required"})
        if rule.min_qty and g.qty < rule.min_qty:
            issues.append({**base, "reason": "below_min_qty", "min_qty": rule.min_qty, "qty": g.qty})
        if rule.max_per_order and g.qty > rule.max_per_order:
            issues.append({**base, "reason": "over_order_limit", "max_per_order": rule.max_per_order, "qty": g.qty})
        if rule.limit_qty and bought_qty + g.qty > rule.limit_qty:
            issues.append({**base, "reason": "purchase_limit", "limit": rule.limit_qty, "bought": bought_qty,
                           "remaining": max(0, rule.limit_qty - bought_qty)})
        if rule.limit_orders and bought_orders + 1 > rule.limit_orders:
            issues.append({**base, "reason": "purchase_times_limit", "limit": rule.limit_orders,
                           "bought_orders": bought_orders})
        if rule.new_customer_only:
            basis = rule.new_customer_basis
            if basis not in newcomer_checked:
                newcomer_checked[basis] = _has_prior_order(db, user_id, phone_hash, basis)
            if newcomer_checked[basis]:
                issues.append({**base, "reason": "new_customer_only"})
    return issues, views


def _bump(db: Session, subject: str, counter_key: str, qty: int, orders: int, limit_qty: int | None,
          limit_orders: int | None, window_end_at: datetime | None, now: datetime) -> bool:
    """条件自增；首行靠唯一约束裁决并发。返回是否成功。"""
    for _ in range(2):
        cond = [PurchaseQuota.subject == subject, PurchaseQuota.counter_key == counter_key]
        if limit_qty:
            cond.append(PurchaseQuota.qty + qty <= limit_qty)
        if limit_orders:
            cond.append(PurchaseQuota.orders + orders <= limit_orders)
        changed = db.execute(update(PurchaseQuota).where(*cond).values(
            qty=PurchaseQuota.qty + qty, orders=PurchaseQuota.orders + orders, updated_at=now)).rowcount
        if changed == 1:
            return True
        exists = db.scalar(select(PurchaseQuota.id).where(PurchaseQuota.subject == subject,
                                                          PurchaseQuota.counter_key == counter_key))
        if exists:
            return False
        if (limit_qty and qty > limit_qty) or (limit_orders and orders > limit_orders):
            return False
        try:
            with db.begin_nested():
                db.add(PurchaseQuota(subject=subject, counter_key=counter_key, qty=qty, orders=orders,
                                     window_end_at=window_end_at, updated_at=now))
            return True
        except IntegrityError:
            continue
    return False


def consume(db: Session, user_id: int, phone_hash: str | None, groups: dict[str, Group], now: datetime) -> dict[str, dict]:
    """下单同事务计入名额；任何一项超限都抛 4225（调用方回滚整单）。返回每个计数键写进行快照的内容。"""
    issues, _ = check(db, user_id, phone_hash, groups)
    if issues:
        first = issues[0]
        raise AppError(ErrorCode.LIMIT_REACHED, "purchase rule not met", data={**first, "issues": issues})
    records: dict[str, dict] = {}
    newcomer_subjects: set[str] = set()
    for ck in sorted(groups):
        g = groups[ck]
        rule = g.rule
        subs = subjects(user_id, phone_hash, rule)
        if rule.counts:
            for subject in sorted(subs):
                if not _bump(db, subject, ck, g.qty, 1, rule.limit_qty, rule.limit_orders, g.window_end_at, now):
                    raise AppError(ErrorCode.LIMIT_REACHED, "purchase limit reached",
                                   data={"reason": "purchase_limit", "key": rule.key, "limit": rule.limit_qty,
                                         "limit_orders": rule.limit_orders})
            records[ck] = {"key": ck, "subjects": subs, "qty": g.qty, "orders": 1,
                           "refund_release": rule.release_on_refund}
        if rule.new_customer_only:
            newcomer_subjects.update(subs)
    if newcomer_subjects:
        for subject in sorted(newcomer_subjects):
            if not _bump(db, subject, NEWCOMER_KEY, 0, 1, None, 1, None, now):
                raise AppError(ErrorCode.LIMIT_REACHED, "new customer offer already used",
                               data={"reason": "new_customer_only"})
        records[NEWCOMER_KEY] = {"key": NEWCOMER_KEY, "subjects": sorted(newcomer_subjects), "qty": 0, "orders": 1,
                                 "refund_release": all(groups[ck].rule.release_on_refund for ck in groups
                                                       if groups[ck].rule.new_customer_only)}
    return records


def _decrement(db: Session, subject: str, counter_key: str, qty: int, orders: int) -> None:
    if qty:
        db.execute(update(PurchaseQuota).where(PurchaseQuota.subject == subject, PurchaseQuota.counter_key == counter_key,
                                               PurchaseQuota.qty >= qty).values(qty=PurchaseQuota.qty - qty))
    if orders:
        db.execute(update(PurchaseQuota).where(PurchaseQuota.subject == subject, PurchaseQuota.counter_key == counter_key,
                                               PurchaseQuota.orders >= orders).values(orders=PurchaseQuota.orders - orders))


def item_quotas(order: Order, item: OrderItem) -> list[dict[str, Any]]:
    """行快照里记下的计数；旧订单（只有 limit_per_user）按旧口径还原。"""
    snap = json.loads(item.snapshot_json or "{}")
    if "quotas" in snap:
        return snap["quotas"]
    if snap.get("limit_per_user"):
        return [{"key": f"{item.kind}:{item.product_id}|all|all", "subjects": [f"user:{order.user_id}"],
                 "qty": item.qty, "orders": 0, "refund_release": False}]
    return []


def release_all(db: Session, order: Order, items: list[OrderItem]) -> None:
    """取消 / 超时关单：原数退回。"""
    for item in items:
        for quota in item_quotas(order, item):
            for subject in quota["subjects"]:
                _decrement(db, subject, quota["key"], int(quota.get("qty") or 0), int(quota.get("orders") or 0))


def release_refunded(db: Session, order: Order, refunded: list[tuple[OrderItem, int]], all_items: list[OrderItem]) -> None:
    """退款成功：规则允许时按退款件数退回；同一计数键下的行全部退完时再退回次数（每单只退一次）。"""
    by_key: dict[str, list[tuple[OrderItem, dict[str, Any]]]] = {}
    for item in all_items:
        for quota in item_quotas(order, item):
            by_key.setdefault(quota["key"], []).append((item, quota))
    touched: set[str] = set()
    for item, qty in refunded:
        for quota in item_quotas(order, item):
            if not quota.get("refund_release"):
                continue
            touched.add(quota["key"])
            share = min(qty, int(quota.get("qty") or 0))
            if share:
                for subject in quota["subjects"]:
                    _decrement(db, subject, quota["key"], share, 0)
    for key in sorted(touched):
        pairs = by_key.get(key, [])
        if not pairs or not all(it.refunded_qty >= it.qty for it, _ in pairs):
            continue
        for _, quota in pairs:  # 次数只记在该键的第一行上
            if int(quota.get("orders") or 0):
                for subject in quota["subjects"]:
                    _decrement(db, subject, key, 0, int(quota["orders"]))


def purge(db: Session, now: datetime, keep_days: int = 7) -> int:
    """删除已结束超过 keep_days 的周期计数行（不分周期的不删）。"""
    from sqlalchemy import delete  # noqa: PLC0415

    return db.execute(delete(PurchaseQuota).where(PurchaseQuota.window_end_at.is_not(None),
                                                  PurchaseQuota.window_end_at < now - timedelta(days=keep_days))).rowcount
