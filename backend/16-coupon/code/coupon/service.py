"""coupon 用例（全部规则在这里）。

事务约定（修复 R7 指出的“模块内 commit 破坏调用方事务”）：
- 本文件的用例函数**不 commit、不 rollback**，只写入调用方的会话；HTTP 接口由 `run_idempotent` 统一提交，
  订单等模块在自己的下单 / 支付 / 关单事务里直接调用 `lock / redeem / unlock / return_on_refund`。
- 抛出 AppError 时，调用方回滚整个事务（唯一约束冲突后会话必须回滚）。
- 例外：`jobs.py` 的定时任务是顶层用例，自己分批提交；`events.py` 的订阅函数同样不提交，由事件 worker 提交。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import String, case, cast, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from luzback import AppError, ErrorCode, Field, ListSpec, StateMachine, enqueue_event, get_settings, utcnow, write_audit
from luzback.business import business_value
from luzback.audit import diff
from luzback.crypto import FieldCipher

from . import rules as R
from .config import get_audience_checker, get_config, get_member_profile_provider
from .models import (
    AUDIENCE_ALL, AUDIENCE_LEVELS, AUDIENCE_NEW_MEMBERS, AUDIENCE_TAGS, BATCH_DONE, BATCH_PENDING, CHANNEL_ADMIN,
    CHANNEL_CLAIM_CENTER, CHANNEL_CODE, CHANNEL_COMPENSATION, CHANNEL_EVENT, CHANNEL_LOTTERY, CHANNELS, CODE_UNUSED,
    CODE_USED, CODE_VOID, COUPON_STATUSES, ITEM_FAILED, ITEM_ISSUED, ITEM_PENDING, SELF_SERVICE_CHANNELS,
    STATUS_EXPIRED, STATUS_LOCKED, STATUS_REVOKED, STATUS_UNUSED, STATUS_USED, TEMPLATE_ACTIVE, TEMPLATE_DRAFT,
    TEMPLATE_ENDED, TEMPLATE_PAUSED, TEMPLATE_STATUSES, TEMPLATE_TYPES, VALID_FIXED, CouponCode, CouponCodeAttempt,
    CouponCodeBatch, CouponIssueBatch, CouponIssueBatchItem, CouponIssueLog, CouponTemplate, UserCoupon,
)

aware = R.aware

TEMPLATE_FLOW = StateMachine(
    "coupon_template",
    {TEMPLATE_DRAFT: {TEMPLATE_ACTIVE}, TEMPLATE_ACTIVE: {TEMPLATE_PAUSED, TEMPLATE_ENDED},
     TEMPLATE_PAUSED: {TEMPLATE_ACTIVE, TEMPLATE_ENDED}},
    terminal=frozenset({TEMPLATE_ENDED}),
)
COUPON_FLOW = StateMachine(
    "user_coupon",
    {STATUS_UNUSED: {STATUS_LOCKED, STATUS_EXPIRED, STATUS_REVOKED},
     STATUS_LOCKED: {STATUS_UNUSED, STATUS_USED, STATUS_EXPIRED},
     STATUS_USED: {STATUS_UNUSED, STATUS_EXPIRED}},
    terminal=frozenset({STATUS_EXPIRED, STATUS_REVOKED}),
)

EDITABLE_FIELDS = (
    "name", "description", "store_id", "type", "amount_cents", "rate_bp", "max_discount_cents", "exchange_qty",
    "threshold_cents", "scope_type", "scope_ids", "exclude_product_ids", "stack_group", "stack_with_campaign",
    "stack_with_member", "valid_type", "use_start_at", "use_end_at", "valid_days", "start_delay_days",
    "claim_start_at", "claim_end_at", "total_stock", "per_member_limit", "per_member_daily_limit", "audience",
    "channels", "trigger_topic",
)
JSON_FIELDS = {"scope_ids": "scope_ids_json", "exclude_product_ids": "exclude_product_ids_json",
               "audience": "audience_json", "channels": "channels_json"}


def rule(message: str, data: Any = None) -> AppError:
    return AppError(ErrorCode.BUSINESS_RULE, message, data)


def limit(message: str, data: Any = None) -> AppError:
    return AppError(ErrorCode.LIMIT_REACHED, message, data)


def _now(now: datetime | None) -> datetime:
    return aware(now) if now else utcnow()


def _local_day(now: datetime) -> str:
    return now.astimezone(ZoneInfo(get_config().timezone)).date().isoformat()


def _cipher() -> FieldCipher:
    return FieldCipher(get_settings().field_encryption_key)


# ================================================================ 模板


def template_fields(t: CouponTemplate) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key in EDITABLE_FIELDS:
        if key in JSON_FIELDS:
            out[key] = R.loads(getattr(t, JSON_FIELDS[key]), {} if key == "audience" else [])
        else:
            out[key] = getattr(t, key)
    for key in ("use_start_at", "use_end_at", "claim_start_at", "claim_end_at"):
        out[key] = aware(out[key])
    return out


def _assign(t: CouponTemplate, data: dict[str, Any]) -> None:
    for key in EDITABLE_FIELDS:
        if key in JSON_FIELDS:
            setattr(t, JSON_FIELDS[key], json.dumps(data[key], ensure_ascii=False))
        else:
            setattr(t, key, data[key])


def template_view(t: CouponTemplate, admin: bool = False) -> dict[str, Any]:
    f = template_fields(t)
    remaining = None if t.total_stock is None else max(0, t.total_stock - t.issued_count)
    view = {
        "id": t.id, "name": t.name, "description": t.description, "type": t.type, "store_id": t.store_id,
        "status": t.status, "rules": R.snapshot(f), "rules_text": R.describe(R.snapshot(f)),
        "validity_text": R.describe_validity(f, get_config().timezone),
        "valid_type": t.valid_type, "use_start_at": f["use_start_at"], "use_end_at": f["use_end_at"],
        "valid_days": t.valid_days, "start_delay_days": t.start_delay_days,
        "claim_start_at": f["claim_start_at"], "claim_end_at": f["claim_end_at"],
        "total_stock": t.total_stock, "remaining_stock": remaining, "sold_out": remaining == 0,
        "per_member_limit": t.per_member_limit, "per_member_daily_limit": t.per_member_daily_limit,
    }
    if admin:
        view.update({
            "issued_count": t.issued_count, "used_count": t.used_count, "audience": f["audience"],
            "channels": f["channels"], "trigger_topic": t.trigger_topic, "version": t.version,
            "published_at": aware(t.published_at), "created_at": aware(t.created_at), "updated_at": aware(t.updated_at),
            "created_by": t.created_by, "updated_by": t.updated_by,
        })
    return view


def get_template(db: Session, template_id: int) -> CouponTemplate:
    # populate_existing：条件 UPDATE 不同步会话内对象，读模板一律取库里的当前值
    t = db.get(CouponTemplate, template_id, populate_existing=True)
    if t is None or t.deleted_at is not None:
        raise AppError(ErrorCode.NOT_FOUND, "coupon template not found")
    return t


def create_template(db: Session, staff_id: int, data: dict[str, Any], now: datetime | None = None) -> CouponTemplate:
    now = _now(now)
    clean = R.validate_template({k: data.get(k) for k in EDITABLE_FIELDS if k in data} | {"name": data.get("name")})
    t = CouponTemplate(status=TEMPLATE_DRAFT, version=1, issued_count=0, used_count=0, created_by=staff_id,
                       updated_by=staff_id, created_at=now, updated_at=now)
    _assign(t, clean)
    db.add(t)
    db.flush()
    write_audit(db, "staff", staff_id, "coupon.template.create", "coupon_template", t.id, after=_audit_view(clean))
    return t


def _audit_view(fields: dict[str, Any]) -> dict[str, Any]:
    return json.loads(R.dumps(fields))


def update_template(db: Session, staff_id: int, template_id: int, changes: dict[str, Any],
                    expected_version: int | None = None, apply_to_issued: bool = False,
                    now: datetime | None = None) -> CouponTemplate:
    """编辑模板。草稿任意修改；已发布只能按 `rules.check_published_edit` 放宽，使用规则的修改只影响之后发出的券
    （已发出的券保留领取时的快照）。apply_to_issued=True 时，仅当修改对顾客全部有利才同步给未使用的券。"""
    now = _now(now)
    t = get_template(db, template_id)
    if t.status == TEMPLATE_ENDED:
        raise AppError(ErrorCode.CONFLICT, "ended templates cannot be edited")
    if expected_version is not None and expected_version != t.version:
        raise AppError(ErrorCode.CONFLICT, "template was changed by someone else, reload and retry",
                       {"version": t.version})
    old = template_fields(t)
    unknown = set(changes) - set(EDITABLE_FIELDS)
    if unknown:
        raise R.bad(f"fields not editable: {', '.join(sorted(unknown))}")
    new = R.validate_template({**old, **changes})
    published = t.status != TEMPLATE_DRAFT
    if published:
        R.check_published_edit(old, new, t.issued_count)
    changed = diff(_audit_view(old), _audit_view(new))
    if not changed:
        return t
    old_rules, new_rules = R.snapshot(old), R.snapshot(new)
    rules_changed = old_rules != new_rules or any(old[k] != new[k] for k in R.VALIDITY_FIELDS)
    if apply_to_issued:
        if not published:
            raise rule("apply_to_issued only applies to published templates")
        worse = R.unfavorable_changes(old_rules, new_rules)
        if not R.validity_favorable(old, new):
            worse.append("validity")
        if worse:
            raise rule("changes are not favorable to customers and cannot apply to issued coupons", {"fields": worse})
    values: dict[str, Any] = {}
    probe = CouponTemplate()
    _assign(probe, new)
    for key in EDITABLE_FIELDS:
        column = JSON_FIELDS.get(key, key)
        values[column] = getattr(probe, column)
    values.update(version=CouponTemplate.version + 1, updated_by=staff_id, updated_at=now)
    rows = db.execute(update(CouponTemplate).where(
        CouponTemplate.id == t.id, CouponTemplate.version == t.version, CouponTemplate.status == t.status,
        CouponTemplate.issued_count <= (new["total_stock"] if new["total_stock"] is not None else R.MAX_STOCK),
    ).values(**values)).rowcount
    if rows != 1:
        raise AppError(ErrorCode.CONFLICT, "template changed concurrently, reload and retry")
    db.refresh(t)
    synced = 0
    if apply_to_issued and rules_changed:
        coupon_values: dict[str, Any] = {"rules_json": R.dumps(new_rules), "template_version": t.version,
                                         "name": new["name"]}
        if new["valid_type"] == VALID_FIXED:
            coupon_values.update(valid_from=new["use_start_at"], valid_to=new["use_end_at"])
        synced = db.execute(update(UserCoupon).where(
            UserCoupon.template_id == t.id, UserCoupon.status == STATUS_UNUSED).values(**coupon_values)).rowcount
    write_audit(db, "staff", staff_id, "coupon.template.update", "coupon_template", t.id,
                before={k: v["from"] for k, v in changed.items()},
                after={**{k: v["to"] for k, v in changed.items()}, "apply_to_issued": apply_to_issued, "synced": synced})
    return t


def _transition(db: Session, staff_id: int, template_id: int, target: str, action: str,
                now: datetime | None = None, **values: Any) -> CouponTemplate:
    now = _now(now)
    t = get_template(db, template_id)
    source = t.status
    if source == target:
        return t  # 重复操作幂等
    TEMPLATE_FLOW.advance(db, CouponTemplate, t.id, source, target, extra_where=(CouponTemplate.deleted_at.is_(None),),
                          updated_by=staff_id, updated_at=now, **values)
    db.refresh(t)
    write_audit(db, "staff", staff_id, action, "coupon_template", t.id, before={"status": source},
                after={"status": target})
    enqueue_event(db, "coupon.template_status_changed", {"template_id": t.id, "from": source, "status": target},
                  "coupon_template", t.id)
    return t


def publish_template(db: Session, staff_id: int, template_id: int, now: datetime | None = None) -> CouponTemplate:
    now = _now(now)
    t = get_template(db, template_id)
    if t.status == TEMPLATE_DRAFT:
        f = R.validate_template(template_fields(t))
        if f["claim_end_at"] and f["claim_end_at"] <= now:
            raise rule("claim period already ended")
        if f["valid_type"] == VALID_FIXED and f["use_end_at"] <= now:
            raise rule("validity period already ended")
    return _transition(db, staff_id, template_id, TEMPLATE_ACTIVE, "coupon.template.publish", now,
                       published_at=t.published_at or now)


def pause_template(db: Session, staff_id: int, template_id: int, now: datetime | None = None) -> CouponTemplate:
    return _transition(db, staff_id, template_id, TEMPLATE_PAUSED, "coupon.template.pause", now)


def resume_template(db: Session, staff_id: int, template_id: int, now: datetime | None = None) -> CouponTemplate:
    t = get_template(db, template_id)
    if t.status != TEMPLATE_PAUSED and t.status != TEMPLATE_ACTIVE:
        raise AppError(ErrorCode.CONFLICT, f"coupon_template: illegal transition {t.status} -> active")
    return _transition(db, staff_id, template_id, TEMPLATE_ACTIVE, "coupon.template.resume", now)


def end_template(db: Session, staff_id: int, template_id: int, now: datetime | None = None) -> CouponTemplate:
    """结束发放：之后不能再领取 / 发放；已发出的券照常可用到有效期结束（已作出的优惠承诺应当履行）。"""
    return _transition(db, staff_id, template_id, TEMPLATE_ENDED, "coupon.template.end", now)


def delete_template(db: Session, staff_id: int, template_id: int, now: datetime | None = None) -> None:
    now = _now(now)
    t = get_template(db, template_id)
    rows = db.execute(update(CouponTemplate).where(
        CouponTemplate.id == t.id, CouponTemplate.status == TEMPLATE_DRAFT, CouponTemplate.deleted_at.is_(None),
    ).values(deleted_at=now, updated_by=staff_id)).rowcount
    if rows != 1:
        raise AppError(ErrorCode.CONFLICT, "only draft templates can be deleted; end a published template instead")
    write_audit(db, "staff", staff_id, "coupon.template.delete", "coupon_template", t.id, before={"status": t.status})


# ================================================================ 发放（领取 / 后台 / 事件 / 抽奖 / 兑换码）


def _default_audience(db: Session, user_id: int, audience: dict, now: datetime) -> str | None:
    kind = audience.get("type", AUDIENCE_ALL)
    if kind == AUDIENCE_ALL:
        return None
    provider = get_member_profile_provider()
    if provider is None:
        return "audience_unavailable"
    profile = provider(db, user_id)
    if profile is None:
        return "member_unknown"
    if kind == AUDIENCE_NEW_MEMBERS:
        joined = aware(profile.registered_at)
        if joined is None or now - joined > timedelta(days=audience.get("days", 7)):
            return "not_new_member"
        return None
    if kind == AUDIENCE_LEVELS:
        return None if profile.level in audience.get("levels", []) else "level_not_matched"
    if kind == AUDIENCE_TAGS:
        return None if set(profile.tags) & set(audience.get("tags", [])) else "tag_not_matched"
    return "audience_unknown"


def check_audience(db: Session, user_id: int, audience: dict, now: datetime) -> str | None:
    checker = get_audience_checker() or _default_audience
    return checker(db, user_id, audience, now)


def _issue(db: Session, template_id: int, user_id: int, channel: str, now: datetime, *, ref_key: str | None = None,
           source_ref: str | None = None, enforce_limits: bool = True) -> tuple[UserCoupon, bool]:
    """发一张券；返回 (券, 是否为重放)。顺序：业务键去重 → 模板与渠道 → 领取期 → 人群 → 每人限领 →
    条件扣库存 → 插入（槽位唯一约束裁决并发）→ 发放日志 → 事件。不 commit。"""
    if channel not in CHANNELS:
        raise R.bad("unknown channel")
    if ref_key:
        log = db.execute(select(CouponIssueLog).where(CouponIssueLog.ref_key == ref_key)).scalar_one_or_none()
        if log is not None:
            if log.user_id != user_id or log.template_id != template_id:
                raise AppError(ErrorCode.CONFLICT, "ref_key already used for another coupon")
            return db.get(UserCoupon, log.user_coupon_id), True
    t = get_template(db, template_id)
    if t.status != TEMPLATE_ACTIVE:
        raise rule("coupon is not available", {"reason": "not_active"})
    f = template_fields(t)
    if channel not in f["channels"]:
        raise rule("coupon cannot be issued through this channel", {"reason": "channel"})
    if channel in SELF_SERVICE_CHANNELS:
        if f["claim_start_at"] and now < f["claim_start_at"]:
            raise rule("claiming has not started", {"reason": "not_started"})
        if f["claim_end_at"] and now >= f["claim_end_at"]:
            raise rule("claiming has ended", {"reason": "claim_ended"})
    if f["valid_type"] == VALID_FIXED and now >= f["use_end_at"]:
        raise rule("coupon validity has ended", {"reason": "validity_ended"})
    if channel in (CHANNEL_CLAIM_CENTER, CHANNEL_CODE, CHANNEL_EVENT):
        reason = check_audience(db, user_id, f["audience"], now)
        if reason:
            raise rule("not eligible for this coupon", {"reason": reason})

    day = _local_day(now)
    per_limit, template_version, template_name = t.per_member_limit, t.version, t.name
    owned, today = db.execute(
        select(func.count(), func.coalesce(func.sum(case((UserCoupon.claim_day == day, 1), else_=0)), 0))
        .where(UserCoupon.template_id == t.id, UserCoupon.user_id == user_id)
    ).one()
    if enforce_limits:
        if t.per_member_limit and owned >= t.per_member_limit:
            raise limit("per-member limit reached", {"reason": "per_member_limit", "limit": t.per_member_limit})
        if channel in SELF_SERVICE_CHANNELS and t.per_member_daily_limit and today >= t.per_member_daily_limit:
            raise limit("daily limit reached", {"reason": "daily_limit", "limit": t.per_member_daily_limit})

    taken = db.execute(update(CouponTemplate).where(
        CouponTemplate.id == t.id, CouponTemplate.status == TEMPLATE_ACTIVE, CouponTemplate.deleted_at.is_(None),
        or_(CouponTemplate.total_stock.is_(None), CouponTemplate.issued_count < CouponTemplate.total_stock),
    ).values(issued_count=CouponTemplate.issued_count + 1)).rowcount
    if taken != 1:
        status = db.execute(select(CouponTemplate.status).where(CouponTemplate.id == t.id)).scalar_one()
        if status != TEMPLATE_ACTIVE:
            raise rule("coupon is not available", {"reason": "not_active"})
        raise limit("coupon sold out", {"reason": "sold_out"})

    valid_from, valid_to = R.validity_window(f, now)
    coupon = UserCoupon(
        user_id=user_id, template_id=template_id, template_version=template_version, name=template_name,
        rules_json=R.dumps(R.snapshot(f)), slot=owned + 1, claim_day=day, day_slot=today + 1,
        status=STATUS_UNUSED, channel=channel, source_ref=source_ref, claimed_at=now,
        valid_from=valid_from, valid_to=valid_to, discount_cents=0, created_at=now, updated_at=now,
    )
    db.add(coupon)
    try:
        db.flush()
    except IntegrityError as exc:
        # 同一会员并发领取抢到同一个槽位：只有一个成功；调用方回滚（库存扣减随之回滚）
        if per_limit and owned + 1 >= per_limit:
            raise limit("per-member limit reached", {"reason": "per_member_limit"}) from exc
        raise AppError(ErrorCode.CONFLICT, "concurrent claim, please retry", retry_after=1) from exc
    if ref_key:
        db.add(CouponIssueLog(ref_key=ref_key, template_id=t.id, user_id=user_id, channel=channel,
                              source_ref=source_ref, user_coupon_id=coupon.id, created_at=now))
        try:
            db.flush()
        except IntegrityError as exc:
            raise AppError(ErrorCode.CONFLICT, "duplicate issue request in progress", retry_after=1) from exc
    enqueue_event(db, "coupon.claimed", {"user_coupon_id": coupon.id, "user_id": user_id, "template_id": t.id,
                                         "channel": channel, "source_ref": source_ref,
                                         "valid_to": valid_to.isoformat()}, "user_coupon", coupon.id)
    return coupon, False


def try_issue(db: Session, template_id: int, user_id: int, channel: str, ref_key: str, *,
              source_ref: str | None = None, enforce_limits: bool = True,
              now: datetime | None = None) -> tuple[UserCoupon | None, str | None]:
    """事件消费者用：业务原因发不出（售罄、限领、人群、未上架）返回 (None, 原因)，且此时没有任何写入；
    并发冲突（唯一约束）仍然抛出，由调用方回滚重试。"""
    try:
        coupon, _ = _issue(db, template_id, user_id, channel, _now(now), ref_key=ref_key, source_ref=source_ref,
                           enforce_limits=enforce_limits)
    except AppError as exc:
        if isinstance(exc.__cause__, IntegrityError):
            raise
        reason = exc.data.get("reason") if isinstance(exc.data, dict) else None
        return None, reason or exc.message
    return coupon, None


def claim(db: Session, user_id: int, template_id: int, now: datetime | None = None) -> UserCoupon:
    """会员在领券中心领取。不 commit（接口层 run_idempotent 提交）。"""
    coupon, _ = _issue(db, template_id, user_id, CHANNEL_CLAIM_CENTER, _now(now))
    return coupon


def issue(db: Session, template_id: int, user_id: int, ref_key: str, *, channel: str = CHANNEL_EVENT,
          source_ref: str | None = None, enforce_limits: bool = True, now: datetime | None = None) -> UserCoupon:
    """供其他模块在自己的事务里发券（注册礼、抽奖奖品、补偿、生日礼）。ref_key 唯一：重复调用返回同一张券。"""
    if not ref_key or len(ref_key) > 128:
        raise R.bad("ref_key is required (<= 128 chars)")
    coupon, _ = _issue(db, template_id, user_id, channel, _now(now), ref_key=ref_key, source_ref=source_ref,
                       enforce_limits=enforce_limits)
    return coupon


# ---------------------------------------------------------------- 兑换码


def _code_window(now: datetime) -> int:
    return int(now.timestamp()) // get_config().code_window_seconds


def check_code_throttle(db: Session, user_id: int, now: datetime) -> None:
    cfg = get_config()
    failures = db.execute(select(CouponCodeAttempt.failures).where(
        CouponCodeAttempt.user_id == user_id, CouponCodeAttempt.window == _code_window(now))).scalar_one_or_none()
    if failures is not None and failures >= cfg.code_max_failures:
        retry = cfg.code_window_seconds - int(now.timestamp()) % cfg.code_window_seconds
        raise AppError(ErrorCode.RATE_LIMITED, "too many invalid codes, please retry later", retry_after=max(1, retry))


def record_code_failure(db: Session, user_id: int, now: datetime | None = None) -> int:
    """记一次兑换失败（调用方在业务事务回滚后单独提交）。返回本窗口失败次数。"""
    now = _now(now)
    window = _code_window(now)
    where = (CouponCodeAttempt.user_id == user_id, CouponCodeAttempt.window == window)
    rows = db.execute(update(CouponCodeAttempt).where(*where).values(failures=CouponCodeAttempt.failures + 1)).rowcount
    if rows == 0:
        db.add(CouponCodeAttempt(user_id=user_id, window=window, failures=1))
        try:
            db.flush()
        except IntegrityError:
            db.rollback()
            db.execute(update(CouponCodeAttempt).where(*where).values(failures=CouponCodeAttempt.failures + 1))
    return db.execute(select(CouponCodeAttempt.failures).where(*where)).scalar_one()


class InvalidCode(AppError):
    """兑换码不存在 / 已使用 / 已作废：统一提示，不泄露是哪一种（防枚举）。"""

    def __init__(self) -> None:
        super().__init__(ErrorCode.NOT_FOUND, "invalid or used redeem code", {"reason": "code_invalid"})


def redeem_code(db: Session, user_id: int, raw_code: str, now: datetime | None = None) -> UserCoupon:
    now = _now(now)
    check_code_throttle(db, user_id, now)
    code = R.normalize_code(raw_code)
    if not R.CODE_PATTERN.match(code):
        raise InvalidCode()
    row = db.execute(select(CouponCode).where(CouponCode.code_hash == _cipher().lookup_hash(code))).scalar_one_or_none()
    if row is None or row.status != CODE_UNUSED:
        raise InvalidCode()
    taken = db.execute(update(CouponCode).where(CouponCode.id == row.id, CouponCode.status == CODE_UNUSED)
                       .values(status=CODE_USED, used_by_user_id=user_id, used_at=now)).rowcount
    if taken != 1:
        raise InvalidCode()
    coupon, _ = _issue(db, row.template_id, user_id, CHANNEL_CODE, now, ref_key=f"code:{row.id}",
                       source_ref=f"code_batch:{row.batch_id}")
    db.execute(update(CouponCode).where(CouponCode.id == row.id).values(user_coupon_id=coupon.id))
    return coupon


def create_code_batch(db: Session, staff_id: int, template_id: int, count: int, name: str,
                      now: datetime | None = None) -> CouponCodeBatch:
    now = _now(now)
    cfg = get_config()
    t = get_template(db, template_id)
    if t.status == TEMPLATE_ENDED:
        raise rule("template has ended")
    if CHANNEL_CODE not in R.loads(t.channels_json, []):
        raise rule("template does not allow the code channel")
    if not 1 <= count <= cfg.code_batch_max:
        raise R.bad(f"count must be 1-{cfg.code_batch_max}")
    if t.total_stock is not None:
        unredeemed = db.execute(select(func.count()).select_from(CouponCode).where(
            CouponCode.template_id == t.id, CouponCode.status == CODE_UNUSED)).scalar_one()
        if count + unredeemed > t.total_stock - t.issued_count:
            raise limit("not enough stock for these codes", {"available": t.total_stock - t.issued_count - unredeemed})
    batch = CouponCodeBatch(template_id=t.id, name=(name or "").strip()[:64] or f"batch-{now:%Y%m%d%H%M}",
                            count=count, created_by=staff_id, created_at=now, updated_at=now)
    db.add(batch)
    db.flush()
    cipher = _cipher()
    codes: set[str] = set()
    while len(codes) < count:
        codes.add(R.generate_code(cfg.code_length))
    db.add_all([CouponCode(batch_id=batch.id, template_id=t.id, code_hash=cipher.lookup_hash(code),
                           code_enc=cipher.encrypt(code), status=CODE_UNUSED, created_at=now) for code in codes])
    db.flush()
    write_audit(db, "staff", staff_id, "coupon.codes.create", "coupon_code_batch", batch.id,
                after={"template_id": t.id, "count": count})
    return batch


def export_code_rows(db: Session, staff_id: int, batch_id: int, now: datetime | None = None) -> list[list[Any]]:
    """导出兑换码明文（敏感：权限 coupon:code + 审计）。"""
    now = _now(now)
    batch = db.get(CouponCodeBatch, batch_id)
    if batch is None:
        raise AppError(ErrorCode.NOT_FOUND, "code batch not found")
    cipher = _cipher()
    rows = db.execute(select(CouponCode).where(CouponCode.batch_id == batch.id).order_by(CouponCode.id)).scalars().all()
    db.execute(update(CouponCodeBatch).where(CouponCodeBatch.id == batch.id).values(exported_at=now))
    write_audit(db, "staff", staff_id, "coupon.codes.export", "coupon_code_batch", batch.id, after={"rows": len(rows)})
    return [[cipher.decrypt(r.code_enc), r.status, r.used_by_user_id, aware(r.used_at)] for r in rows]


def void_code_batch(db: Session, staff_id: int, batch_id: int) -> int:
    batch = db.get(CouponCodeBatch, batch_id)
    if batch is None:
        raise AppError(ErrorCode.NOT_FOUND, "code batch not found")
    voided = db.execute(update(CouponCode).where(CouponCode.batch_id == batch.id, CouponCode.status == CODE_UNUSED)
                        .values(status=CODE_VOID)).rowcount
    write_audit(db, "staff", staff_id, "coupon.codes.void", "coupon_code_batch", batch.id, after={"voided": voided})
    return voided


# ---------------------------------------------------------------- 后台批量发放


def create_issue_batch(db: Session, staff_id: int, template_id: int, user_ids: list[int], reason: str,
                       channel: str = CHANNEL_ADMIN, ignore_limit: bool = False,
                       now: datetime | None = None) -> CouponIssueBatch:
    """创建批量发放任务；由 jobs.process_issue_batches 逐个会员发放，(batch, member) 幂等。"""
    now = _now(now)
    cfg = get_config()
    if channel not in (CHANNEL_ADMIN, CHANNEL_COMPENSATION):
        raise R.bad("channel must be admin or compensation")
    reason = (reason or "").strip()
    if not 2 <= len(reason) <= 200:
        raise R.bad("reason is required (2-200 chars)")
    members = list(dict.fromkeys(int(u) for u in user_ids if int(u) > 0))
    if not 1 <= len(members) <= cfg.issue_batch_max:
        raise R.bad(f"user_ids must contain 1-{cfg.issue_batch_max} members")
    t = get_template(db, template_id)
    if t.status != TEMPLATE_ACTIVE:
        raise rule("template is not active")
    if channel not in R.loads(t.channels_json, []):
        raise rule("template does not allow this channel")
    if t.total_stock is not None and t.total_stock - t.issued_count < len(members):
        raise limit("not enough stock for this batch", {"available": t.total_stock - t.issued_count})
    batch = CouponIssueBatch(template_id=t.id, channel=channel, reason=reason, ignore_limit=ignore_limit,
                             total=len(members), status=BATCH_PENDING, created_by=staff_id, created_at=now, updated_at=now)
    db.add(batch)
    db.flush()
    db.add_all([CouponIssueBatchItem(batch_id=batch.id, user_id=u, status=ITEM_PENDING) for u in members])
    db.flush()
    write_audit(db, "staff", staff_id, "coupon.issue_batch.create", "coupon_issue_batch", batch.id,
                after={"template_id": t.id, "members": len(members), "channel": channel, "ignore_limit": ignore_limit},
                reason=reason)
    return batch


def issue_batch_item(db: Session, batch: CouponIssueBatch, user_id: int, now: datetime) -> UserCoupon:
    coupon, _ = _issue(db, batch.template_id, user_id, batch.channel, now, ref_key=f"batch:{batch.id}:{user_id}",
                       source_ref=f"batch:{batch.id}", enforce_limits=not batch.ignore_limit)
    return coupon


# ================================================================ 订单使用


def coupon_rules(c: UserCoupon) -> dict[str, Any]:
    return json.loads(c.rules_json)


def display_status(c: UserCoupon, now: datetime) -> str:
    if c.status == STATUS_UNUSED:
        if now >= aware(c.valid_to):
            return STATUS_EXPIRED
        if now < aware(c.valid_from):
            return "not_started"
    return c.status


def coupon_view(c: UserCoupon, now: datetime | None = None) -> dict[str, Any]:
    now = _now(now)
    rules = coupon_rules(c)
    return {
        "id": c.id, "template_id": c.template_id, "name": c.name, "status": c.status,
        "display_status": display_status(c, now), "rules": rules, "rules_text": R.describe(rules),
        "valid_from": aware(c.valid_from), "valid_to": aware(c.valid_to), "channel": c.channel,
        "claimed_at": aware(c.claimed_at), "used_at": aware(c.used_at), "order_id": c.order_id,
        "discount_cents": c.discount_cents, "stack_with_member": rules.get("stack_with_member", True),
    }


def _state_rejection(c: UserCoupon, now: datetime, order_id: int | None = None) -> R.Rejection | None:
    if c.status == STATUS_LOCKED and order_id is not None and c.order_id == order_id:
        pass  # 同一订单重复锁定
    elif c.status != STATUS_UNUSED:
        return R.Rejection("unavailable", {"status": c.status})
    if now < aware(c.valid_from):
        return R.Rejection("not_started", {"valid_from": aware(c.valid_from).isoformat()})
    if now >= aware(c.valid_to):
        return R.Rejection("expired")
    return None


@dataclass
class CouponPricing:
    """券抵扣结果：每张券的抵扣额与在订单行上的分摊（订单写入 order_items.coupon_alloc_cents，售后按行退款）。"""

    evaluation: R.Evaluation
    coupons: list[dict[str, Any]] = field(default_factory=list)  # [{user_coupon_id, template_id, discount_cents, lines}]

    @property
    def coupon_cents(self) -> int:
        return sum(c["discount_cents"] for c in self.coupons)

    @property
    def line_coupon_cents(self) -> list[int]:
        return self.evaluation.line_coupon_cents

    def to_dict(self) -> dict[str, Any]:
        return {"coupon_cents": self.coupon_cents, "line_coupon_cents": self.line_coupon_cents,
                "coupons": self.coupons, "subtotal_cents": self.evaluation.quote.subtotal_cents,
                "payable_cents": self.evaluation.quote.payable_cents}


def _key(coupon_id: int) -> str:
    return f"coupon:{coupon_id}"


def available_coupons(db: Session, user_id: int, lines: list[R.CartLine], campaign_promotions: list = (),
                      shipping_cents: int = 0, now: datetime | None = None) -> dict[str, Any]:
    """下单页：会员的券分为可用（含抵扣额，按抵扣额降序）与不可用（含原因，如“还差 ¥X”）；best 为推荐的一张。"""
    now = _now(now)
    R.validate_lines(lines)
    coupons = db.execute(select(UserCoupon).where(UserCoupon.user_id == user_id, UserCoupon.status == STATUS_UNUSED)
                         .order_by(UserCoupon.valid_to, UserCoupon.id).limit(200)).scalars().all()
    usable, unusable = [], []
    for c in coupons:
        view = coupon_view(c, now)
        rejection = _state_rejection(c, now)
        if rejection is None:
            evaluation = R.evaluate([(_key(c.id), coupon_rules(c))], lines, campaign_promotions, shipping_cents)
            rejection = evaluation.rejected.get(_key(c.id))
            if rejection is None:
                view["discount_cents"] = evaluation.discounts[_key(c.id)]["amount_cents"]
                usable.append(view)
                continue
        unusable.append({**view, "unusable": rejection.to_dict()})
    usable.sort(key=lambda v: (-v["discount_cents"], v["valid_to"], v["id"]))
    return {"usable": usable, "unusable": unusable, "best": usable[0]["id"] if usable else None}


def best_coupon(db: Session, user_id: int, lines: list[R.CartLine], campaign_promotions: list = (),
                shipping_cents: int = 0, now: datetime | None = None) -> dict[str, Any] | None:
    """推荐抵扣最大的一张（同额取先到期的）。"""
    result = available_coupons(db, user_id, lines, campaign_promotions, shipping_cents, now)
    return result["usable"][0] if result["usable"] else None


def price_with_coupons(db: Session, user_id: int, coupon_ids: list[int], lines: list[R.CartLine],
                       campaign_promotions: list = (), shipping_cents: int = 0, now: datetime | None = None,
                       order_id: int | None = None) -> CouponPricing:
    """用指定的券计算抵扣（报价与下单同一函数，保证金额一致）。任何一张不可用 → 4220（带原因），不静默丢券。"""
    now = _now(now)
    ids = list(dict.fromkeys(int(i) for i in coupon_ids))
    if len(ids) > get_config().max_coupons_per_order:
        raise rule("too many coupons for one order")
    if not ids:
        return CouponPricing(R.evaluate([], lines, campaign_promotions, shipping_cents), [])
    rows = db.execute(select(UserCoupon).where(UserCoupon.id.in_(ids))).scalars().all()
    found = {c.id: c for c in rows if c.user_id == user_id}
    if len(found) != len(ids):
        raise AppError(ErrorCode.NOT_FOUND, "coupon not found")
    coupons = [found[i] for i in sorted(ids)]
    for c in coupons:
        rejection = _state_rejection(c, now, order_id)
        if rejection:
            raise rule("coupon cannot be used", {"user_coupon_id": c.id, **rejection.to_dict()})
    if R.stack_conflicts([coupon_rules(c).get("stack_group") or "" for c in coupons]):
        raise rule("these coupons cannot be used together", R.Rejection("stack_conflict").to_dict())
    evaluation = R.evaluate([(_key(c.id), coupon_rules(c)) for c in coupons], lines, campaign_promotions, shipping_cents)
    for c in coupons:
        rejection = evaluation.rejected.get(_key(c.id))
        if rejection:
            raise rule("coupon cannot be used", {"user_coupon_id": c.id, **rejection.to_dict()})
    result = [{"user_coupon_id": c.id, "template_id": c.template_id,
               "discount_cents": evaluation.discounts[_key(c.id)]["amount_cents"],
               "lines": evaluation.discounts[_key(c.id)]["lines"]} for c in coupons]
    return CouponPricing(evaluation, result)


def lock(db: Session, user_id: int, order_id: int, coupon_ids: list[int], lines: list[R.CartLine],
         campaign_promotions: list = (), shipping_cents: int = 0, now: datetime | None = None) -> CouponPricing:
    """下单事务内锁券：unused → locked（条件 UPDATE，带有效期条件）。同券并发用于两单只有一单成功（4090）。
    同一订单重复调用幂等。不 commit。"""
    now = _now(now)
    pricing_result = price_with_coupons(db, user_id, coupon_ids, lines, campaign_promotions, shipping_cents, now,
                                        order_id=order_id)
    for item in pricing_result.coupons:
        values = dict(order_id=order_id, discount_cents=item["discount_cents"], alloc_json=R.dumps(item["lines"]),
                      locked_at=now, updated_at=now)
        rows = db.execute(update(UserCoupon).where(
            UserCoupon.id == item["user_coupon_id"], UserCoupon.user_id == user_id,
            or_(UserCoupon.status == STATUS_UNUSED,
                (UserCoupon.status == STATUS_LOCKED) & (UserCoupon.order_id == order_id)),
            UserCoupon.valid_from <= now, UserCoupon.valid_to > now,
        ).values(status=STATUS_LOCKED, **values)).rowcount
        if rows != 1:
            raise AppError(ErrorCode.CONFLICT, "coupon is no longer available", {"user_coupon_id": item["user_coupon_id"]})
        enqueue_event(db, "coupon.locked", {"user_coupon_id": item["user_coupon_id"], "user_id": user_id,
                                            "order_id": order_id, "discount_cents": item["discount_cents"]},
                      "user_coupon", item["user_coupon_id"])
    return pricing_result


def _order_coupons(db: Session, order_id: int, status: str) -> list[UserCoupon]:
    return db.execute(select(UserCoupon).where(UserCoupon.order_id == order_id, UserCoupon.status == status)
                      .order_by(UserCoupon.id)).scalars().all()


def _bump_used(db: Session, counts: dict[int, int]) -> None:
    for template_id, n in sorted(counts.items()):
        if n > 0:
            db.execute(update(CouponTemplate).where(CouponTemplate.id == template_id)
                       .values(used_count=CouponTemplate.used_count + n))
        elif n < 0:
            db.execute(update(CouponTemplate).where(CouponTemplate.id == template_id,
                                                    CouponTemplate.used_count >= -n)
                       .values(used_count=CouponTemplate.used_count + n))


def redeem(db: Session, order_id: int, now: datetime | None = None) -> list[int]:
    """支付成功：本订单锁定的券 locked → used（锁定时已校验有效期，支付时不再因到期拒绝）。重复调用幂等。不 commit。"""
    now = _now(now)
    redeemed: list[int] = []
    counts: dict[int, int] = {}
    for c in _order_coupons(db, order_id, STATUS_LOCKED):
        rows = db.execute(update(UserCoupon).where(
            UserCoupon.id == c.id, UserCoupon.status == STATUS_LOCKED, UserCoupon.order_id == order_id,
        ).values(status=STATUS_USED, used_at=now, updated_at=now)).rowcount
        if rows == 1:
            redeemed.append(c.id)
            counts[c.template_id] = counts.get(c.template_id, 0) + 1
            enqueue_event(db, "coupon.redeemed", {"user_coupon_id": c.id, "user_id": c.user_id, "order_id": order_id,
                                                  "template_id": c.template_id, "discount_cents": c.discount_cents},
                          "user_coupon", c.id)
    _bump_used(db, counts)
    already = [c.id for c in _order_coupons(db, order_id, STATUS_USED)]
    return sorted(set(redeemed) | set(already))


_CLEAR_ORDER = dict(order_id=None, discount_cents=0, alloc_json=None, locked_at=None)


def unlock(db: Session, order_id: int, now: datetime | None = None) -> dict[str, list[int]]:
    """订单取消 / 超时关闭：locked → unused；此时券已过期则 locked → expired（不再可用，修复 002Gym 的问题）。
    不是本订单锁定的券不受影响；重复调用幂等。不 commit。"""
    now = _now(now)
    result: dict[str, list[int]] = {"unlocked": [], "expired": []}
    for c in _order_coupons(db, order_id, STATUS_LOCKED):
        expired = now >= aware(c.valid_to)
        target = STATUS_EXPIRED if expired else STATUS_UNUSED
        extra = {"expired_at": now} if expired else {}
        rows = db.execute(update(UserCoupon).where(
            UserCoupon.id == c.id, UserCoupon.status == STATUS_LOCKED, UserCoupon.order_id == order_id,
        ).values(status=target, updated_at=now, **_CLEAR_ORDER, **extra)).rowcount
        if rows != 1:
            continue
        if expired:
            result["expired"].append(c.id)
            enqueue_event(db, "coupon.expired", {"user_coupon_id": c.id, "user_id": c.user_id, "order_id": order_id,
                                                 "reason": "expired_before_unlock"}, "user_coupon", c.id)
        else:
            result["unlocked"].append(c.id)
            enqueue_event(db, "coupon.unlocked", {"user_coupon_id": c.id, "user_id": c.user_id, "order_id": order_id},
                          "user_coupon", c.id)
    return result


def return_on_refund(db: Session, order_id: int, full_refund: bool, now: datetime | None = None,
                     policy: str | None = None) -> dict[str, Any]:
    """退款后按策略退券（七日办法第十五条：券按原方式退回，另有约定除外）。

    - policy（默认取经营设置 coupon.return_policy）：full_refund 只在整单全额退款时退；any_refund 任何退款都退；never 不退。
    - 经营设置 coupon.return_window_hours > 0 时，核销超过该小时数后不退。
    - 退回时券仍在有效期 → unused；已过有效期 → expired（不再可用）。
    - 退的是券本身；退款资金一律原路退回，不得折成券（微信小程序运营规范 §2.14 第 6 条），本模块不提供“退款转券”。
    不 commit；重复调用幂等。
    """
    now = _now(now)
    cfg = get_config()
    policy = policy or business_value(db, "coupon.return_policy")
    window_hours = business_value(db, "coupon.return_window_hours")
    used = _order_coupons(db, order_id, STATUS_USED)
    result: dict[str, Any] = {"returned": [], "expired": [], "kept": []}
    if not used:
        return result
    if policy == "never" or (policy == "full_refund" and not full_refund):
        result["kept"] = [{"user_coupon_id": c.id, "reason": "policy" if policy == "never" else "partial_refund"}
                          for c in used]
        return result
    counts: dict[int, int] = {}
    for c in used:
        if window_hours and c.used_at and now - aware(c.used_at) > timedelta(hours=window_hours):
            result["kept"].append({"user_coupon_id": c.id, "reason": "return_window_passed"})
            continue
        expired = now >= aware(c.valid_to)
        target = STATUS_EXPIRED if expired else STATUS_UNUSED
        extra = {"expired_at": now} if expired else {}
        rows = db.execute(update(UserCoupon).where(
            UserCoupon.id == c.id, UserCoupon.status == STATUS_USED, UserCoupon.order_id == order_id,
        ).values(status=target, returned_at=now, used_at=None, updated_at=now, **_CLEAR_ORDER, **extra)).rowcount
        if rows != 1:
            continue
        counts[c.template_id] = counts.get(c.template_id, 0) - 1
        result["expired" if expired else "returned"].append(c.id)
        enqueue_event(db, "coupon.returned", {"user_coupon_id": c.id, "user_id": c.user_id, "order_id": order_id,
                                              "status": target, "full_refund": full_refund}, "user_coupon", c.id)
    _bump_used(db, counts)
    return result


def revoke_by_source(db: Session, source_ref: str, reason: str, now: datetime | None = None) -> dict[str, Any]:
    """来源（例如赠券的订单、抽奖记录）退款或作废时收回发出的券：未使用的 → revoked；已锁定 / 已使用的保留并报告，
    由调用方决定是否阻止来源退款。不 commit。"""
    now = _now(now)
    ids = db.execute(select(CouponIssueLog.user_coupon_id).where(CouponIssueLog.source_ref == source_ref)).scalars().all()
    result: dict[str, Any] = {"revoked": [], "kept": []}
    if not ids:
        return result
    coupons = db.execute(select(UserCoupon).where(UserCoupon.id.in_(ids)).order_by(UserCoupon.id)).scalars().all()
    for c in coupons:
        rows = db.execute(update(UserCoupon).where(UserCoupon.id == c.id, UserCoupon.status == STATUS_UNUSED)
                          .values(status=STATUS_REVOKED, revoked_at=now, revoke_reason=reason[:200],
                                  updated_at=now)).rowcount
        if rows == 1:
            result["revoked"].append(c.id)
            enqueue_event(db, "coupon.revoked", {"user_coupon_id": c.id, "user_id": c.user_id,
                                                 "source_ref": source_ref}, "user_coupon", c.id)
        else:
            db.refresh(c)
            result["kept"].append({"user_coupon_id": c.id, "status": c.status})
    return result


def revoke(db: Session, staff_id: int, coupon_id: int, reason: str, now: datetime | None = None) -> UserCoupon:
    """后台作废一张未使用的券（误发等），原因必填并审计。"""
    now = _now(now)
    reason = (reason or "").strip()
    if not 2 <= len(reason) <= 200:
        raise R.bad("reason is required (2-200 chars)")
    c = db.get(UserCoupon, coupon_id)
    if c is None:
        raise AppError(ErrorCode.NOT_FOUND, "coupon not found")
    COUPON_FLOW.advance(db, UserCoupon, c.id, STATUS_UNUSED, STATUS_REVOKED, revoked_at=now, revoke_reason=reason,
                        updated_at=now)
    db.refresh(c)
    write_audit(db, "staff", staff_id, "coupon.revoke", "user_coupon", c.id, before={"status": STATUS_UNUSED},
                after={"status": STATUS_REVOKED}, reason=reason)
    enqueue_event(db, "coupon.revoked", {"user_coupon_id": c.id, "user_id": c.user_id}, "user_coupon", c.id)
    return c


# ================================================================ 统计


def template_stats(db: Session, template_id: int) -> dict[str, Any]:
    t = get_template(db, template_id)
    by_status = dict(db.execute(select(UserCoupon.status, func.count()).where(UserCoupon.template_id == t.id)
                                .group_by(UserCoupon.status)).all())
    by_channel = dict(db.execute(select(UserCoupon.channel, func.count()).where(UserCoupon.template_id == t.id)
                                 .group_by(UserCoupon.channel)).all())
    used_amount, order_count = db.execute(select(func.coalesce(func.sum(UserCoupon.discount_cents), 0),
                                                 func.count(func.distinct(UserCoupon.order_id)))
                                          .where(UserCoupon.template_id == t.id, UserCoupon.status == STATUS_USED)).one()
    issued = sum(by_status.values())
    used = by_status.get(STATUS_USED, 0)
    return {
        "template_id": t.id, "name": t.name, "issued": issued, "issued_counter": t.issued_count,
        "used": used, "used_counter": t.used_count, "used_amount_cents": int(used_amount),
        "orders": int(order_count), "conversion_bp": used * 10000 // issued if issued else 0,
        "by_status": {s: by_status.get(s, 0) for s in COUPON_STATUSES}, "by_channel": by_channel,
        "remaining_stock": None if t.total_stock is None else t.total_stock - t.issued_count,
    }


# ================================================================ 列表声明（ListSpec）


TEMPLATE_SPEC = ListSpec(
    fields={
        "status": Field(CouponTemplate.status, ops=("in",), choices=frozenset(TEMPLATE_STATUSES)),
        "type": Field(CouponTemplate.type, ops=("in",), choices=frozenset(TEMPLATE_TYPES)),
        "store_id": Field(CouponTemplate.store_id, "int", ops=("eq", "null")),
        "created_at": Field(CouponTemplate.created_at, "datetime", ops=("range",), sortable=True),
        "published_at": Field(CouponTemplate.published_at, "datetime", ops=("range",), sortable=True),
        "issued_count": Field(CouponTemplate.issued_count, "int", ops=("range",), sortable=True),
        "used_count": Field(CouponTemplate.used_count, "int", ops=("range",), sortable=True),
    },
    id_column=CouponTemplate.id,
    keyword_columns=(CouponTemplate.name,),
)

CLAIM_CENTER_SPEC = ListSpec(
    fields={
        "type": Field(CouponTemplate.type, ops=("in",), choices=frozenset(TEMPLATE_TYPES)),
        "store_id": Field(CouponTemplate.store_id, "int"),
        "published_at": Field(CouponTemplate.published_at, "datetime", sortable=True),
    },
    id_column=CouponTemplate.id,
    default_sort="-published_at",
)

MEMBER_COUPON_SPEC = ListSpec(
    fields={
        "status": Field(UserCoupon.status, ops=("in",), choices=frozenset(COUPON_STATUSES)),
        "template_id": Field(UserCoupon.template_id, "int", ops=("in",)),
        "user_id": Field(UserCoupon.user_id, "int"),
        "channel": Field(UserCoupon.channel, ops=("in",), choices=frozenset(CHANNELS)),
        "order_id": Field(UserCoupon.order_id, "int"),
        "claimed_at": Field(UserCoupon.claimed_at, "datetime", ops=("range",), sortable=True),
        "used_at": Field(UserCoupon.used_at, "datetime", ops=("range",), sortable=True),
        "valid_to": Field(UserCoupon.valid_to, "datetime", ops=("range",), sortable=True),
        "discount_cents": Field(UserCoupon.discount_cents, "int", ops=("range",), sortable=True),
    },
    id_column=UserCoupon.id,
    keyword_columns=(UserCoupon.name, cast(UserCoupon.user_id, String), UserCoupon.source_ref),
    default_sort="-claimed_at",
)

_PHONE = re.compile(r"^1\d{10}$")


def looks_like_phone(keyword: str | None) -> bool:
    return bool(keyword and _PHONE.match(keyword.strip()))


def user_id_by_phone(db: Session, phone: str) -> int | None:
    """手机号 → 会员 id：01-identity 的 find_user_by_phone（哈希查找）；identity 未装配时返回 None。"""
    try:
        from identity import find_user_by_phone  # type: ignore[attr-defined]  # noqa: PLC0415
    except ImportError:
        return None
    user = find_user_by_phone(db, phone.strip())
    return int(user.id) if user is not None else None


MY_COUPON_SPEC = ListSpec(
    fields={
        "template_id": Field(UserCoupon.template_id, "int"),
        "claimed_at": Field(UserCoupon.claimed_at, "datetime", sortable=True),
        "valid_to": Field(UserCoupon.valid_to, "datetime", sortable=True),
    },
    id_column=UserCoupon.id,
    default_sort="-claimed_at",
    extra_params=frozenset({"tab"}),
)

ISSUE_BATCH_SPEC = ListSpec(
    fields={
        "status": Field(CouponIssueBatch.status, ops=("in",), choices=frozenset({BATCH_PENDING, BATCH_DONE})),
        "template_id": Field(CouponIssueBatch.template_id, "int"),
        "created_at": Field(CouponIssueBatch.created_at, "datetime", ops=("range",), sortable=True),
    },
    id_column=CouponIssueBatch.id,
    keyword_columns=(CouponIssueBatch.reason,),
)

CODE_BATCH_SPEC = ListSpec(
    fields={
        "template_id": Field(CouponCodeBatch.template_id, "int"),
        "created_at": Field(CouponCodeBatch.created_at, "datetime", ops=("range",), sortable=True),
    },
    id_column=CouponCodeBatch.id,
    keyword_columns=(CouponCodeBatch.name,),
)

TABS = ("available", "used", "expired")


def my_coupons_stmt(user_id: int, tab: str, now: datetime):
    stmt = select(UserCoupon).where(UserCoupon.user_id == user_id)
    if tab == "available":
        return stmt.where(UserCoupon.status.in_((STATUS_UNUSED, STATUS_LOCKED)), UserCoupon.valid_to > now)
    if tab == "used":
        return stmt.where(UserCoupon.status == STATUS_USED)
    if tab == "expired":
        return stmt.where(or_(UserCoupon.status == STATUS_EXPIRED,
                              (UserCoupon.status == STATUS_UNUSED) & (UserCoupon.valid_to <= now)))
    raise R.bad(f"tab must be one of {', '.join(TABS)}")


def my_coupon_counts(db: Session, user_id: int, now: datetime | None = None) -> dict[str, int]:
    now = _now(now)
    available = (UserCoupon.status.in_((STATUS_UNUSED, STATUS_LOCKED))) & (UserCoupon.valid_to > now)
    expired = (UserCoupon.status == STATUS_EXPIRED) | ((UserCoupon.status == STATUS_UNUSED) & (UserCoupon.valid_to <= now))
    row = db.execute(select(
        func.coalesce(func.sum(case((available, 1), else_=0)), 0),
        func.coalesce(func.sum(case((UserCoupon.status == STATUS_USED, 1), else_=0)), 0),
        func.coalesce(func.sum(case((expired, 1), else_=0)), 0),
    ).where(UserCoupon.user_id == user_id)).one()
    return {"available": int(row[0]), "used": int(row[1]), "expired": int(row[2])}


def claim_center_stmt(now: datetime):
    return select(CouponTemplate).where(
        CouponTemplate.status == TEMPLATE_ACTIVE, CouponTemplate.deleted_at.is_(None),
        CouponTemplate.channels_json.like(f'%"{CHANNEL_CLAIM_CENTER}"%'),
        or_(CouponTemplate.claim_start_at.is_(None), CouponTemplate.claim_start_at <= now),
        or_(CouponTemplate.claim_end_at.is_(None), CouponTemplate.claim_end_at > now),
    )


def claimed_counts(db: Session, user_id: int, template_ids: list[int]) -> dict[int, int]:
    if not template_ids:
        return {}
    return dict(db.execute(select(UserCoupon.template_id, func.count()).where(
        UserCoupon.user_id == user_id, UserCoupon.template_id.in_(template_ids)).group_by(UserCoupon.template_id)).all())
