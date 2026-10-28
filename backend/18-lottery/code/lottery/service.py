"""lottery 用例：活动、奖品、次数、抽奖、发奖、实物领奖、公示、统计、事件消费。

一个用例一个事务。带 request_key 的写用例内部走 run_idempotent（fn 内不 commit，由执行器提交）；
无键调用（事件消费、任务）自行 commit。所有计数 / 扣减 / 状态推进都是条件 UPDATE + rowcount。
"""
from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import String, case, cast, delete, false, func, insert, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from luzback import (
    AppError, ErrorCode, Field, ListSpec, StateMachine, enqueue_event, get_settings, mark_processed, run_idempotent,
    utcnow, write_audit,
)
from luzback.crypto import FieldCipher
from luzback.export import cents_to_yuan, export_csv
from luzback.security import mask_address, mask_name, mask_phone

from . import hooks, rules
from .config import ODDS_BASIS, get_config
from .models import (
    A_DRAFT, A_ENDED, A_PUBLISHED, A_RUNNING, ACTIVITY_KINDS, ACTIVITY_STATUSES, D_AWAITING, D_CLAIMED, D_DELIVERED,
    D_EXPIRED, D_FAILED, D_NONE, D_PENDING, D_SHIPPED, DELIVERY_STATUSES, FB_BUDGET, FB_MEMBER_LIMIT, FB_OUT_OF_STOCK,
    G_ADMIN, G_CHECKIN, G_PAYMENT, GRANT_ACTIVE, GRANT_REVOKED, GRANT_SOURCES, OOS_EXCLUDE, P_PHYSICAL, P_THANKS,
    PRIZE_KINDS, SRC_EARNED, SRC_FREE, VIRTUAL_KINDS, LotteryActivity, LotteryAmendment, LotteryChanceAccount,
    LotteryChanceGrant, LotteryChancePeriod, LotteryDraw, LotteryMemberPrizeWin, LotteryPrize,
)

CONSUMER = "lottery"

ACTIVITY_SM = StateMachine("lottery_activity", {
    A_DRAFT: {A_PUBLISHED},
    A_PUBLISHED: {A_RUNNING, A_ENDED},
    A_RUNNING: {A_ENDED},
}, terminal=frozenset({A_ENDED}))

DELIVERY_SM = StateMachine("lottery_delivery", {
    D_PENDING: {D_DELIVERED, D_FAILED},
    D_FAILED: {D_PENDING, D_DELIVERED},
    D_AWAITING: {D_CLAIMED, D_EXPIRED},
    D_CLAIMED: {D_CLAIMED, D_SHIPPED},
    D_SHIPPED: {D_DELIVERED},
}, terminal=frozenset({D_DELIVERED, D_EXPIRED, D_NONE}))

ACTIVITY_FIELDS = (
    "store_id", "name", "kind", "start_at", "end_at", "rules_text", "audience", "free_daily_chances", "limit_daily",
    "limit_monthly", "limit_total", "earn_payment_min_cents", "earn_payment_chances", "earn_checkin_chances",
    "out_of_stock_policy", "budget_cents", "claim_days", "early_end_allowed", "publicity_threshold_cents",
)
PRIZE_FIELDS = ("seq", "name", "kind", "ref_id", "quantity", "value_cents", "weight_bp", "stock_total",
                "per_member_limit", "image_url", "description")
PHONE = re.compile(r"^1\d{10}$")


@dataclass(frozen=True)
class StaffActor:
    """后台操作人：id、门店范围（None = 全部门店）与请求上下文（写审计）。"""

    id: int
    store_scope: list[int] | None = None
    ip: str | None = None
    user_agent: str | None = None
    trace_id: str | None = None


# ================================================================ 基础助手

def _now(now: datetime | None) -> datetime:
    return rules.aware(now) if now is not None else utcnow()


def _fp(payload: Any) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str, ensure_ascii=False).encode()).hexdigest()


def _idem(db: Session, scope: str, key: str | None, actor: str, payload: Any, fn: Callable[[], Any],
          now: datetime | None = None) -> Any:
    return run_idempotent(db, scope, key, fn, actor=actor, fingerprint=_fp(payload), require_key=True, now=now)


def _cipher() -> FieldCipher:
    return FieldCipher(get_settings().field_encryption_key)


def _audit(db: Session, actor: StaffActor, action: str, resource_id: Any, before: Any = None, after: Any = None,
           reason: str | None = None, resource: str = "lottery_activity") -> None:
    write_audit(db, "staff", actor.id, action, resource, resource_id, before=before, after=after, reason=reason,
                ip=actor.ip, user_agent=actor.user_agent, trace_id=actor.trace_id)


def _assert_scope(actor: StaffActor | None, store_id: int | None) -> None:
    if actor is None or actor.store_scope is None:
        return
    if not store_id or int(store_id) not in actor.store_scope:
        raise AppError(ErrorCode.SCOPE_FORBIDDEN, "store out of scope", data={"store_id": store_id})


def _scoped(stmt, actor: StaffActor | None, column):
    if actor is None or actor.store_scope is None:
        return stmt
    if not actor.store_scope:
        return stmt.where(false())
    return stmt.where(column.in_(actor.store_scope))


def insert_ignore(db: Session, model: Any, values: dict[str, Any]) -> bool:
    """INSERT … 冲突忽略（唯一约束裁决并发）。返回是否插入了新行。"""
    dialect = db.get_bind().dialect.name
    if dialect == "sqlite":
        from sqlalchemy.dialects.sqlite import insert as dialect_insert
        stmt = dialect_insert(model).values(**values).on_conflict_do_nothing()
    elif dialect == "postgresql":  # pragma: no cover - 需要 Postgres
        from sqlalchemy.dialects.postgresql import insert as dialect_insert
        stmt = dialect_insert(model).values(**values).on_conflict_do_nothing()
    elif dialect == "mysql":  # pragma: no cover - 需要 MySQL
        stmt = insert(model).values(**values).prefix_with("IGNORE")
    else:  # pragma: no cover
        try:
            with db.begin_nested():
                db.execute(insert(model).values(**values))
            return True
        except IntegrityError:
            return False
    return db.execute(stmt).rowcount == 1


def _load_activity(db: Session, activity_id: int, actor: StaffActor | None = None) -> LotteryActivity:
    act = db.get(LotteryActivity, activity_id, populate_existing=True)
    if act is None or act.deleted_at is not None:
        raise AppError(ErrorCode.NOT_FOUND, "activity not found")
    if actor is not None:
        _assert_scope(actor, act.store_id)
    return act


def _fresh_activity(db: Session, activity_id: int) -> LotteryActivity:
    act = _load_activity(db, activity_id)
    db.refresh(act)
    return act


def _activity_dict(act: LotteryActivity) -> dict[str, Any]:
    return {k: getattr(act, k) for k in ACTIVITY_FIELDS}


def _prizes(db: Session, activity_id: int) -> list[LotteryPrize]:
    return list(db.execute(select(LotteryPrize).where(LotteryPrize.activity_id == activity_id)
                           .order_by(LotteryPrize.seq)).scalars().all())


def _prize_view(p: LotteryPrize, public: bool = False) -> dict[str, Any]:
    out = {
        "id": p.id, "seq": p.seq, "name": p.name, "kind": p.kind, "quantity": p.quantity, "value_cents": p.value_cents,
        "weight_bp": p.weight_bp, "probability": rules.percent(p.weight_bp), "stock_total": p.stock_total,
        "stock_left": p.stock_left, "per_member_limit": p.per_member_limit, "image_url": p.image_url,
        "description": p.description,
    }
    if not public:
        out.update({"ref_id": p.ref_id, "win_count": p.win_count})
    return out


def activity_view(db: Session, act: LotteryActivity, public: bool = False) -> dict[str, Any]:
    out: dict[str, Any] = {
        "id": act.id, "store_id": act.store_id, "name": act.name, "kind": act.kind, "status": act.status,
        "start_at": rules.iso(act.start_at), "end_at": rules.iso(act.end_at), "rules_text": act.rules_text,
        "audience": act.audience, "free_daily_chances": act.free_daily_chances, "limit_daily": act.limit_daily,
        "limit_monthly": act.limit_monthly, "limit_total": act.limit_total,
        "earn_payment_min_cents": act.earn_payment_min_cents, "earn_payment_chances": act.earn_payment_chances,
        "earn_checkin_chances": act.earn_checkin_chances, "out_of_stock_policy": act.out_of_stock_policy,
        "claim_days": act.claim_days, "early_end_allowed": act.early_end_allowed,
        "publicity_threshold_cents": act.publicity_threshold_cents, "rules_hash": act.rules_hash,
        "published_at": rules.iso(act.published_at), "ended_at": rules.iso(act.ended_at),
        "prizes": [_prize_view(p, public) for p in _prizes(db, act.id)],
        "amendments": [
            {"field": a.field, "before": a.before, "after": a.after, "at": rules.iso(a.created_at)}
            for a in db.execute(select(LotteryAmendment).where(LotteryAmendment.activity_id == act.id)
                                .order_by(LotteryAmendment.id)).scalars().all()
        ],
    }
    if public:
        out["disclosure"] = disclosure(act)
    else:
        out.update({"budget_cents": act.budget_cents, "spent_cents": act.spent_cents,
                    "reserved_cents": act.reserved_cents, "unlimited_spent_cents": act.unlimited_spent_cents,
                    "version": act.version,
                    "end_reason": act.end_reason, "created_at": rules.iso(act.created_at)})
    return out


def disclosure(act: LotteryActivity) -> dict[str, str]:
    """开奖前须公示的信息（第十三条第一款）：奖项种类、参与条件、开奖方式、奖品数量或中奖概率、开奖时间。"""
    conditions = []
    if act.free_daily_chances:
        conditions.append(f"每天免费 {act.free_daily_chances} 次")
    if act.earn_payment_min_cents:
        conditions.append(f"单笔实付满 {cents_to_yuan(act.earn_payment_min_cents)} 元获得 {act.earn_payment_chances} 次")
    if act.earn_checkin_chances:
        conditions.append(f"每次签到获得 {act.earn_checkin_chances} 次")
    limits = []
    for label, value in (("每日", act.limit_daily), ("每月", act.limit_monthly), ("活动期间", act.limit_total)):
        if value:
            limits.append(f"{label}最多 {value} 次")
    oos = ("抽中库存已发完的奖品时按“谢谢参与”处理，不重抽" if act.out_of_stock_policy != OOS_EXCLUDE
           else "奖品发完后移出奖池，其余奖项按公示权重重新计算概率")
    return {
        "method": "服务端密码学安全随机数即时开奖；每次抽奖记录随机值与奖池快照，可复现核验",
        "draw_time": f"{rules.iso(act.start_at)} 至 {rules.iso(act.end_at)}，抽奖即时开奖",
        "conditions": "；".join(conditions) or "按活动规则获得次数",
        "limits": "；".join(limits) or "不限",
        "out_of_stock": oos,
        "publicity": f"价值超过 {cents_to_yuan(act.publicity_threshold_cents)} 元的奖项兑奖情况实时公示",
    }


# ================================================================ 活动（后台）

def _validated_fields(data: Mapping[str, Any], base: Mapping[str, Any] | None = None) -> dict[str, Any]:
    cfg = get_config()
    merged = dict(base or {})
    merged.update({k: v for k, v in data.items() if k in ACTIVITY_FIELDS})
    merged.setdefault("kind", "wheel")
    merged.setdefault("out_of_stock_policy", "thanks")
    merged.setdefault("claim_days", cfg.default_claim_days)
    merged.setdefault("publicity_threshold_cents", cfg.publicity_threshold_cents)
    merged.setdefault("audience", "all")
    rules.validate_activity_fields(merged)
    if not 0 < int(merged["publicity_threshold_cents"]) <= cfg.publicity_threshold_cents:
        raise AppError(ErrorCode.BAD_REQUEST, "publicity_threshold_cents must be within (0, 500 yuan]")
    for k in ("start_at", "end_at"):
        merged[k] = rules.aware(merged[k])
    return merged


def create_activity(db: Session, actor: StaffActor, data: Mapping[str, Any], request_key: str | None,
                    prizes: list[Mapping[str, Any]] | None = None) -> dict:
    def fn() -> dict:
        fields = _validated_fields(data)
        _assert_scope(actor, fields.get("store_id"))
        act = LotteryActivity(**fields, status=A_DRAFT, created_by=actor.id, updated_by=actor.id)
        db.add(act)
        db.flush()
        if prizes:
            _replace_prizes(db, act.id, prizes)
        _audit(db, actor, "lottery.activity.create", act.id, after=_jsonable(fields))
        return activity_view(db, act)

    return _idem(db, "lottery.activity.create", request_key, f"staff:{actor.id}",
                 {"data": _jsonable(dict(data)), "prizes": prizes}, fn)


def _replace_prizes(db: Session, activity_id: int, prizes: list[Mapping[str, Any]]) -> None:
    cfg = get_config()
    if len(prizes) > cfg.max_prizes:
        raise AppError(ErrorCode.BAD_REQUEST, f"at most {cfg.max_prizes} prizes")
    seqs = [int(p["seq"]) for p in prizes]
    if len(set(seqs)) != len(seqs):
        raise AppError(ErrorCode.BAD_REQUEST, "prize seq must be unique")
    for p in prizes:
        rules.validate_prize(p, cfg.prize_value_cap_cents)
    db.execute(delete(LotteryPrize).where(LotteryPrize.activity_id == activity_id))
    for p in prizes:
        values = {k: p.get(k) for k in PRIZE_FIELDS}
        values["quantity"] = int(values.get("quantity") or (0 if p["kind"] == P_THANKS else 1))
        values["value_cents"] = int(values.get("value_cents") or 0)
        values["per_member_limit"] = int(values.get("per_member_limit") or 0)
        values["description"] = values.get("description") or ""
        db.add(LotteryPrize(activity_id=activity_id, stock_left=values["stock_total"], win_count=0, **values))
    db.flush()


def set_prizes(db: Session, actor: StaffActor, activity_id: int, prizes: list[Mapping[str, Any]],
               request_key: str | None) -> dict:
    """整体替换奖池；只有草稿可以。发布后奖项、概率、价值冻结（只能 add_prize_stock）。"""
    def fn() -> dict:
        act = _load_activity(db, activity_id, actor)
        if act.status != A_DRAFT:
            raise AppError(ErrorCode.CONFLICT, "published prizes and odds are frozen", data={"status": act.status})
        before = [_prize_view(p) for p in _prizes(db, activity_id)]
        locked = db.execute(update(LotteryActivity).where(
            LotteryActivity.id == activity_id, LotteryActivity.status == A_DRAFT, LotteryActivity.version == act.version,
        ).values(version=LotteryActivity.version + 1, updated_by=actor.id)).rowcount
        if locked != 1:
            raise AppError(ErrorCode.CONFLICT, "activity changed concurrently, refresh and retry")
        _replace_prizes(db, activity_id, prizes)
        _audit(db, actor, "lottery.prizes.replace", activity_id, before=before, after=list(prizes))
        return activity_view(db, _fresh_activity(db, activity_id))

    return _idem(db, f"lottery.prizes:{activity_id}", request_key, f"staff:{actor.id}", {"prizes": prizes}, fn)


def update_activity(db: Session, actor: StaffActor, activity_id: int, changes: Mapping[str, Any],
                    request_key: str | None, expected_version: int | None = None, now: datetime | None = None) -> dict:
    """草稿：任意修改。已发布 / 进行中：只允许 rules.AMENDABLE 字段朝有利于消费者的方向改，并公示变更。结束：4090。"""
    now = _now(now)

    def fn() -> dict:
        act = _load_activity(db, activity_id, actor)
        if expected_version is not None and act.version != expected_version:
            raise AppError(ErrorCode.CONFLICT, "activity changed, refresh and retry", data={"version": act.version})
        if act.status == A_ENDED:
            raise AppError(ErrorCode.CONFLICT, "ended activity cannot be changed")
        old = _activity_dict(act)
        incoming = {k: (rules.aware(v) if k in ("start_at", "end_at") else v) for k, v in changes.items()
                    if k in ACTIVITY_FIELDS}
        diff = {k: v for k, v in incoming.items() if _cmp(old.get(k)) != _cmp(v)}
        if not diff:
            return activity_view(db, act)
        if "store_id" in diff:
            _assert_scope(actor, diff["store_id"])
        if act.status != A_DRAFT:
            for k, v in diff.items():
                if k not in rules.AMENDABLE or not rules.is_favourable(k, old[k], v):
                    raise AppError(ErrorCode.CONFLICT, "published rules are frozen; only changes in consumers' favour "
                                   "(more stock, later end, more chances, looser limits) are allowed", data={"field": k})
            if "end_at" in diff and rules.aware(diff["end_at"]) <= now:
                raise AppError(ErrorCode.BAD_REQUEST, "end_at must be in the future")
        merged = _validated_fields(diff, old)
        values = {k: merged[k] for k in diff}
        if act.status != A_DRAFT:
            odds = json.loads(act.odds_json or "[]")
            values["rules_hash"] = rules.rules_hash(merged, odds)
        ok = db.execute(update(LotteryActivity).where(
            LotteryActivity.id == activity_id, LotteryActivity.version == act.version,
            LotteryActivity.status == act.status,
        ).values(**values, version=LotteryActivity.version + 1, updated_by=actor.id)).rowcount
        if ok != 1:
            raise AppError(ErrorCode.CONFLICT, "activity changed concurrently, refresh and retry")
        if act.status != A_DRAFT:
            for k, v in diff.items():
                db.add(LotteryAmendment(activity_id=activity_id, field=k, before=str(_cmp(old[k])), after=str(_cmp(v)),
                                        staff_id=actor.id, created_at=now))
            db.flush()
        _audit(db, actor, "lottery.activity.update", activity_id, before=_jsonable({k: old[k] for k in diff}),
               after=_jsonable(diff))
        return activity_view(db, _fresh_activity(db, activity_id))

    return _idem(db, f"lottery.activity.update:{activity_id}", request_key, f"staff:{actor.id}",
                 {"changes": _jsonable(dict(changes)), "v": expected_version}, fn, now)


def _reserve_budget(db: Session, activity_id: int, extra_cents: int) -> None:
    """追加库存前预留预算：预留 + 不限量已用 + 新增 ≤ 预算（条件 UPDATE）。"""
    A = LotteryActivity
    ok = db.execute(update(A).where(A.id == activity_id,
                                    A.reserved_cents + A.unlimited_spent_cents + extra_cents <= A.budget_cents)
                    .values(reserved_cents=A.reserved_cents + extra_cents)).rowcount
    if ok != 1:
        raise AppError(ErrorCode.BUSINESS_RULE, "prize value × stock would exceed the budget; raise budget_cents first",
                       data={"extra_cents": extra_cents})


def add_prize_stock(db: Session, actor: StaffActor, activity_id: int, prize_id: int, add: int,
                    request_key: str | None, now: datetime | None = None) -> dict:
    """发布后唯一允许的奖池变更：给有限库存的奖品追加数量（有利于消费者）。"""
    now = _now(now)
    if not 1 <= add <= 1_000_000:
        raise AppError(ErrorCode.BAD_REQUEST, "add must be within 1..1000000")

    def fn() -> dict:
        act = _load_activity(db, activity_id, actor)
        if act.status not in (A_PUBLISHED, A_RUNNING):
            raise AppError(ErrorCode.CONFLICT, "stock can be added only to a published or running activity "
                           "(edit the draft prize list instead)", data={"status": act.status})
        prize = db.get(LotteryPrize, prize_id, populate_existing=True)
        if prize is None or prize.activity_id != activity_id:
            raise AppError(ErrorCode.NOT_FOUND, "prize not found")
        if prize.kind == P_THANKS or prize.stock_total is None:
            raise AppError(ErrorCode.BUSINESS_RULE, "only limited real prizes have stock")
        _reserve_budget(db, activity_id, prize.value_cents * add)
        before = prize.stock_total
        ok = db.execute(update(LotteryPrize).where(LotteryPrize.id == prize_id, LotteryPrize.stock_total.is_not(None))
                        .values(stock_total=LotteryPrize.stock_total + add, stock_left=LotteryPrize.stock_left + add)
                        ).rowcount
        if ok != 1:
            raise AppError(ErrorCode.CONFLICT, "prize changed concurrently")
        odds = json.loads(act.odds_json or "[]")
        for o in odds:
            if o["prize_id"] == prize_id:
                o["stock_total"] = before + add
        merged = _activity_dict(act)
        db.execute(update(LotteryActivity).where(LotteryActivity.id == activity_id).values(
            odds_json=json.dumps(odds, ensure_ascii=False), rules_hash=rules.rules_hash(merged, odds),
            version=LotteryActivity.version + 1, updated_by=actor.id))
        db.add(LotteryAmendment(activity_id=activity_id, field=f"prize:{prize.seq}:stock_total", before=str(before),
                                after=str(before + add), staff_id=actor.id, created_at=now))
        db.flush()
        _audit(db, actor, "lottery.prize.add_stock", activity_id, before={"prize_id": prize_id, "stock_total": before},
               after={"prize_id": prize_id, "stock_total": before + add})
        return activity_view(db, _fresh_activity(db, activity_id))

    return _idem(db, f"lottery.prize.stock:{prize_id}", request_key, f"staff:{actor.id}", {"add": add}, fn, now)


def publish(db: Session, actor: StaffActor, activity_id: int, request_key: str | None,
            now: datetime | None = None) -> dict:
    """draft → published：校验奖池（万分比合计 10000、含谢谢参与、单奖 ≤ 5 万、预算）后冻结快照与规则哈希。"""
    now = _now(now)
    cfg = get_config()

    def fn() -> dict:
        act = _load_activity(db, activity_id, actor)
        ACTIVITY_SM.check(act.status, A_PUBLISHED)
        fields = _validated_fields({}, _activity_dict(act))
        if not (act.rules_text or "").strip():
            raise AppError(ErrorCode.BUSINESS_RULE, "rules_text is required before publishing (公示规则)")
        if rules.aware(act.end_at) <= now:
            raise AppError(ErrorCode.BUSINESS_RULE, "end_at must be in the future")
        prizes = _prizes(db, activity_id)
        rules.validate_pool([_prize_view(p) | {"ref_id": p.ref_id} for p in prizes], cfg.prize_value_cap_cents,
                            act.budget_cents, cfg.max_prizes)
        db.execute(update(LotteryPrize).where(LotteryPrize.activity_id == activity_id)
                   .values(stock_left=LotteryPrize.stock_total, win_count=0))
        odds = rules.odds_snapshot(prizes)
        ACTIVITY_SM.advance(db, LotteryActivity, activity_id, A_DRAFT, A_PUBLISHED,
                            extra_where=(LotteryActivity.version == act.version,),
                            odds_json=json.dumps(odds, ensure_ascii=False), rules_hash=rules.rules_hash(fields, odds),
                            published_at=now, spent_cents=0, unlimited_spent_cents=0,
                            reserved_cents=sum(int(p.value_cents) * int(p.stock_total) for p in prizes
                                               if p.kind != P_THANKS and p.stock_total is not None),
                            version=LotteryActivity.version + 1, updated_by=actor.id)
        enqueue_event(db, "lottery.activity_published", {"activity_id": activity_id, "store_id": act.store_id},
                      "lottery_activity", activity_id)
        _audit(db, actor, "lottery.activity.publish", activity_id, after={"odds": odds})
        return activity_view(db, _fresh_activity(db, activity_id))

    return _idem(db, f"lottery.activity.publish:{activity_id}", request_key, f"staff:{actor.id}", {}, fn, now)


def end_activity(db: Session, actor: StaffActor, activity_id: int, reason: str, request_key: str | None,
                 now: datetime | None = None) -> dict:
    """提前结束：未开始的已发布活动可以撤下；进行中的活动只有公示过“可提前结束”时才允许（否则属于变更公示规则）。"""
    now = _now(now)
    if not (reason or "").strip():
        raise AppError(ErrorCode.BAD_REQUEST, "reason is required")

    def fn() -> dict:
        act = _load_activity(db, activity_id, actor)
        ACTIVITY_SM.check(act.status, A_ENDED)
        started = act.status == A_RUNNING or rules.aware(act.start_at) <= now
        if started and rules.aware(act.end_at) > now and not act.early_end_allowed:
            raise AppError(ErrorCode.BUSINESS_RULE, "the published rules do not allow ending early",
                           data={"end_at": rules.iso(act.end_at)})
        _end(db, act, now, reason[:200])
        _audit(db, actor, "lottery.activity.end", activity_id, before={"status": act.status},
               after={"status": A_ENDED}, reason=reason)
        return activity_view(db, _fresh_activity(db, activity_id))

    return _idem(db, f"lottery.activity.end:{activity_id}", request_key, f"staff:{actor.id}", {"reason": reason}, fn, now)


def _end(db: Session, act: LotteryActivity, now: datetime, reason: str) -> bool:
    ok = db.execute(update(LotteryActivity).where(
        LotteryActivity.id == act.id, LotteryActivity.status.in_((A_PUBLISHED, A_RUNNING)),
    ).values(status=A_ENDED, ended_at=now, end_reason=reason, version=LotteryActivity.version + 1)).rowcount
    if ok == 1:
        enqueue_event(db, "lottery.activity_ended", {"activity_id": act.id, "reason": reason}, "lottery_activity", act.id)
    return ok == 1


def delete_activity(db: Session, actor: StaffActor, activity_id: int, request_key: str | None,
                    now: datetime | None = None) -> dict:
    now = _now(now)

    def fn() -> dict:
        act = _load_activity(db, activity_id, actor)
        ok = db.execute(update(LotteryActivity).where(LotteryActivity.id == activity_id,
                                                      LotteryActivity.status == A_DRAFT,
                                                      LotteryActivity.deleted_at.is_(None))
                        .values(deleted_at=now, updated_by=actor.id)).rowcount
        if ok != 1:
            raise AppError(ErrorCode.CONFLICT, "only draft activities can be deleted", data={"status": act.status})
        _audit(db, actor, "lottery.activity.delete", activity_id, before={"status": act.status})
        return {"id": activity_id, "deleted": True}

    return _idem(db, f"lottery.activity.delete:{activity_id}", request_key, f"staff:{actor.id}", {}, fn, now)


def get_activity_admin(db: Session, actor: StaffActor, activity_id: int) -> dict:
    return activity_view(db, _load_activity(db, activity_id, actor))


ACTIVITY_LIST = ListSpec(
    fields={
        "status": Field(LotteryActivity.status, ops=("eq", "in"), choices=frozenset(ACTIVITY_STATUSES)),
        "kind": Field(LotteryActivity.kind, ops=("eq", "in"), choices=frozenset(ACTIVITY_KINDS)),
        "store_id": Field(LotteryActivity.store_id, "int", ops=("eq", "in", "null")),
        "start_at": Field(LotteryActivity.start_at, "datetime", ops=("range",), sortable=True),
        "end_at": Field(LotteryActivity.end_at, "datetime", ops=("range",), sortable=True),
        "created_at": Field(LotteryActivity.created_at, "datetime", ops=("range",), sortable=True),
    },
    id_column=LotteryActivity.id,
    keyword_columns=(LotteryActivity.name,),
)


def list_activities(db: Session, actor: StaffActor, params: Mapping[str, Any]) -> dict:
    stmt = _scoped(select(LotteryActivity).where(LotteryActivity.deleted_at.is_(None)), actor, LotteryActivity.store_id)
    return ACTIVITY_LIST.page(db, stmt, params, serialize=lambda a: {
        "id": a.id, "store_id": a.store_id, "name": a.name, "kind": a.kind, "status": a.status,
        "start_at": rules.iso(a.start_at), "end_at": rules.iso(a.end_at), "budget_cents": a.budget_cents,
        "spent_cents": a.spent_cents, "created_at": rules.iso(a.created_at)})


def simulate(db: Session, actor: StaffActor, activity_id: int, n: int, seed: int | None = None) -> dict:
    """离线模拟（不写库、不扣库存）：检验当前奖池的分布与成本期望。"""
    cfg = get_config()
    if not 1 <= n <= cfg.max_simulate:
        raise AppError(ErrorCode.BAD_REQUEST, f"n must be within 1..{cfg.max_simulate}")
    act = _load_activity(db, activity_id, actor)
    odds = json.loads(act.odds_json) if act.odds_json else rules.odds_snapshot(_prizes(db, activity_id))
    if rules.pool_range(odds) != ODDS_BASIS:
        raise AppError(ErrorCode.BUSINESS_RULE, "weights must sum to 10000 before simulating")
    rng = hooks.SeededRandom(seed) if seed is not None else hooks.SecretsRandom()
    counts = rules.simulate(odds, n, rng)
    rows = []
    for o in odds:
        c = counts[int(o["prize_id"])]
        rows.append({"prize_id": o["prize_id"], "seq": o["seq"], "name": o["name"], "weight_bp": o["weight_bp"],
                     "expected": round(n * o["weight_bp"] / ODDS_BASIS, 2), "observed": c,
                     "observed_bp": round(c * ODDS_BASIS / n, 2), "cost_cents": c * int(o["value_cents"] or 0)})
    return {"n": n, "rows": rows, "expected_cost_per_draw_cents":
            round(sum(int(o["value_cents"] or 0) * o["weight_bp"] for o in odds) / ODDS_BASIS, 2)}


# ================================================================ 顾客端：活动与次数

def _public_activity(db: Session, activity_id: int) -> LotteryActivity:
    act = db.get(LotteryActivity, activity_id, populate_existing=True)
    if act is None or act.deleted_at is not None or act.status == A_DRAFT:
        raise AppError(ErrorCode.NOT_FOUND, "activity not found")
    return act


def activity_detail(db: Session, activity_id: int) -> dict:
    return activity_view(db, _public_activity(db, activity_id), public=True)


OPEN_LIST = ListSpec(
    fields={
        "kind": Field(LotteryActivity.kind, ops=("eq",), choices=frozenset(ACTIVITY_KINDS)),
        "start_at": Field(LotteryActivity.start_at, "datetime", sortable=True),
        "end_at": Field(LotteryActivity.end_at, "datetime", sortable=True),
        "created_at": Field(LotteryActivity.created_at, "datetime", sortable=True),
    },
    id_column=LotteryActivity.id,
    default_sort="end_at",
    extra_params=frozenset({"store_id"}),
)


def list_open_activities(db: Session, params: Mapping[str, Any], store_id: int | None, secret: str,
                         now: datetime | None = None) -> dict:
    now = _now(now)
    stmt = select(LotteryActivity).where(LotteryActivity.deleted_at.is_(None),
                                         LotteryActivity.status.in_((A_PUBLISHED, A_RUNNING)),
                                         LotteryActivity.end_at > now)
    if store_id is not None:
        stmt = stmt.where(or_(LotteryActivity.store_id.is_(None), LotteryActivity.store_id == store_id))
    return OPEN_LIST.cursor(db, stmt, params, secret, serialize=lambda a: {
        "id": a.id, "name": a.name, "kind": a.kind, "status": a.status, "store_id": a.store_id,
        "start_at": rules.iso(a.start_at), "end_at": rules.iso(a.end_at)})


def _ensure_counters(db: Session, activity_id: int, member_id: int, day: str, month: str) -> None:
    insert_ignore(db, LotteryChanceAccount, {"activity_id": activity_id, "member_id": member_id, "granted": 0, "used": 0,
                                             "draws": 0})
    for period in (day, month):
        insert_ignore(db, LotteryChancePeriod, {"activity_id": activity_id, "member_id": member_id, "period": period,
                                                "draws": 0, "free_used": 0})


def my_chances(db: Session, activity_id: int, member_id: int, now: datetime | None = None) -> dict:
    now = _now(now)
    cfg = get_config()
    act = _public_activity(db, activity_id)
    day, month = rules.day_key(now, cfg.timezone), rules.month_key(now, cfg.timezone)
    acc = db.execute(select(LotteryChanceAccount).where(LotteryChanceAccount.activity_id == activity_id,
                                                        LotteryChanceAccount.member_id == member_id)).scalar_one_or_none()
    periods = {p.period: p for p in db.execute(select(LotteryChancePeriod).where(
        LotteryChancePeriod.activity_id == activity_id, LotteryChancePeriod.member_id == member_id,
        LotteryChancePeriod.period.in_((day, month)))).scalars().all()}
    d, m = periods.get(day), periods.get(month)
    free_left = max(0, act.free_daily_chances - (d.free_used if d else 0))
    earned_left = max(0, (acc.granted - acc.used) if acc else 0)

    def left(limit: int, used: int) -> int | None:
        return None if limit == 0 else max(0, limit - used)

    daily_left = left(act.limit_daily, d.draws if d else 0)
    monthly_left = left(act.limit_monthly, m.draws if m else 0)
    total_left = left(act.limit_total, acc.draws if acc else 0)
    available = free_left + earned_left
    for cap in (daily_left, monthly_left, total_left):
        if cap is not None:
            available = min(available, cap)
    open_now = act.status in (A_PUBLISHED, A_RUNNING) and rules.aware(act.start_at) <= now < rules.aware(act.end_at)
    return {"activity_id": activity_id, "free_left_today": free_left, "earned_left": earned_left,
            "daily_left": daily_left, "monthly_left": monthly_left, "total_left": total_left,
            "available": available if open_now else 0, "open": open_now}


# ================================================================ 次数发放

def _grant(db: Session, act: LotteryActivity, member_id: int, chances: int, source: str, source_ref: str,
           operator_id: int | None = None, reason: str | None = None) -> tuple[LotteryChanceGrant, bool]:
    if source not in GRANT_SOURCES:
        raise AppError(ErrorCode.BAD_REQUEST, "unknown grant source")
    if chances < 1:
        raise AppError(ErrorCode.BAD_REQUEST, "chances must be >= 1")
    if act.status not in (A_PUBLISHED, A_RUNNING):
        raise AppError(ErrorCode.CONFLICT, "chances can be granted only for a published or running activity")
    created = insert_ignore(db, LotteryChanceGrant, {
        "activity_id": act.id, "member_id": member_id, "source": source, "source_ref": source_ref[:128],
        "chances": chances, "status": GRANT_ACTIVE, "operator_id": operator_id, "reason": reason,
        "created_at": utcnow(), "updated_at": utcnow()})
    grant = db.execute(select(LotteryChanceGrant).where(
        LotteryChanceGrant.activity_id == act.id, LotteryChanceGrant.source == source,
        LotteryChanceGrant.source_ref == source_ref[:128])).scalar_one()
    if created:
        insert_ignore(db, LotteryChanceAccount, {"activity_id": act.id, "member_id": member_id, "granted": 0, "used": 0,
                                                 "draws": 0, "created_at": utcnow(), "updated_at": utcnow()})
        db.execute(update(LotteryChanceAccount).where(LotteryChanceAccount.activity_id == act.id,
                                                      LotteryChanceAccount.member_id == member_id)
                   .values(granted=LotteryChanceAccount.granted + chances))
        enqueue_event(db, "lottery.chances_granted", {"activity_id": act.id, "member_id": member_id, "chances": chances,
                                                      "source": source}, "lottery_activity", act.id)
    return grant, created


def _grant_view(g: LotteryChanceGrant, created: bool | None = None) -> dict:
    out = {"id": g.id, "activity_id": g.activity_id, "member_id": g.member_id, "source": g.source,
           "source_ref": g.source_ref, "chances": g.chances, "status": g.status, "operator_id": g.operator_id,
           "reason": g.reason, "created_at": rules.iso(g.created_at)}
    if created is not None:
        out["created"] = created
    return out


def grant_chances(db: Session, activity_id: int, member_id: int, chances: int, source: str, source_ref: str,
                  operator_id: int | None = None, reason: str | None = None) -> dict:
    """对外：按业务事件发放次数（幂等：同 source + source_ref 只发一次）。自行 commit。"""
    act = _load_activity(db, activity_id)
    try:
        grant, created = _grant(db, act, member_id, chances, source, source_ref, operator_id, reason)
        db.commit()
    except Exception:
        db.rollback()
        raise
    return _grant_view(grant, created)


def admin_grant(db: Session, actor: StaffActor, activity_id: int, member_id: int, chances: int, reason: str,
                request_key: str | None) -> dict:
    cfg = get_config()
    if not 1 <= chances <= cfg.max_admin_grant:
        raise AppError(ErrorCode.BAD_REQUEST, f"chances must be within 1..{cfg.max_admin_grant}")
    if not (reason or "").strip():
        raise AppError(ErrorCode.BAD_REQUEST, "reason is required")

    def fn() -> dict:
        act = _load_activity(db, activity_id, actor)
        grant, created = _grant(db, act, member_id, chances, G_ADMIN, f"staff:{actor.id}:{request_key}", actor.id,
                                reason[:200])
        _audit(db, actor, "lottery.chances.grant", activity_id, after={"member_id": member_id, "chances": chances},
               reason=reason)
        return _grant_view(grant, created)

    return _idem(db, f"lottery.grant:{activity_id}", request_key, f"staff:{actor.id}",
                 {"member_id": member_id, "chances": chances, "reason": reason}, fn)


def revoke_grant(db: Session, activity_id: int, source: str, source_ref: str) -> bool:
    """来源撤销（例如支付退款）：只收回未用的次数，已用的不追溯（不 commit）。"""
    grant = db.execute(select(LotteryChanceGrant).where(
        LotteryChanceGrant.activity_id == activity_id, LotteryChanceGrant.source == source,
        LotteryChanceGrant.source_ref == source_ref)).scalar_one_or_none()
    if grant is None:
        return False
    ok = db.execute(update(LotteryChanceGrant).where(LotteryChanceGrant.id == grant.id,
                                                     LotteryChanceGrant.status == GRANT_ACTIVE)
                    .values(status=GRANT_REVOKED)).rowcount
    if ok != 1:
        return False
    A = LotteryChanceAccount
    db.execute(update(A).where(A.activity_id == activity_id, A.member_id == grant.member_id).values(
        granted=case((A.granted - grant.chances >= A.used, A.granted - grant.chances), else_=A.used)))
    return True


GRANT_LIST = ListSpec(
    fields={
        "activity_id": Field(LotteryChanceGrant.activity_id, "int", ops=("eq", "in")),
        "member_id": Field(LotteryChanceGrant.member_id, "int"),
        "source": Field(LotteryChanceGrant.source, ops=("eq", "in"), choices=frozenset(GRANT_SOURCES)),
        "status": Field(LotteryChanceGrant.status, choices=frozenset({GRANT_ACTIVE, GRANT_REVOKED})),
        "created_at": Field(LotteryChanceGrant.created_at, "datetime", ops=("range",), sortable=True),
        "chances": Field(LotteryChanceGrant.chances, "int", ops=("range",), sortable=True),
    },
    id_column=LotteryChanceGrant.id,
    keyword_columns=(LotteryChanceGrant.source_ref,),
)


def list_grants(db: Session, actor: StaffActor, params: Mapping[str, Any]) -> dict:
    stmt = select(LotteryChanceGrant).join(LotteryActivity, LotteryActivity.id == LotteryChanceGrant.activity_id)
    stmt = _scoped(stmt, actor, LotteryActivity.store_id)
    return GRANT_LIST.page(db, stmt, params, serialize=_grant_view)


# ================================================================ 抽奖

def _consume_chance(db: Session, act: LotteryActivity, member_id: int, now: datetime) -> str:
    """消耗一次机会：先用当日免费，再用获得的次数；日 / 月 / 总上限全部条件 UPDATE，任何一步失败整笔回滚。"""
    cfg = get_config()
    day, month = rules.day_key(now, cfg.timezone), rules.month_key(now, cfg.timezone)
    _ensure_counters(db, act.id, member_id, day, month)
    P, A = LotteryChancePeriod, LotteryChanceAccount
    day_where = [P.activity_id == act.id, P.member_id == member_id, P.period == day]
    if act.limit_daily:
        day_where.append(P.draws < act.limit_daily)
    source = None
    if act.free_daily_chances > 0:
        if db.execute(update(P).where(*day_where, P.free_used < act.free_daily_chances)
                      .values(draws=P.draws + 1, free_used=P.free_used + 1)).rowcount == 1:
            source = SRC_FREE
    if source is None:
        if db.execute(update(P).where(*day_where).values(draws=P.draws + 1)).rowcount != 1:
            raise AppError(ErrorCode.LIMIT_REACHED, "daily draw limit reached", data={"reason": "daily_limit"})
        if db.execute(update(A).where(A.activity_id == act.id, A.member_id == member_id, A.used < A.granted)
                      .values(used=A.used + 1)).rowcount != 1:
            raise AppError(ErrorCode.LIMIT_REACHED, "no draw chances left", data={"reason": "no_chance"})
        source = SRC_EARNED
    month_where = [P.activity_id == act.id, P.member_id == member_id, P.period == month]
    if act.limit_monthly:
        month_where.append(P.draws < act.limit_monthly)
    if db.execute(update(P).where(*month_where).values(draws=P.draws + 1)).rowcount != 1:
        raise AppError(ErrorCode.LIMIT_REACHED, "monthly draw limit reached", data={"reason": "monthly_limit"})
    total_where = [A.activity_id == act.id, A.member_id == member_id]
    if act.limit_total:
        total_where.append(A.draws < act.limit_total)
    if db.execute(update(A).where(*total_where).values(draws=A.draws + 1)).rowcount != 1:
        raise AppError(ErrorCode.LIMIT_REACHED, "total draw limit reached", data={"reason": "total_limit"})
    return source


def _try_win(db: Session, act: LotteryActivity, member_id: int, prize: Mapping[str, Any]) -> str | None:
    """依次占用：个人中奖上限 → 库存（有限奖品，预算已在发布时预留）或预算余量（不限量奖品）；
    任一失败回退已占用的部分，返回退化原因。"""
    prize_id = int(prize["prize_id"])
    value = int(prize["value_cents"] or 0)
    limit = int(prize.get("per_member_limit") or 0)
    W, A, P = LotteryMemberPrizeWin, LotteryActivity, LotteryPrize
    if limit:
        insert_ignore(db, W, {"activity_id": act.id, "prize_id": prize_id, "member_id": member_id, "wins": 0})
        if db.execute(update(W).where(W.prize_id == prize_id, W.member_id == member_id, W.wins < limit)
                      .values(wins=W.wins + 1)).rowcount != 1:
            return FB_MEMBER_LIMIT
    unlimited = db.execute(select(P.stock_total.is_(None)).where(P.id == prize_id)).scalar_one()
    if unlimited:
        taken = db.execute(update(A).where(
            A.id == act.id, A.unlimited_spent_cents + value <= A.budget_cents - A.reserved_cents,
        ).values(unlimited_spent_cents=A.unlimited_spent_cents + value, spent_cents=A.spent_cents + value)).rowcount
        reason = FB_BUDGET
        if taken == 1:
            db.execute(update(P).where(P.id == prize_id).values(win_count=P.win_count + 1))
    else:
        taken = db.execute(update(P).where(P.id == prize_id, P.stock_left > 0)
                           .values(stock_left=P.stock_left - 1, win_count=P.win_count + 1)).rowcount
        reason = FB_OUT_OF_STOCK
        if taken == 1:
            db.execute(update(A).where(A.id == act.id).values(spent_cents=A.spent_cents + value))
    if taken != 1:
        if limit:
            db.execute(update(W).where(W.prize_id == prize_id, W.member_id == member_id).values(wins=W.wins - 1))
        return reason
    return None


def _open_for_draw(db: Session, act: LotteryActivity, now: datetime) -> None:
    if act.status not in (A_PUBLISHED, A_RUNNING):
        raise AppError(ErrorCode.BUSINESS_RULE, "activity is not open", data={"status": act.status})
    if now < rules.aware(act.start_at):
        raise AppError(ErrorCode.BUSINESS_RULE, "activity has not started", data={"start_at": rules.iso(act.start_at)})
    if now >= rules.aware(act.end_at):
        raise AppError(ErrorCode.BUSINESS_RULE, "activity has ended", data={"end_at": rules.iso(act.end_at)})
    if act.status == A_PUBLISHED:  # 到点惰性开始（任务也会推进）
        db.execute(update(LotteryActivity).where(LotteryActivity.id == act.id, LotteryActivity.status == A_PUBLISHED)
                   .values(status=A_RUNNING))


def draw(db: Session, activity_id: int, user_id: int, idempotency_key: str | None, *, store_id: int | None = None,
         ip: str | None = None, now: datetime | None = None) -> dict:
    """抽一次（CONTRACTS：draw(db, activity_id, user_id, idempotency_key)）。

    一个事务：消耗次数 → 随机选奖 → 占用个人上限 / 预算 / 库存（失败退化为谢谢参与）→ 写记录 → 发件箱事件。
    同一 Idempotency-Key 重放返回首次结果，不重抽。
    """
    now = _now(now)
    return _idem(db, f"lottery.draw:{activity_id}", idempotency_key, f"user:{user_id}", {"store_id": store_id},
                 lambda: _draw_tx(db, activity_id, user_id, idempotency_key or "", store_id, ip, now), now)


def _draw_tx(db: Session, activity_id: int, member_id: int, request_key: str, store_id: int | None, ip: str | None,
             now: datetime) -> dict:
    cfg = get_config()
    act = _public_activity(db, activity_id)
    _open_for_draw(db, act, now)
    if act.store_id is not None and store_id is not None and int(store_id) != act.store_id:
        raise AppError(ErrorCode.BUSINESS_RULE, "activity is not available in this store")
    if not hooks.audience_checker()(db, act, member_id):
        raise AppError(ErrorCode.BUSINESS_RULE, "you are not eligible for this activity", data={"reason": "audience"})
    member_limiter, ip_limiter = hooks.limiters()
    if member_limiter is not None and not member_limiter.allow(f"lottery:m:{member_id}"):
        raise AppError(ErrorCode.RATE_LIMITED, "too many draws, please slow down", retry_after=60)
    if ip and ip_limiter is not None and not ip_limiter.allow(f"lottery:ip:{ip}"):
        raise AppError(ErrorCode.RATE_LIMITED, "too many draws from this network", retry_after=60)
    hooks.risk_checker()(db, hooks.RiskContext(act.id, member_id, store_id, ip, request_key))

    source = _consume_chance(db, act, member_id, now)

    odds = json.loads(act.odds_json)
    pool = odds
    if act.out_of_stock_policy == OOS_EXCLUDE:
        empty = set(db.execute(select(LotteryPrize.id).where(
            LotteryPrize.activity_id == act.id, LotteryPrize.stock_left.is_not(None), LotteryPrize.stock_left <= 0,
        )).scalars().all())
        pool = [o for o in odds if o["prize_id"] not in empty]
    rng = hooks.random_source()
    total = rules.pool_range(pool)
    r = rng.randbelow(total)
    hit, _ = rules.pick(pool, r)
    result, reason = hit, None
    if hit["kind"] != P_THANKS:
        reason = _try_win(db, act, member_id, hit)
        if reason is not None:
            result = rules.thanks_entry(odds)
    won = result["kind"] != P_THANKS
    record = LotteryDraw(
        activity_id=act.id, member_id=member_id, store_id=act.store_id if act.store_id is not None else store_id,
        request_key=request_key, prize_id=result["prize_id"], prize_seq=result["seq"], prize_kind=result["kind"],
        prize_name=result["name"], ref_id=result.get("ref_id"), quantity=int(result.get("quantity") or 0),
        value_cents=int(result["value_cents"] or 0) if won else 0, won=won, hit_prize_id=hit["prize_id"],
        fallback_reason=reason, random_value=r, random_range=total, rng=getattr(rng, "name", "custom")[:16],
        odds_json=json.dumps(pool, ensure_ascii=False), rules_hash=act.rules_hash or "", chance_source=source, ip=ip,
        delivery_status=D_NONE, created_at=now, updated_at=now,
    )
    if won and result["kind"] in VIRTUAL_KINDS:
        record.delivery_status = D_PENDING
        record.delivery_attempts = 1
        record.delivery_due_at = now + timedelta(seconds=cfg.delivery_ack_timeout_seconds)
    elif won and result["kind"] == P_PHYSICAL:
        record.delivery_status = D_AWAITING
        record.claim_deadline = now + timedelta(days=act.claim_days)
    db.add(record)
    try:
        db.flush()
    except IntegrityError as exc:
        raise AppError(ErrorCode.DUPLICATE_REQUEST, "duplicate draw request", retry_after=1) from exc
    enqueue_event(db, "lottery.drawn", {"draw_id": record.id, "activity_id": act.id, "member_id": member_id,
                                        "won": won, "prize_kind": record.prize_kind}, "lottery_draw", record.id)
    if won:
        _enqueue_prize_won(db, record, attempt=1)
    return draw_view(record)


def prize_won_payload(record: LotteryDraw, attempt: int) -> dict:
    """`lottery.prize_won` v1 事件契约（消费方按 delivery_key 去重）。"""
    return {
        "draw_id": record.id, "activity_id": record.activity_id, "member_id": record.member_id,
        "store_id": record.store_id, "prize_id": record.prize_id, "prize_kind": record.prize_kind,
        "prize_name": record.prize_name, "ref_id": record.ref_id, "quantity": record.quantity,
        "value_cents": record.value_cents, "delivery_key": f"lottery:draw:{record.id}", "attempt": attempt,
        "won_at": rules.iso(record.created_at),
    }


def _enqueue_prize_won(db: Session, record: LotteryDraw, attempt: int) -> None:
    enqueue_event(db, "lottery.prize_won", prize_won_payload(record, attempt), "lottery_draw", record.id)


def draw_view(d: LotteryDraw, admin: bool = False) -> dict:
    out = {
        "id": d.id, "activity_id": d.activity_id, "won": d.won, "fallback_reason": d.fallback_reason,
        "prize": {"id": d.prize_id, "seq": d.prize_seq, "name": d.prize_name, "kind": d.prize_kind,
                  "quantity": d.quantity, "value_cents": d.value_cents},
        "delivery_status": d.delivery_status, "claim_deadline": rules.iso(d.claim_deadline),
        "carrier": d.carrier, "tracking_no": d.tracking_no, "shipped_at": rules.iso(d.shipped_at),
        "delivered_at": rules.iso(d.delivered_at), "created_at": rules.iso(d.created_at),
        "audit": {"random_value": d.random_value, "random_range": d.random_range, "rng": d.rng,
                  "rules_hash": d.rules_hash},
    }
    if admin:
        cipher = _cipher()
        out.update({
            "member_id": d.member_id, "store_id": d.store_id, "chance_source": d.chance_source, "ip": d.ip,
            "hit_prize_id": d.hit_prize_id, "ref_id": d.ref_id, "delivery_attempts": d.delivery_attempts,
            "delivery_due_at": rules.iso(d.delivery_due_at), "delivery_ref": d.delivery_ref,
            "delivery_error": d.delivery_error, "claimed_at": rules.iso(d.claimed_at),
            "claim": {"name": mask_name(cipher.decrypt(d.claim_name_enc)),
                      "phone": mask_phone(cipher.decrypt(d.claim_phone_enc)),
                      "address": mask_address(cipher.decrypt(d.claim_address_enc))} if d.claimed_at else None,
            "odds": json.loads(d.odds_json), "replay_ok": verify_draw(d),
        })
    return out


def verify_draw(d: LotteryDraw) -> bool:
    """用记录复现：随机值 + 当次奖池 → 命中奖品必须一致。"""
    try:
        return rules.replay(json.loads(d.odds_json), d.random_value, d.random_range) == d.hit_prize_id
    except (ValueError, AppError):
        return False


# ================================================================ 顾客端记录与实物领奖

MY_DRAWS = ListSpec(
    fields={
        "activity_id": Field(LotteryDraw.activity_id, "int"),
        "won": Field(LotteryDraw.won, "bool"),
        "delivery_status": Field(LotteryDraw.delivery_status, ops=("eq", "in"), choices=frozenset(DELIVERY_STATUSES)),
        "created_at": Field(LotteryDraw.created_at, "datetime", sortable=True),
    },
    id_column=LotteryDraw.id,
)


def my_records(db: Session, member_id: int, params: Mapping[str, Any], secret: str) -> dict:
    stmt = select(LotteryDraw).where(LotteryDraw.member_id == member_id)
    return MY_DRAWS.cursor(db, stmt, params, secret, serialize=draw_view)


def _own_draw(db: Session, member_id: int, draw_id: int) -> LotteryDraw:
    d = db.get(LotteryDraw, draw_id, populate_existing=True)
    if d is None or d.member_id != member_id:
        raise AppError(ErrorCode.NOT_FOUND, "record not found")
    return d


def my_record(db: Session, member_id: int, draw_id: int) -> dict:
    return draw_view(_own_draw(db, member_id, draw_id))


def claim_prize(db: Session, member_id: int, draw_id: int, name: str, phone: str, address: str,
                request_key: str | None, now: datetime | None = None) -> dict:
    """实物奖填写收货信息（期限内可修改，发货后不可改）。联系方式加密存储，后台默认脱敏。"""
    now = _now(now)
    name, phone, address = (name or "").strip(), (phone or "").strip(), (address or "").strip()
    if not 1 <= len(name) <= 32 or not PHONE.match(phone) or not 5 <= len(address) <= 200:
        raise AppError(ErrorCode.BAD_REQUEST, "name 1-32 chars, mainland mobile number, address 5-200 chars")

    def fn() -> dict:
        d = _own_draw(db, member_id, draw_id)
        if d.prize_kind != P_PHYSICAL or not d.won:
            raise AppError(ErrorCode.BUSINESS_RULE, "only physical prizes need claiming")
        if d.delivery_status not in (D_AWAITING, D_CLAIMED):
            raise AppError(ErrorCode.CONFLICT, "prize can no longer be claimed", data={"status": d.delivery_status})
        if d.claim_deadline is not None and now > rules.aware(d.claim_deadline):
            raise AppError(ErrorCode.BUSINESS_RULE, "claim period has expired",
                           data={"claim_deadline": rules.iso(d.claim_deadline)})
        cipher = _cipher()
        DELIVERY_SM.advance(db, LotteryDraw, d.id, d.delivery_status, D_CLAIMED,
                            status_column="delivery_status", claim_name_enc=cipher.encrypt(name),
                            claim_phone_enc=cipher.encrypt(phone), claim_address_enc=cipher.encrypt(address),
                            claimed_at=now, updated_at=now)
        db.refresh(d)
        return draw_view(d)

    return _idem(db, f"lottery.claim:{draw_id}", request_key, f"user:{member_id}",
                 {"name": name, "phone": phone, "address": address}, fn, now)


# ================================================================ 发奖（后台与回执）

def _staff_draw(db: Session, actor: StaffActor, draw_id: int) -> LotteryDraw:
    d = db.get(LotteryDraw, draw_id, populate_existing=True)
    if d is None:
        raise AppError(ErrorCode.NOT_FOUND, "record not found")
    act = db.get(LotteryActivity, d.activity_id)
    _assert_scope(actor, act.store_id if act is not None else d.store_id)
    return d


def get_record_admin(db: Session, actor: StaffActor, draw_id: int) -> dict:
    return draw_view(_staff_draw(db, actor, draw_id), admin=True)


def reveal_claim_contact(db: Session, actor: StaffActor, draw_id: int) -> dict:
    """发货用：查看收货人明文，写敏感查看审计。"""
    d = _staff_draw(db, actor, draw_id)
    if d.claimed_at is None:
        raise AppError(ErrorCode.CONFLICT, "prize has not been claimed")
    cipher = _cipher()
    _audit(db, actor, "lottery_draw.sensitive_read", d.id, after={"fields": ["name", "phone", "address"]},
           resource="lottery_draw")
    db.commit()
    return {"id": d.id, "name": cipher.decrypt(d.claim_name_enc), "phone": cipher.decrypt(d.claim_phone_enc),
            "address": cipher.decrypt(d.claim_address_enc)}


def ship_prize(db: Session, actor: StaffActor, draw_id: int, carrier: str, tracking_no: str, request_key: str | None,
               now: datetime | None = None) -> dict:
    now = _now(now)

    def fn() -> dict:
        d = _staff_draw(db, actor, draw_id)
        if d.prize_kind != P_PHYSICAL:
            raise AppError(ErrorCode.BUSINESS_RULE, "only physical prizes are shipped")
        DELIVERY_SM.advance(db, LotteryDraw, d.id, d.delivery_status, D_SHIPPED, status_column="delivery_status",
                            extra_where=(LotteryDraw.delivery_status == D_CLAIMED,), carrier=carrier[:32],
                            tracking_no=tracking_no[:64], shipped_at=now, updated_at=now)
        enqueue_event(db, "lottery.prize_shipped", {"draw_id": d.id, "member_id": d.member_id, "carrier": carrier,
                                                    "tracking_no": tracking_no}, "lottery_draw", d.id)
        _audit(db, actor, "lottery.prize.ship", d.id, before={"delivery_status": d.delivery_status},
               after={"delivery_status": D_SHIPPED, "carrier": carrier, "tracking_no": tracking_no},
               resource="lottery_draw")
        db.refresh(d)
        return draw_view(d, admin=True)

    return _idem(db, f"lottery.ship:{draw_id}", request_key, f"staff:{actor.id}",
                 {"carrier": carrier, "tracking_no": tracking_no}, fn, now)


def confirm_received(db: Session, actor: StaffActor, draw_id: int, request_key: str | None,
                     now: datetime | None = None) -> dict:
    now = _now(now)

    def fn() -> dict:
        d = _staff_draw(db, actor, draw_id)
        DELIVERY_SM.advance(db, LotteryDraw, d.id, d.delivery_status, D_DELIVERED, status_column="delivery_status",
                            extra_where=(LotteryDraw.delivery_status == D_SHIPPED,), delivered_at=now, updated_at=now)
        _audit(db, actor, "lottery.prize.received", d.id, after={"delivery_status": D_DELIVERED}, resource="lottery_draw")
        db.refresh(d)
        return draw_view(d, admin=True)

    return _idem(db, f"lottery.received:{draw_id}", request_key, f"staff:{actor.id}", {}, fn, now)


def retry_delivery(db: Session, actor: StaffActor, draw_id: int, request_key: str | None,
                   now: datetime | None = None) -> dict:
    """人工重试：failed → pending，重新发出 lottery.prize_won（同一 delivery_key，消费方去重）。"""
    now = _now(now)

    def fn() -> dict:
        d = _staff_draw(db, actor, draw_id)
        DELIVERY_SM.advance(db, LotteryDraw, d.id, d.delivery_status, D_PENDING, status_column="delivery_status",
                            delivery_attempts=1, delivery_error=None, updated_at=now,
                            delivery_due_at=now + timedelta(seconds=get_config().delivery_ack_timeout_seconds))
        db.refresh(d)
        _enqueue_prize_won(db, d, attempt=1)
        _audit(db, actor, "lottery.delivery.retry", d.id, after={"delivery_status": D_PENDING}, resource="lottery_draw")
        return draw_view(d, admin=True)

    return _idem(db, f"lottery.retry:{draw_id}", request_key, f"staff:{actor.id}", {}, fn, now)


def ack_delivery(db: Session, draw_id: int, ok: bool, ref: str | None = None, error: str | None = None,
                 now: datetime | None = None) -> str:
    """消费方回执（不 commit）。成功：pending / failed → delivered；失败：记录原因，交给重试任务按退避重发。"""
    now = _now(now)
    d = db.get(LotteryDraw, draw_id, populate_existing=True)
    if d is None or d.prize_kind not in VIRTUAL_KINDS or not d.won:
        return "ignored"
    D = LotteryDraw
    if ok:
        n = db.execute(update(D).where(D.id == draw_id, D.delivery_status.in_((D_PENDING, D_FAILED))).values(
            delivery_status=D_DELIVERED, delivered_at=now, delivery_ref=(ref or "")[:128] or None,
            delivery_error=None, updated_at=now)).rowcount
        if n == 1:
            enqueue_event(db, "lottery.prize_delivered", {"draw_id": draw_id, "member_id": d.member_id,
                                                          "prize_kind": d.prize_kind}, "lottery_draw", draw_id)
        return "delivered" if n == 1 else "duplicate"
    n = db.execute(update(D).where(D.id == draw_id, D.delivery_status == D_PENDING).values(
        delivery_error=(error or "failed")[:255], delivery_due_at=now, updated_at=now)).rowcount
    return "retry_scheduled" if n == 1 else "ignored"


# ================================================================ 后台记录列表 / 导出 / 统计

RECORD_LIST = ListSpec(
    fields={
        "activity_id": Field(LotteryDraw.activity_id, "int", ops=("eq", "in")),
        "prize_id": Field(LotteryDraw.prize_id, "int", ops=("eq", "in")),
        "prize_kind": Field(LotteryDraw.prize_kind, ops=("eq", "in"), choices=frozenset(PRIZE_KINDS)),
        "won": Field(LotteryDraw.won, "bool"),
        "member_id": Field(LotteryDraw.member_id, "int"),
        "store_id": Field(LotteryDraw.store_id, "int", ops=("eq", "in")),
        "delivery_status": Field(LotteryDraw.delivery_status, ops=("eq", "in"), choices=frozenset(DELIVERY_STATUSES)),
        "fallback_reason": Field(LotteryDraw.fallback_reason, ops=("eq", "null"),
                                 choices=frozenset({FB_OUT_OF_STOCK, FB_MEMBER_LIMIT, FB_BUDGET})),
        "value_cents": Field(LotteryDraw.value_cents, "int", ops=("range",), sortable=True),
        "created_at": Field(LotteryDraw.created_at, "datetime", ops=("range",), sortable=True),
    },
    id_column=LotteryDraw.id,
    keyword_columns=(cast(LotteryDraw.member_id, String), LotteryDraw.prize_name, LotteryDraw.tracking_no),
)

EXPORT_HEADERS = ("记录ID", "活动ID", "会员ID", "门店ID", "奖项序号", "奖品", "类型", "价值(元)", "中奖", "退化原因",
                  "发奖状态", "发奖尝试", "快递公司", "运单号", "抽奖时间", "到账/签收时间", "随机值", "随机范围", "规则哈希")


def _records_stmt(actor: StaffActor):
    return _scoped(select(LotteryDraw), actor, LotteryDraw.store_id)


def list_records(db: Session, actor: StaffActor, params: Mapping[str, Any]) -> dict:
    return RECORD_LIST.page(db, _records_stmt(actor), params, serialize=lambda d: draw_view(d, admin=False) | {
        "member_id": d.member_id, "store_id": d.store_id, "delivery_attempts": d.delivery_attempts})


def export_records(db: Session, actor: StaffActor, params: Mapping[str, Any]):
    """CSV（不含收货人信息）；导出写审计。"""
    stream = export_csv(db, RECORD_LIST, _records_stmt(actor), params, EXPORT_HEADERS, lambda d: (
        d.id, d.activity_id, d.member_id, d.store_id, d.prize_seq, d.prize_name, d.prize_kind,
        cents_to_yuan(d.value_cents), d.won, d.fallback_reason, d.delivery_status, d.delivery_attempts, d.carrier,
        d.tracking_no, rules.aware(d.created_at), rules.aware(d.delivered_at), d.random_value, d.random_range,
        d.rules_hash))
    _audit(db, actor, "lottery.records.export", "-", after={"params": {k: str(v) for k, v in params.items()}},
           resource="lottery_draw")
    db.commit()
    return stream


def stats(db: Session, actor: StaffActor, activity_id: int) -> dict:
    act = _load_activity(db, activity_id, actor)
    D = LotteryDraw
    base = (D.activity_id == activity_id,)
    draws, members, wins, cost = db.execute(select(
        func.count(D.id), func.count(func.distinct(D.member_id)),
        func.coalesce(func.sum(case((D.won.is_(True), 1), else_=0)), 0),
        func.coalesce(func.sum(case((D.won.is_(True), D.value_cents), else_=0)), 0),
    ).where(*base)).one()
    delivered_cost = db.execute(select(func.coalesce(func.sum(D.value_cents), 0)).where(
        *base, D.won.is_(True), D.delivery_status == D_DELIVERED)).scalar_one()
    by_prize = {int(pid): (int(c), int(v)) for pid, c, v in db.execute(select(
        D.prize_id, func.count(D.id), func.coalesce(func.sum(D.value_cents), 0)).where(*base).group_by(D.prize_id)).all()}
    by_status = {s: int(c) for s, c in db.execute(select(D.delivery_status, func.count(D.id)).where(*base)
                                                  .group_by(D.delivery_status)).all()}
    fallbacks = {s: int(c) for s, c in db.execute(select(D.fallback_reason, func.count(D.id)).where(
        *base, D.fallback_reason.is_not(None)).group_by(D.fallback_reason)).all()}
    granted = db.execute(select(func.coalesce(func.sum(LotteryChanceGrant.chances), 0)).where(
        LotteryChanceGrant.activity_id == activity_id, LotteryChanceGrant.status == GRANT_ACTIVE)).scalar_one()
    return {
        "activity_id": activity_id, "draws": int(draws), "members": int(members), "wins": int(wins),
        "win_rate_bp": int(wins) * ODDS_BASIS // int(draws) if draws else 0,
        "cost_cents": int(cost), "delivered_cost_cents": int(delivered_cost), "budget_cents": act.budget_cents,
        "spent_cents": act.spent_cents, "chances_granted": int(granted), "by_delivery_status": by_status,
        "fallbacks": fallbacks,
        "prizes": [{"prize_id": p.id, "seq": p.seq, "name": p.name, "kind": p.kind, "weight_bp": p.weight_bp,
                    "stock_total": p.stock_total, "stock_left": p.stock_left,
                    "draws": by_prize.get(p.id, (0, 0))[0], "cost_cents": by_prize.get(p.id, (0, 0))[1]}
                   for p in _prizes(db, activity_id)],
    }


# ================================================================ 公示

WINNER_LIST = ListSpec(
    fields={"created_at": Field(LotteryDraw.created_at, "datetime", sortable=True)},
    id_column=LotteryDraw.id,
)


def winners(db: Session, activity_id: int, params: Mapping[str, Any], secret: str) -> dict:
    """超过公示门槛（默认 500 元）的奖项兑奖情况，会员信息脱敏（第十三条第二款）。"""
    act = _public_activity(db, activity_id)
    display = hooks.member_display()
    stmt = select(LotteryDraw).where(LotteryDraw.activity_id == activity_id, LotteryDraw.won.is_(True),
                                     LotteryDraw.value_cents > act.publicity_threshold_cents)
    return WINNER_LIST.cursor(db, stmt, params, secret, serialize=lambda d: {
        "id": d.id, "member": rules.mask_member(d.member_id, display(db, d.member_id)), "prize_name": d.prize_name,
        "prize_kind": d.prize_kind, "value_cents": d.value_cents, "won_at": rules.iso(d.created_at),
        "delivery_status": d.delivery_status, "delivered_at": rules.iso(d.delivered_at)})


# ================================================================ 事件消费

def handle_event(db: Session, topic: str, payload: Mapping[str, Any], event_id: str | None = None,
                 now: datetime | None = None) -> str:
    """发件箱消费者入口（自行 commit）：
    - payment.succeeded → 满额送次数；payment.refunded → 收回未用次数；
    - member.checked_in（可配置）→ 签到送次数；lottery.delivery_result → 发奖回执。
    """
    cfg = get_config()
    now = _now(now)
    try:
        if event_id and not mark_processed(db, CONSUMER, event_id):
            return "duplicate"
        if topic == cfg.payment_succeeded_topic:
            result = _on_payment(db, payload, now)
        elif topic == cfg.payment_refunded_topic:
            ref = str(payload.get("payment_id") or "")
            ids = db.execute(select(LotteryChanceGrant.activity_id).where(
                LotteryChanceGrant.source == G_PAYMENT, LotteryChanceGrant.source_ref == ref)).scalars().all()
            result = f"revoked:{sum(1 for a in ids if revoke_grant(db, a, G_PAYMENT, ref))}" if ref else "skipped"
        elif topic == cfg.checkin_topic:
            result = _on_checkin(db, payload, now)
        elif topic == cfg.delivery_result_topic:
            result = ack_delivery(db, int(payload["draw_id"]), bool(payload.get("ok")), payload.get("ref"),
                                  payload.get("error"), now)
        else:
            result = "skipped"
        db.commit()
        return result
    except Exception:
        db.rollback()
        raise


def _eligible(db: Session, store_id: Any, at: datetime, extra) -> list[LotteryActivity]:
    stmt = select(LotteryActivity).where(
        LotteryActivity.deleted_at.is_(None), LotteryActivity.status.in_((A_PUBLISHED, A_RUNNING)),
        LotteryActivity.start_at <= at, LotteryActivity.end_at > at, extra)
    if store_id is not None:
        stmt = stmt.where(or_(LotteryActivity.store_id.is_(None), LotteryActivity.store_id == int(store_id)))
    else:
        stmt = stmt.where(LotteryActivity.store_id.is_(None))
    return list(db.execute(stmt.order_by(LotteryActivity.id)).scalars().all())


def _member_of(payload: Mapping[str, Any]) -> int | None:
    raw = payload.get("member_id") or payload.get("user_id")
    return int(raw) if raw else None


def _on_payment(db: Session, payload: Mapping[str, Any], now: datetime) -> str:
    cfg = get_config()
    member_id, payment_id = _member_of(payload), payload.get("payment_id")
    amount = int(payload.get("amount_cents") or 0)
    btype = payload.get("business_type")
    if not member_id or not payment_id or (btype is not None and btype not in cfg.earn_payment_business_types):
        return "skipped"
    at = rules.aware(datetime.fromisoformat(payload["paid_at"])) if payload.get("paid_at") else now
    granted = 0
    for act in _eligible(db, payload.get("store_id"), at, LotteryActivity.earn_payment_min_cents > 0):
        if amount >= act.earn_payment_min_cents:
            _, created = _grant(db, act, member_id, act.earn_payment_chances, G_PAYMENT, str(payment_id))
            granted += int(created)
    return f"granted:{granted}"


def _on_checkin(db: Session, payload: Mapping[str, Any], now: datetime) -> str:
    member_id = _member_of(payload)
    ref = payload.get("checkin_id") or payload.get("date")
    if not member_id or not ref:
        return "skipped"
    at = rules.aware(datetime.fromisoformat(payload["checked_in_at"])) if payload.get("checked_in_at") else now
    granted = 0
    for act in _eligible(db, payload.get("store_id"), at, LotteryActivity.earn_checkin_chances > 0):
        _, created = _grant(db, act, member_id, act.earn_checkin_chances, G_CHECKIN, f"{member_id}:{ref}")
        granted += int(created)
    return f"granted:{granted}"


# ================================================================ 杂项

def _cmp(v: Any) -> Any:
    if isinstance(v, datetime):
        return rules.aware(v).isoformat()
    return v


def _jsonable(d: Mapping[str, Any]) -> dict:
    return {k: _cmp(v) for k, v in d.items()}
