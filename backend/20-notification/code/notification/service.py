"""notification 用例：模板注册、notify 入队与受理、订阅额度、投递 worker、站内信、后台日志与统计。

事务约定：
- notify / notify_staff 只写发件箱事件，不提交（与业务写同事务）。
- 后台写用例（场景 / 模板 CRUD、重发、上报订阅）不提交，由 API 层 run_idempotent 提交。
- 受理（accept_request）、投递（dispatch_due / process_delivery）、推送事件（handle_push_event）、任务自己提交：
  外部调用前先提交占位（sending + 额度占用），调用后在新事务里写结果，事务中不做外部 HTTP（SPEC §6.10）。
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
from collections.abc import Mapping
from datetime import date, datetime, timedelta, timezone
from typing import Any

from sqlalchemy import case, delete, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from luzback import AppError, ErrorCode, Field, ListSpec, enqueue_event, write_audit
from luzback.audit import diff
from luzback.export import export_csv
from luzback.resilience import BreakerRegistry, CircuitOpenError, TransientError, UpstreamError, resilient

from . import channels, rules
from .config import get_config, now as clock_now
from .defaults import DEFAULT_SCENES
from .models import (
    AUDIENCE_MEMBER, AUDIENCE_STAFF, AUDIENCES, CATEGORIES, CHANNEL_INAPP, CHANNEL_SMS, CHANNEL_SUBSCRIBE, CHANNELS,
    DELIVERY_SM, DELIVERY_STATUSES, SMS_MODES, ST_FAILED, ST_PENDING, ST_SENDING, ST_SENT, ST_SKIPPED, SUB_ACTIVE,
    SUB_BANNED, SUB_REJECTED, NotificationCounter, NotificationDelivery, NotificationInbox, NotificationRequest,
    NotificationScene, NotificationSubscribeEvent, NotificationSubscription, NotificationTemplate,
)

log = logging.getLogger("luz.notification")

TOPIC_REQUESTED = "notification.requested"
breakers = BreakerRegistry()

_SCENE_RE = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z0-9_]+){1,3}$")
_PAGE_RE = re.compile(r"^[A-Za-z0-9_\-/]+(\?[A-Za-z0-9_\-=&%.{}]*)?$")
_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_\-:.]{0,127}$")


def _aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _bad(message: str, data: Any = None) -> AppError:
    return AppError(ErrorCode.BAD_REQUEST, message, data)


# ======================================================================== 业务数据编码（保留 datetime / date 类型）


def encode_data(data: Mapping[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in data.items():
        if isinstance(value, datetime):
            out[key] = {"__dt__": _aware(value).isoformat()}
        elif isinstance(value, date):
            out[key] = {"__d__": value.isoformat()}
        elif value is None or isinstance(value, (str, int, float, bool)):
            out[key] = value
        else:
            out[key] = str(value)
    return out


def decode_data(data: Mapping[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in data.items():
        if isinstance(value, dict) and "__dt__" in value:
            out[key] = datetime.fromisoformat(value["__dt__"])
        elif isinstance(value, dict) and "__d__" in value:
            out[key] = date.fromisoformat(value["__d__"])
        else:
            out[key] = value
    return out


def _with_derived(data: dict[str, Any], tz: str) -> dict[str, Any]:
    """派生常用展示字段：amount_cents → amount_yuan；datetime → *_text（门店时区）。"""
    out = dict(data)
    for key, value in data.items():
        if key.endswith("_cents") and isinstance(value, int) and not isinstance(value, bool):
            out.setdefault(key[:-6] + "_yuan", f"{value // 100}.{value % 100:02d}")
        if isinstance(value, datetime):
            local = rules._local(value, tz)
            out.setdefault(key + "_text", f"{local.month}月{local.day}日 {local:%H:%M}")
        if isinstance(value, date) and not isinstance(value, datetime):
            out.setdefault(key + "_text", f"{value.month}月{value.day}日")
    return out


# ======================================================================== 模板注册（后台）


def _scene_or_404(db: Session, scene: str) -> NotificationScene:
    row = db.execute(select(NotificationScene).where(NotificationScene.scene == scene)).scalar_one_or_none()
    if row is None:
        raise AppError(ErrorCode.NOT_FOUND, "scene not found")
    return row


def _templates(db: Session, scene: str) -> dict[str, NotificationTemplate]:
    rows = db.execute(select(NotificationTemplate).where(NotificationTemplate.scene == scene)).scalars().all()
    return {t.channel: t for t in rows}


def template_to_dict(t: NotificationTemplate) -> dict[str, Any]:
    body: dict[str, Any] = {"channel": t.channel, "enabled": t.enabled, "page": t.page, "version": t.version,
                            "updated_at": _aware(t.updated_at)}
    if t.channel == CHANNEL_SUBSCRIBE:
        body.update(template_id=t.template_ref, long_term=t.long_term, keywords=json.loads(t.keywords_json or "[]"))
    elif t.channel == CHANNEL_SMS:
        body.update(template_code=t.template_ref, params=json.loads(t.keywords_json or "[]"))
    else:
        body.update(title_tpl=t.title_tpl, body_tpl=t.body_tpl)
    return body


def scene_to_dict(s: NotificationScene, templates: dict[str, NotificationTemplate] | None = None) -> dict[str, Any]:
    out = {"id": s.id, "scene": s.scene, "name": s.name, "category": s.category, "audience": s.audience,
           "urgent": s.urgent, "sms_mode": s.sms_mode, "enabled": s.enabled, "updated_at": _aware(s.updated_at)}
    if templates is not None:
        out["templates"] = {ch: template_to_dict(t) for ch, t in sorted(templates.items())}
    return out


SCENE_SPEC = ListSpec(
    fields={
        "category": Field(NotificationScene.category, "str", ("eq", "in"), choices=frozenset(CATEGORIES)),
        "audience": Field(NotificationScene.audience, "str", ("eq",), choices=frozenset(AUDIENCES)),
        "enabled": Field(NotificationScene.enabled, "bool"),
        "scene": Field(NotificationScene.scene, "str", ("eq",), sortable=True),
        "updated_at": Field(NotificationScene.updated_at, "datetime", ("range",), sortable=True),
    },
    id_column=NotificationScene.id,
    keyword_columns=(NotificationScene.scene, NotificationScene.name),
    default_sort="scene",
)


def list_scenes(db: Session, params: Mapping[str, Any]) -> dict[str, Any]:
    page = SCENE_SPEC.page(db, select(NotificationScene), params, serialize=lambda s: s)
    scenes = [s.scene for s in page["items"]]
    tpl_rows = db.execute(select(NotificationTemplate).where(NotificationTemplate.scene.in_(scenes))).scalars().all() if scenes else []
    grouped: dict[str, dict[str, NotificationTemplate]] = {}
    for t in tpl_rows:
        grouped.setdefault(t.scene, {})[t.channel] = t
    page["items"] = [scene_to_dict(s, grouped.get(s.scene, {})) for s in page["items"]]
    return page


def get_scene(db: Session, scene: str) -> dict[str, Any]:
    row = _scene_or_404(db, scene)
    return scene_to_dict(row, _templates(db, scene))


def _validate_scene_fields(body: Mapping[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    if "name" in body:
        name = str(body["name"] or "").strip()
        if not 1 <= len(name) <= 64:
            raise _bad("name must be 1-64 chars")
        out["name"] = name
    if "category" in body:
        if body["category"] not in CATEGORIES:
            raise _bad("unknown category")
        out["category"] = body["category"]
    if "audience" in body:
        if body["audience"] not in AUDIENCES:
            raise _bad("unknown audience")
        out["audience"] = body["audience"]
    if "sms_mode" in body:
        if body["sms_mode"] not in SMS_MODES:
            raise _bad("sms_mode must be off|fallback|always")
        out["sms_mode"] = body["sms_mode"]
    for flag in ("urgent", "enabled"):
        if flag in body:
            out[flag] = bool(body[flag])
    return out


def create_scene(db: Session, staff_id: int, body: Mapping[str, Any]) -> dict[str, Any]:
    scene = str(body.get("scene") or "")
    if not _SCENE_RE.match(scene) or len(scene) > 64:
        raise _bad("scene must look like order.paid (lowercase, dot separated)")
    fields = _validate_scene_fields(body)
    if "name" not in fields:
        raise _bad("name is required")
    if db.execute(select(NotificationScene.id).where(NotificationScene.scene == scene)).first():
        raise AppError(ErrorCode.CONFLICT, "scene already exists")
    row = NotificationScene(scene=scene, created_by=staff_id, updated_by=staff_id, **fields)
    db.add(row)
    try:
        db.flush()
    except IntegrityError as exc:
        raise AppError(ErrorCode.CONFLICT, "scene already exists") from exc
    after = scene_to_dict(row)
    write_audit(db, "staff", staff_id, "notification.scene.create", "notification_scene", row.id, None, after)
    return scene_to_dict(row, {})


def update_scene(db: Session, staff_id: int, scene: str, body: Mapping[str, Any]) -> dict[str, Any]:
    row = _scene_or_404(db, scene)
    fields = _validate_scene_fields(body)
    before = scene_to_dict(row)
    for key, value in fields.items():
        setattr(row, key, value)
    row.updated_by = staff_id
    db.flush()
    after = scene_to_dict(row)
    write_audit(db, "staff", staff_id, "notification.scene.update", "notification_scene", row.id, before, diff(before, after))
    return scene_to_dict(row, _templates(db, scene))


def delete_scene(db: Session, staff_id: int, scene: str) -> dict[str, Any]:
    """删除场景与其模板；历史投递记录保留（按场景名字符串关联）。"""
    row = _scene_or_404(db, scene)
    before = scene_to_dict(row, _templates(db, scene))
    db.execute(delete(NotificationTemplate).where(NotificationTemplate.scene == scene))
    db.delete(row)
    db.flush()
    write_audit(db, "staff", staff_id, "notification.scene.delete", "notification_scene", row.id, before, None)
    return {"scene": scene, "deleted": True}


def _validate_page(page: Any) -> str:
    page = str(page or "").strip()
    if not page:
        return ""
    if len(page) > 256 or "://" in page or page.startswith("/") or not _PAGE_RE.match(page):
        raise _bad("page must be a mini-program page path like pages/order/detail?id={order_id}")
    rules.placeholders(page)
    return page


def upsert_template(db: Session, staff_id: int, scene: str, channel: str, body: Mapping[str, Any]) -> dict[str, Any]:
    """新建或修改场景的渠道模板；保存前校验关键词类型与占位符，非法 4001。"""
    scene_row = _scene_or_404(db, scene)
    if channel not in CHANNELS:
        raise _bad("channel must be subscribe|sms|inapp")
    if channel == CHANNEL_SUBSCRIBE and scene_row.audience != AUDIENCE_MEMBER:
        raise _bad("subscribe messages can only target members")
    values: dict[str, Any] = {"enabled": bool(body.get("enabled", True))}
    try:
        values["page"] = _validate_page(body.get("page"))
        if channel == CHANNEL_SUBSCRIBE:
            template_id = str(body.get("template_id") or "").strip()
            if values["enabled"] and not template_id:
                raise _bad("template_id is required when enabled")
            if len(template_id) > 128:
                raise _bad("template_id too long")
            values["template_ref"] = template_id
            values["keywords_json"] = json.dumps(rules.validate_subscribe_mapping(body.get("keywords")), ensure_ascii=False)
            values["long_term"] = bool(body.get("long_term", False))
        elif channel == CHANNEL_SMS:
            code = str(body.get("template_code") or "").strip()
            if values["enabled"] and not code:
                raise _bad("template_code is required when enabled")
            if len(code) > 128:
                raise _bad("template_code too long")
            values["template_ref"] = code
            values["keywords_json"] = json.dumps(rules.validate_sms_params(body.get("params")), ensure_ascii=False)
        else:
            title, text = str(body.get("title_tpl") or "").strip(), str(body.get("body_tpl") or "").strip()
            if not title or not text or len(title) > 200 or len(text) > 1000:
                raise _bad("title_tpl (≤200) and body_tpl (≤1000) are required")
            rules.placeholders(title)
            rules.placeholders(text)
            values.update(title_tpl=title, body_tpl=text)
    except ValueError as exc:
        raise _bad(str(exc)) from exc
    existing = _templates(db, scene).get(channel)
    if existing is None:
        row = NotificationTemplate(scene=scene, channel=channel, created_by=staff_id, updated_by=staff_id, **values)
        db.add(row)
        try:
            db.flush()
        except IntegrityError as exc:
            raise AppError(ErrorCode.CONFLICT, "template changed concurrently, retry") from exc
        write_audit(db, "staff", staff_id, "notification.template.create", "notification_template", row.id, None,
                    template_to_dict(row))
        return template_to_dict(row)
    before = template_to_dict(existing)
    for key, value in values.items():
        setattr(existing, key, value)
    existing.version += 1
    existing.updated_by = staff_id
    db.flush()
    after = template_to_dict(existing)
    write_audit(db, "staff", staff_id, "notification.template.update", "notification_template", existing.id, before,
                diff(before, after))
    return after


def delete_template(db: Session, staff_id: int, scene: str, channel: str) -> dict[str, Any]:
    _scene_or_404(db, scene)
    row = _templates(db, scene).get(channel)
    if row is None:
        raise AppError(ErrorCode.NOT_FOUND, "template not found")
    before = template_to_dict(row)
    db.delete(row)
    db.flush()
    write_audit(db, "staff", staff_id, "notification.template.delete", "notification_template", row.id, before, None)
    return {"scene": scene, "channel": channel, "deleted": True}


def ensure_default_scenes(db: Session, scenes: list[dict[str, Any]] | None = None) -> int:
    """幂等种子：补齐缺失的场景与渠道模板；外部渠道模板 id 留空且停用。返回新增行数。"""
    added = 0
    for spec in scenes or DEFAULT_SCENES:
        row = db.execute(select(NotificationScene).where(NotificationScene.scene == spec["scene"])).scalar_one_or_none()
        if row is None:
            row = NotificationScene(scene=spec["scene"], name=spec["name"], category=spec["category"],
                                    audience=spec["audience"], urgent=spec["urgent"], sms_mode=spec["sms_mode"])
            db.add(row)
            added += 1
        existing = _templates(db, spec["scene"])
        for channel, tpl in spec.get("templates", {}).items():
            if channel in existing:
                continue
            values: dict[str, Any] = {"page": tpl.get("page", "")}
            if channel == CHANNEL_INAPP:
                values.update(title_tpl=tpl["title_tpl"], body_tpl=tpl["body_tpl"], enabled=True)
            elif channel == CHANNEL_SUBSCRIBE:
                values.update(keywords_json=json.dumps(rules.validate_subscribe_mapping(tpl["keywords"]), ensure_ascii=False),
                              template_ref=tpl.get("template_id", ""), enabled=bool(tpl.get("template_id")),
                              long_term=bool(tpl.get("long_term", False)))
            else:
                values.update(keywords_json=json.dumps(rules.validate_sms_params(tpl.get("params")), ensure_ascii=False),
                              template_ref=tpl.get("template_code", ""), enabled=bool(tpl.get("template_code")))
            db.add(NotificationTemplate(scene=spec["scene"], channel=channel, **values))
            added += 1
    db.commit()
    return added


# ======================================================================== notify（业务模块调用）


def _check_notify_args(scene: str, dedupe_key: str, data: Mapping[str, Any]) -> None:
    if not isinstance(scene, str) or not _SCENE_RE.match(scene):
        raise _bad("invalid scene")
    if not isinstance(dedupe_key, str) or not _KEY_RE.match(dedupe_key):
        raise _bad("dedupe_key must be 1-128 chars of [A-Za-z0-9_-:.]")
    if not isinstance(data, Mapping):
        raise _bad("data must be a mapping")


def notify(db: Session, scene: str, member_id: int, data: Mapping[str, Any], dedupe_key: str, *,
           store_id: int | None = None) -> None:
    """给会员发通知：只写发件箱事件（与业务写同事务，不提交）。同 (scene, member, dedupe_key) 只发一次。"""
    _check_notify_args(scene, dedupe_key, data)
    payload = {"scene": scene, "audience": AUDIENCE_MEMBER, "recipient_ids": [int(member_id)], "store_id": store_id,
               "dedupe_key": dedupe_key, "data": encode_data(data)}
    enqueue_event(db, TOPIC_REQUESTED, payload, aggregate_type="notification", aggregate_id=f"{scene}:{dedupe_key}"[:64])


def notify_staff(db: Session, scene: str, staff_ids: list[int], data: Mapping[str, Any], dedupe_key: str, *,
                 store_id: int | None = None) -> None:
    """员工告警（设备离线、安全告警）：短信 / 站内信，按场景 sms_mode。只写发件箱事件。"""
    _check_notify_args(scene, dedupe_key, data)
    ids = sorted({int(s) for s in staff_ids})
    if not ids or len(ids) > 200:
        raise _bad("staff_ids must contain 1-200 ids")
    payload = {"scene": scene, "audience": AUDIENCE_STAFF, "recipient_ids": ids, "store_id": store_id,
               "dedupe_key": dedupe_key, "data": encode_data(data)}
    enqueue_event(db, TOPIC_REQUESTED, payload, aggregate_type="notification", aggregate_id=f"{scene}:{dedupe_key}"[:64])


# ======================================================================== 频控计数（独立小事务）


def _bump(db: Session, key: str, bucket: str, limit: int, commit: bool = True) -> bool:
    """计数 +1（不超过 limit）；成功返回 True。条件 UPDATE + 唯一约束，单独提交。

    commit=False（事件订阅者内，由 worker 提交）：只 flush；并发插入冲突抛 IntegrityError，由 worker 回滚重试。
    """
    if limit <= 0:
        return False
    if not commit:
        res = db.execute(update(NotificationCounter)
                         .where(NotificationCounter.key == key, NotificationCounter.bucket == bucket,
                                NotificationCounter.count < limit)
                         .values(count=NotificationCounter.count + 1))
        if res.rowcount == 1:
            return True
        if db.execute(select(NotificationCounter.id).where(NotificationCounter.key == key,
                                                           NotificationCounter.bucket == bucket)).first():
            return False
        db.add(NotificationCounter(key=key, bucket=bucket, count=1))
        db.flush()
        return True
    for _ in range(3):
        res = db.execute(update(NotificationCounter)
                         .where(NotificationCounter.key == key, NotificationCounter.bucket == bucket,
                                NotificationCounter.count < limit)
                         .values(count=NotificationCounter.count + 1))
        if res.rowcount == 1:
            db.commit()
            return True
        exists = db.execute(select(NotificationCounter.id).where(NotificationCounter.key == key,
                                                                 NotificationCounter.bucket == bucket)).first()
        if exists:
            db.commit()
            return False
        db.add(NotificationCounter(key=key, bucket=bucket, count=1))
        try:
            db.commit()
            return True
        except IntegrityError:
            db.rollback()
    return False


def _unbump(db: Session, key: str, bucket: str) -> None:
    db.execute(update(NotificationCounter)
               .where(NotificationCounter.key == key, NotificationCounter.bucket == bucket, NotificationCounter.count > 0)
               .values(count=NotificationCounter.count - 1))


def _phone_key(phone: str) -> str:
    return "sms:" + hashlib.sha256(phone.encode()).hexdigest()[:32]


# ======================================================================== 受理（发件箱消费者 → 投递表）


def _tz_for(store_id: int | None) -> str:
    return channels.store_timezone(store_id, get_config().timezone)


def accept_request(db: Session, *, scene: str, audience: str, recipient_ids: list[int], data: Mapping[str, Any],
                   dedupe_key: str, store_id: int | None = None, source_event_id: str | None = None,
                   now: datetime | None = None, commit: bool = True) -> list[int]:
    """按场景渠道策略生成投递行；每个收件人一个事务。返回新建的请求 id（重复的跳过）。

    commit=False：用于 events.SUBSCRIPTIONS（worker 统一提交 / 回滚重试），只 flush，唯一约束冲突向上抛出。
    """
    now = now or clock_now()
    cfg = get_config()
    scene_row = db.execute(select(NotificationScene).where(NotificationScene.scene == scene)).scalar_one_or_none()
    if scene_row is None or not scene_row.enabled:
        log.info("notification scene %s missing or disabled; skipped", scene)
        return []
    if scene_row.audience != audience:
        log.warning("notification scene %s audience %s != %s; skipped", scene, scene_row.audience, audience)
        return []
    templates = {ch: t for ch, t in _templates(db, scene).items() if t.enabled}
    tz = _tz_for(store_id)
    plain = decode_data(data)
    derived = _with_derived(plain, tz)
    created: list[int] = []
    for rid in recipient_ids:
        rid = int(rid)
        dup = db.execute(select(NotificationRequest.id).where(
            NotificationRequest.scene == scene, NotificationRequest.audience == audience,
            NotificationRequest.recipient_id == rid, NotificationRequest.dedupe_key == dedupe_key)).first()
        if dup:
            continue
        # 每会员非紧急频控（订阅消息 + 短信合计，按请求计一次；站内信不受限）
        limited = False
        if audience == AUDIENCE_MEMBER and not scene_row.urgent:
            limited = not _bump(db, f"member:{rid}", "d:" + rules.local_day(now, tz), cfg.member_nonurgent_per_day,
                                commit=commit)
        req = NotificationRequest(scene=scene, audience=audience, recipient_id=rid, store_id=store_id,
                                  dedupe_key=dedupe_key, data_json=json.dumps(dict(data), ensure_ascii=False),
                                  source_event_id=source_event_id, created_at=now)
        db.add(req)
        try:
            db.flush()
        except IntegrityError:
            if not commit:
                raise
            db.rollback()
            continue
        ref = str(rid) if audience == AUDIENCE_MEMBER else f"staff:{rid}"

        def delivery(channel: str, template_ref: str = "", **extra: Any) -> NotificationDelivery:
            row = NotificationDelivery(request_id=req.id, scene=scene, channel=channel, audience=audience,
                                       recipient_id=rid, recipient_ref=ref, store_id=store_id, urgent=scene_row.urgent,
                                       template_ref=template_ref, next_attempt_at=now, **extra)
            db.add(row)
            return row

        inapp = templates.get(CHANNEL_INAPP)
        if inapp is not None:
            try:
                title = rules.clean_text(rules.render_text(inapp.title_tpl, derived))[:200]
                body = rules.clean_text(rules.render_text(inapp.body_tpl, derived))[:1000]
                db.add(NotificationInbox(recipient_type=audience, recipient_id=rid, request_id=req.id, scene=scene,
                                         category=scene_row.category, title=title, body=body,
                                         page=_render_page(inapp.page, derived), created_at=now))
                delivery(CHANNEL_INAPP, status=ST_SENT, sent_at=now)
            except rules.MissingField as exc:
                delivery(CHANNEL_INAPP, status=ST_SKIPPED, errcode="missing_field", errmsg=f"missing {exc.field}")
        external_status = {"status": ST_SKIPPED, "errcode": "member_rate_limited"} if limited else {}
        sub = templates.get(CHANNEL_SUBSCRIBE)
        has_sub = audience == AUDIENCE_MEMBER and sub is not None and bool(sub.template_ref)
        if has_sub:
            delivery(CHANNEL_SUBSCRIBE, sub.template_ref, **external_status)
        sms = templates.get(CHANNEL_SMS)
        if sms is not None and sms.template_ref and (
                scene_row.sms_mode == "always" or (scene_row.sms_mode == "fallback" and not has_sub)):
            delivery(CHANNEL_SMS, sms.template_ref, **external_status)
        if commit:
            db.commit()
        else:
            db.flush()
        created.append(req.id)
    return created


def _render_page(page: str, data: Mapping[str, Any]) -> str:
    if not page:
        return ""
    try:
        return rules.render_text(page, data)[:256]
    except rules.MissingField:
        return page.split("?", 1)[0]


# ======================================================================== 订阅额度


def _grant(db: Session, member_id: int, template_id: str, long_term: bool, now: datetime) -> bool:
    """一次 accept：可用次数 +1（不超过上限），状态恢复 active。不提交。"""
    cap = get_config().quota_cap_per_template
    res = db.execute(update(NotificationSubscription)
                     .where(NotificationSubscription.member_id == member_id,
                            NotificationSubscription.template_id == template_id)
                     .values(available=case((NotificationSubscription.available < cap,
                                             NotificationSubscription.available + 1),
                                            else_=NotificationSubscription.available),
                             accepted_total=NotificationSubscription.accepted_total + 1,
                             status=SUB_ACTIVE, long_term=long_term, last_accept_at=now, updated_at=now))
    if res.rowcount == 1:
        return True
    db.add(NotificationSubscription(member_id=member_id, template_id=template_id, available=1, long_term=long_term,
                                    status=SUB_ACTIVE, accepted_total=1, last_accept_at=now, updated_at=now))
    try:
        db.flush()
    except IntegrityError as exc:
        raise AppError(ErrorCode.CONFLICT, "subscription updated concurrently, retry") from exc
    return True


def _set_status(db: Session, member_id: int, template_id: str, status: str, now: datetime) -> None:
    """拒收 / 封禁：清零可用次数。不提交。"""
    res = db.execute(update(NotificationSubscription)
                     .where(NotificationSubscription.member_id == member_id,
                            NotificationSubscription.template_id == template_id)
                     .values(available=0, status=status, last_reject_at=now, updated_at=now))
    if res.rowcount == 0:
        db.add(NotificationSubscription(member_id=member_id, template_id=template_id, available=0, status=status,
                                        last_reject_at=now, updated_at=now))
        try:
            db.flush()
        except IntegrityError as exc:
            raise AppError(ErrorCode.CONFLICT, "subscription updated concurrently, retry") from exc


def _subscribe_templates(db: Session) -> dict[str, NotificationTemplate]:
    rows = db.execute(select(NotificationTemplate).where(NotificationTemplate.channel == CHANNEL_SUBSCRIBE,
                                                         NotificationTemplate.template_ref != "")).scalars().all()
    return {t.template_ref: t for t in rows}


def report_subscriptions(db: Session, member_id: int, results: Mapping[str, str], request_key: str,
                         now: datetime | None = None) -> dict[str, Any]:
    """前端上报 wx.requestSubscribeMessage 结果（{模板 id: accept|reject|ban|filter|acceptWith…}）。不提交。

    只接受已配置的模板 id（其余忽略并返回 ignored）；一次最多 5 个（官方上限）。
    quota_source=frontend 时 accept 计入额度；拒收 / 封禁总是生效。
    """
    now = now or clock_now()
    if not results or len(results) > rules.REQUEST_MAX_TEMPLATES:
        raise _bad(f"results must contain 1-{rules.REQUEST_MAX_TEMPLATES} templates")
    configured = _subscribe_templates(db)
    count_accept = get_config().quota_source == "frontend"
    applied, ignored = [], []
    for template_id, status in results.items():
        tpl = configured.get(template_id)
        known = status in rules.ACCEPT_STATUSES | rules.REJECT_STATUSES | rules.BAN_STATUSES | rules.IGNORED_STATUSES
        if tpl is None or not known:
            ignored.append(template_id)
            continue
        counted = False
        if status in rules.ACCEPT_STATUSES and count_accept:
            counted = _grant(db, member_id, template_id, tpl.long_term, now)
        elif status in rules.REJECT_STATUSES:
            _set_status(db, member_id, template_id, SUB_REJECTED, now)
        elif status in rules.BAN_STATUSES:
            _set_status(db, member_id, template_id, SUB_BANNED, now)
        db.add(NotificationSubscribeEvent(dedupe_key=f"frontend:{member_id}:{request_key}:{template_id}"[:200],
                                          source="frontend", event="report", member_id=member_id,
                                          template_id=template_id, status=status, counted=counted, created_at=now))
        applied.append(template_id)
    db.flush()
    return {"applied": applied, "ignored": ignored, "subscriptions": member_subscriptions(db, member_id)}


def member_subscriptions(db: Session, member_id: int) -> list[dict[str, Any]]:
    """会员各场景的订阅模板与剩余次数（前端“开启提醒”入口用）。"""
    tpls = db.execute(select(NotificationTemplate).where(NotificationTemplate.channel == CHANNEL_SUBSCRIBE,
                                                         NotificationTemplate.enabled.is_(True),
                                                         NotificationTemplate.template_ref != "")
                      .order_by(NotificationTemplate.scene)).scalars().all()
    subs = {s.template_id: s for s in db.execute(select(NotificationSubscription)
                                                 .where(NotificationSubscription.member_id == member_id)).scalars()}
    out = []
    for t in tpls:
        s = subs.get(t.template_ref)
        out.append({"scene": t.scene, "template_id": t.template_ref, "long_term": t.long_term,
                    "available": (s.available if s else 0), "status": (s.status if s else "none")})
    return out


def subscribe_request_plan(db: Session, scenes: list[str]) -> dict[str, Any]:
    """前端调用 wx.requestSubscribeMessage 前取模板 id：最多 5 个，一次性与长期不能混用（否则 20002）。"""
    if not scenes or len(scenes) > rules.REQUEST_MAX_TEMPLATES:
        raise _bad(f"scenes must contain 1-{rules.REQUEST_MAX_TEMPLATES} items")
    rows = db.execute(select(NotificationTemplate).where(NotificationTemplate.channel == CHANNEL_SUBSCRIBE,
                                                         NotificationTemplate.scene.in_(scenes),
                                                         NotificationTemplate.enabled.is_(True),
                                                         NotificationTemplate.template_ref != "")).scalars().all()
    ids, kinds = [], set()
    for t in sorted(rows, key=lambda r: scenes.index(r.scene)):
        if t.template_ref not in ids:
            ids.append(t.template_ref)
            kinds.add(t.long_term)
    if len(kinds) > 1:
        raise AppError(ErrorCode.BUSINESS_RULE, "one-time and long-term templates cannot be requested together")
    return {"template_ids": ids, "long_term": bool(kinds and kinds.pop())}


def handle_push_event(db: Session, message: Mapping[str, Any], now: datetime | None = None,
                      commit: bool = True) -> dict[str, int]:
    """处理已验签解密的订阅消息推送（popup / change / sent），dedupe_key 去重。

    commit=True：每项单独提交（直接调用）；commit=False：每项一个保存点、不提交（wechat_mp.dispatch 的处理函数约定，
    由 dispatch 与自己的推送去重记录一起提交）。"""
    now = now or clock_now()
    items = rules.parse_subscribe_event(dict(message))
    counts = {"applied": 0, "duplicate": 0, "ignored": 0}
    if not items:
        return counts
    resolver = channels.get_recipient_resolver()
    configured = _subscribe_templates(db)
    count_accept = get_config().quota_source == "push"
    member_id = resolver.member_id_by_openid(db, items[0].openid) if items[0].openid else None
    openid_hash = hashlib.sha256(items[0].openid.encode()).hexdigest()[:16]
    for item in items:
        key = f"push:{item.event}:{openid_hash}:{item.create_time}:{item.template_id}:{item.msg_id}"[:200]
        if db.execute(select(NotificationSubscribeEvent.id).where(NotificationSubscribeEvent.dedupe_key == key)).first():
            counts["duplicate"] += 1
            continue
        counted = False
        tpl = configured.get(item.template_id)
        savepoint = None if commit else db.begin_nested()
        try:
            if item.event in ("popup", "change") and member_id is not None and tpl is not None:
                if item.status in rules.ACCEPT_STATUSES and item.event == "popup" and count_accept:
                    counted = _grant(db, member_id, item.template_id, tpl.long_term, now)
                elif item.status in rules.REJECT_STATUSES:
                    _set_status(db, member_id, item.template_id, SUB_REJECTED, now)
            elif item.event == "sent" and item.msg_id and item.error_code != 0:
                # 异步下发失败：投递已是终态 sent，只记录错误码供后台排查
                db.execute(update(NotificationDelivery).where(NotificationDelivery.provider_msg_id == item.msg_id)
                           .values(errcode=f"async:{item.error_code}", errmsg=item.error_status[:200]))
            db.add(NotificationSubscribeEvent(dedupe_key=key, source="push", event=item.event, member_id=member_id,
                                              template_id=item.template_id, status=item.status,
                                              msg_id=item.msg_id or None, error_code=item.error_code,
                                              counted=counted, created_at=now))
            if commit:
                db.commit()
            else:
                db.flush()
                savepoint.commit()
            counts["applied" if (member_id is not None or item.event == "sent") else "ignored"] += 1
        except (IntegrityError, AppError):
            if commit:
                db.rollback()
            else:
                savepoint.rollback()
            counts["duplicate"] += 1
    return counts


def handle_push_message(db: Session, message: Mapping[str, Any]) -> dict[str, int]:
    """wechat_mp.register_push_handler 用的处理函数：fn(db, message)，不提交。"""
    return handle_push_event(db, message, commit=False)


def _reserve_quota(db: Session, member_id: int, template_id: str) -> str:
    """发送前占用一次额度。返回 long_term（不扣）| reserved（已扣 1）| none（无额度）。不提交。"""
    sub = db.execute(select(NotificationSubscription.long_term, NotificationSubscription.status)
                     .where(NotificationSubscription.member_id == member_id,
                            NotificationSubscription.template_id == template_id)).first()
    if sub is None or sub.status != SUB_ACTIVE:
        return "none"
    if sub.long_term:
        return "long_term"
    res = db.execute(update(NotificationSubscription)
                     .where(NotificationSubscription.member_id == member_id,
                            NotificationSubscription.template_id == template_id,
                            NotificationSubscription.status == SUB_ACTIVE,
                            NotificationSubscription.available > 0)
                     .values(available=NotificationSubscription.available - 1))
    return "reserved" if res.rowcount == 1 else "none"


def _release_quota(db: Session, member_id: int, template_id: str) -> None:
    db.execute(update(NotificationSubscription)
               .where(NotificationSubscription.member_id == member_id, NotificationSubscription.template_id == template_id)
               .values(available=NotificationSubscription.available + 1))


# ======================================================================== 投递 worker


def _finish(db: Session, d: NotificationDelivery, target: str, now: datetime, *, errcode: str | None = None,
            errmsg: str | None = None, **values: Any) -> None:
    DELIVERY_SM.advance(db, NotificationDelivery, d.id, d.status, target, errcode=errcode,
                        errmsg=(errmsg or None) and str(errmsg)[:200], updated_at=now, **values)
    d.status = target


def _maybe_fallback(db: Session, d: NotificationDelivery, scene: NotificationScene | None, now: datetime) -> bool:
    """订阅消息没送达且场景配置了 fallback → 补一条短信投递（每请求最多一条）。"""
    if d.channel != CHANNEL_SUBSCRIBE or scene is None or scene.sms_mode != "fallback":
        return False
    sms = _templates(db, d.scene).get(CHANNEL_SMS)
    if sms is None or not sms.enabled or not sms.template_ref:
        return False
    exists = db.execute(select(NotificationDelivery.id).where(NotificationDelivery.request_id == d.request_id,
                                                              NotificationDelivery.channel == CHANNEL_SMS)).first()
    if exists:
        return False
    db.add(NotificationDelivery(request_id=d.request_id, scene=d.scene, channel=CHANNEL_SMS, audience=d.audience,
                                recipient_id=d.recipient_id, recipient_ref=d.recipient_ref, store_id=d.store_id,
                                urgent=d.urgent, template_ref=sms.template_ref, next_attempt_at=now,
                                fallback_from_id=d.id))
    return True


def _defer(db: Session, d: NotificationDelivery, until: datetime, reason: str, now: datetime) -> None:
    """sending → pending（不计失败次数）：静默时段、分钟频控、日上限。"""
    DELIVERY_SM.advance(db, NotificationDelivery, d.id, ST_SENDING, ST_PENDING, next_attempt_at=until,
                        errcode=reason, updated_at=now)


def _is_transport_error(exc: BaseException) -> bool:
    if isinstance(exc, (UpstreamError, CircuitOpenError, TransientError, TimeoutError, ConnectionError)):
        return True
    return isinstance(exc, AppError) and exc.retryable


def _call(name: str, fn) -> channels.SendResult:
    cfg = get_config()
    try:
        return resilient(name, fn, timeout_s=cfg.send_timeout_seconds, attempts=1, breaker=breakers.get(name))
    except Exception as exc:  # noqa: BLE001
        if _is_transport_error(exc):
            return channels.SendResult(ok=False, errcode="transport", errmsg=str(exc)[:200], retryable=True)
        log.exception("notification sender raised")
        return channels.SendResult(ok=False, errcode="exception", errmsg=type(exc).__name__, retryable=False)


def dispatch_due(db: Session, now: datetime | None = None, limit: int | None = None) -> dict[str, int]:
    """处理到期的 pending 投递。返回各结果计数。多实例并发安全（领取用条件 UPDATE）。"""
    now = now or clock_now()
    limit = limit or get_config().batch_size
    ids = db.execute(select(NotificationDelivery.id)
                     .where(NotificationDelivery.status == ST_PENDING, NotificationDelivery.next_attempt_at <= now)
                     .order_by(NotificationDelivery.next_attempt_at, NotificationDelivery.id).limit(limit)).scalars().all()
    db.commit()
    counts: dict[str, int] = {}
    for delivery_id in ids:
        outcome = process_delivery(db, delivery_id, now)
        counts[outcome] = counts.get(outcome, 0) + 1
    return counts


def process_delivery(db: Session, delivery_id: int, now: datetime | None = None) -> str:
    now = now or clock_now()
    cfg = get_config()
    d = db.get(NotificationDelivery, delivery_id)
    if d is None or d.status != ST_PENDING:
        return "not_pending"
    db.refresh(d)
    if d.status != ST_PENDING or _aware(d.next_attempt_at) > now:
        return "not_due"
    scene = db.execute(select(NotificationScene).where(NotificationScene.scene == d.scene)).scalar_one_or_none()
    tz = _tz_for(d.store_id)
    if scene is None or not scene.enabled:
        _finish(db, d, ST_SKIPPED, now, errcode="scene_disabled")
        db.commit()
        return "skipped"
    # 静默时段（门店时区）：非紧急延后到结束时刻
    if not d.urgent and cfg.quiet_enabled:
        release = rules.quiet_release(now, tz, rules.parse_hhmm(cfg.quiet_start), rules.parse_hhmm(cfg.quiet_end))
        if release is not None:
            db.execute(update(NotificationDelivery).where(NotificationDelivery.id == d.id,
                                                          NotificationDelivery.status == ST_PENDING)
                       .values(next_attempt_at=release, errcode="quiet_hours", updated_at=now))
            db.commit()
            return "deferred"
    # 领取：pending → sending（条件 UPDATE，多 worker 只有一个成功）
    try:
        DELIVERY_SM.advance(db, NotificationDelivery, d.id, ST_PENDING, ST_SENDING, updated_at=now)
        db.commit()
    except AppError:
        db.rollback()
        return "not_pending"
    d.status = ST_SENDING
    template = _templates(db, d.scene).get(d.channel)
    if template is None or not template.enabled or not template.template_ref:
        _finish(db, d, ST_SKIPPED, now, errcode="template_disabled")
        _maybe_fallback(db, d, scene, now)
        db.commit()
        return "skipped"
    req = db.get(NotificationRequest, d.request_id)
    data = _with_derived(decode_data(json.loads(req.data_json if req else "{}")), tz)
    if d.channel == CHANNEL_SUBSCRIBE:
        return _send_subscribe(db, d, scene, template, data, tz, now)
    if d.channel == CHANNEL_SMS:
        return _send_sms(db, d, template, data, tz, now)
    _finish(db, d, ST_SKIPPED, now, errcode="unsupported_channel")  # pragma: no cover
    db.commit()  # pragma: no cover
    return "skipped"  # pragma: no cover


def _after_failure(db: Session, d: NotificationDelivery, scene: NotificationScene, result: channels.SendResult,
                   outcome: str, now: datetime) -> str:
    cfg = get_config()
    if outcome == rules.OUTCOME_RETRY:
        attempts = d.attempts + 1
        if attempts >= cfg.max_attempts:
            _finish(db, d, ST_FAILED, now, errcode=result.errcode, errmsg=result.errmsg, attempts=attempts)
            _maybe_fallback(db, d, scene, now)
            db.commit()
            return "failed"
        delay = rules.backoff_seconds(attempts, cfg.retry_backoff_seconds)
        _finish(db, d, ST_PENDING, now, errcode=result.errcode, errmsg=result.errmsg, attempts=attempts,
                next_attempt_at=now + timedelta(seconds=delay))
        db.commit()
        return "retry"
    if outcome == rules.OUTCOME_DEFER_DAY:
        _finish(db, d, ST_PENDING, now, errcode=result.errcode, errmsg=result.errmsg,
                next_attempt_at=rules.next_local_midnight(now, _tz_for(d.store_id)))
        db.commit()
        return "deferred"
    if outcome == rules.OUTCOME_NO_QUOTA:
        _finish(db, d, ST_SKIPPED, now, errcode=result.errcode, errmsg=result.errmsg, attempts=d.attempts + 1)
        _maybe_fallback(db, d, scene, now)
        db.commit()
        return "skipped"
    _finish(db, d, ST_FAILED, now, errcode=result.errcode, errmsg=result.errmsg, attempts=d.attempts + 1)
    _maybe_fallback(db, d, scene, now)
    db.commit()
    return "failed"


def _send_subscribe(db: Session, d: NotificationDelivery, scene: NotificationScene, template: NotificationTemplate,
                    data: dict[str, Any], tz: str, now: datetime) -> str:
    cfg = get_config()
    recipient = channels.get_recipient_resolver().member(db, d.recipient_id)
    if recipient is None or not recipient.openid:
        _finish(db, d, ST_SKIPPED, now, errcode="no_openid")
        _maybe_fallback(db, d, scene, now)
        db.commit()
        return "skipped"
    try:
        payload = rules.build_subscribe_data(json.loads(template.keywords_json or "[]"), data, tz)
    except rules.MissingField as exc:
        _finish(db, d, ST_SKIPPED, now, errcode="missing_field", errmsg=f"missing {exc.field}")
        _maybe_fallback(db, d, scene, now)
        db.commit()
        return "skipped"
    except rules.FormatError as exc:
        _finish(db, d, ST_SKIPPED, now, errcode="format", errmsg=str(exc))
        _maybe_fallback(db, d, scene, now)
        db.commit()
        return "skipped"
    page = _render_page(template.page, data)
    template_id = d.template_ref or template.template_ref
    # 额度：一次性模板占用 1 次（失败退回）；重领的投递若已占用不再重复占用
    reserved = d.quota_reserved
    long_term = False
    if not reserved:
        state = _reserve_quota(db, d.recipient_id, template_id)
        if state == "none":
            _finish(db, d, ST_SKIPPED, now, errcode="no_quota")
            _maybe_fallback(db, d, scene, now)
            db.commit()
            return "skipped"
        long_term = state == "long_term"
        reserved = state == "reserved"
        # 占用与投递行上的标记同一事务提交，进程崩溃后 reclaim 不会重复占用
        db.execute(update(NotificationDelivery).where(NotificationDelivery.id == d.id).values(quota_reserved=reserved))
        db.commit()
    day_bucket = "d:" + rules.local_day(now, tz)
    if not _bump(db, "app:subscribe", day_bucket, cfg.subscribe_daily_cap):
        if reserved:
            _release_quota(db, d.recipient_id, template_id)
        db.execute(update(NotificationDelivery).where(NotificationDelivery.id == d.id).values(quota_reserved=False))
        _defer(db, d, rules.next_local_midnight(now, tz), "daily_cap", now)
        db.commit()
        return "deferred"

    sender = channels.get_subscribe_sender()
    result = _call("notify.subscribe", lambda: sender.send(
        touser=recipient.openid, template_id=template_id, data=payload, page=page or None,
        miniprogram_state=cfg.miniprogram_state, lang=cfg.lang))
    errcode = int(result.errcode) if result.errcode.lstrip("-").isdigit() else None
    if result.ok:
        outcome = rules.OUTCOME_SENT
    elif result.retryable is not None:
        outcome = rules.OUTCOME_RETRY if result.retryable else rules.OUTCOME_PERMANENT
    else:
        outcome = rules.classify_wechat(errcode) if errcode is not None else rules.OUTCOME_PERMANENT
    d.quota_reserved = reserved
    if outcome == rules.OUTCOME_SENT:
        if reserved:
            db.execute(update(NotificationSubscription)
                       .where(NotificationSubscription.member_id == d.recipient_id,
                              NotificationSubscription.template_id == template_id)
                       .values(consumed_total=NotificationSubscription.consumed_total + 1))
        _finish(db, d, ST_SENT, now, errcode=None, attempts=d.attempts + 1, sent_at=now,
                provider_msg_id=(result.msg_id or None), quota_reserved=False)
        db.commit()
        return "sent"
    # 未送达：退回额度与日计数（消息没有下发，额度不算消耗）
    if reserved:
        _release_quota(db, d.recipient_id, template_id)
    _unbump(db, "app:subscribe", day_bucket)
    db.execute(update(NotificationDelivery).where(NotificationDelivery.id == d.id).values(quota_reserved=False))
    if outcome == rules.OUTCOME_NO_QUOTA and cfg.on_43101 == "reset":
        # 微信说没有订阅关系：本地计数以微信为准清零；长期订阅视为用户已退订
        values: dict[str, Any] = {"available": 0, "updated_at": now}
        if long_term:
            values.update(status=SUB_REJECTED, last_reject_at=now)
        db.execute(update(NotificationSubscription)
                   .where(NotificationSubscription.member_id == d.recipient_id,
                          NotificationSubscription.template_id == template_id).values(**values))
    return _after_failure(db, d, scene, result, outcome, now)


def _send_sms(db: Session, d: NotificationDelivery, template: NotificationTemplate, data: dict[str, Any], tz: str,
              now: datetime) -> str:
    cfg = get_config()
    scene = db.execute(select(NotificationScene).where(NotificationScene.scene == d.scene)).scalar_one_or_none()
    resolver = channels.get_recipient_resolver()
    recipient = resolver.member(db, d.recipient_id) if d.audience == AUDIENCE_MEMBER else resolver.staff(db, d.recipient_id)
    if recipient is None or not recipient.phone:
        _finish(db, d, ST_SKIPPED, now, errcode="no_phone")
        db.commit()
        return "skipped"
    try:
        params = rules.build_sms_params(json.loads(template.keywords_json or "[]"), data)
    except rules.MissingField as exc:
        _finish(db, d, ST_SKIPPED, now, errcode="missing_field", errmsg=f"missing {exc.field}")
        db.commit()
        return "skipped"
    key = _phone_key(recipient.phone)
    local = rules._local(now, tz)
    minute_b, hour_b, day_b = local.strftime("m:%Y%m%d%H%M"), local.strftime("h:%Y%m%d%H"), local.strftime("d:%Y%m%d")
    taken: list[tuple[str, str]] = []

    def give_back() -> None:
        for k, b in taken:
            _unbump(db, k, b)

    if not _bump(db, "app:sms", day_b, cfg.sms_daily_cap):
        _defer(db, d, rules.next_local_midnight(now, tz), "sms_daily_cap", now)
        db.commit()
        return "deferred"
    taken.append(("app:sms", day_b))
    for bucket, limit in ((day_b, cfg.sms_per_day), (hour_b, cfg.sms_per_hour)):
        if not _bump(db, key, bucket, limit):
            give_back()
            _finish(db, d, ST_SKIPPED, now, errcode="sms_rate_limited")
            db.commit()
            return "skipped"
        taken.append((key, bucket))
    if not _bump(db, key, minute_b, cfg.sms_per_minute):
        give_back()
        _defer(db, d, (local.replace(second=0, microsecond=0) + timedelta(minutes=1)).astimezone(timezone.utc),
               "sms_minute_limit", now)
        db.commit()
        return "deferred"
    taken.append((key, minute_b))
    sender = channels.get_sms_sender()
    result = _call("notify.sms", lambda: sender.send(phone=recipient.phone, template_code=template.template_ref,
                                                     params=params))
    outcome = rules.OUTCOME_SENT if result.ok else rules.classify_sms(result.errcode, result.retryable)
    if outcome == rules.OUTCOME_SENT:
        _finish(db, d, ST_SENT, now, errcode=None, attempts=d.attempts + 1, sent_at=now,
                provider_msg_id=(result.msg_id or None))
        db.commit()
        return "sent"
    give_back()
    if result.errcode in rules.SMS_RATE_LIMIT_CODES:
        _finish(db, d, ST_SKIPPED, now, errcode=result.errcode, errmsg=result.errmsg, attempts=d.attempts + 1)
        db.commit()
        return "skipped"
    return _after_failure(db, d, scene, result, outcome, now)


def reclaim_stuck(db: Session, now: datetime | None = None) -> int:
    """sending 超过 sending_stuck_seconds（进程崩溃）→ pending 重试；额度占用保留（quota_reserved），不重复扣。"""
    now = now or clock_now()
    cutoff = now - timedelta(seconds=get_config().sending_stuck_seconds)
    res = db.execute(update(NotificationDelivery)
                     .where(NotificationDelivery.status == ST_SENDING, NotificationDelivery.updated_at < cutoff)
                     .values(status=ST_PENDING, errcode="stuck", attempts=NotificationDelivery.attempts + 1,
                             next_attempt_at=now, updated_at=now))
    db.commit()
    return res.rowcount


def purge_old(db: Session, now: datetime | None = None) -> dict[str, int]:
    now = now or clock_now()
    cfg = get_config()
    counters = db.execute(delete(NotificationCounter)
                          .where(NotificationCounter.created_at < now - timedelta(days=3))).rowcount
    inbox = db.execute(delete(NotificationInbox).where(or_(
        NotificationInbox.created_at < now - timedelta(days=cfg.inbox_retention_days),
        NotificationInbox.deleted_at < now - timedelta(days=30)))).rowcount
    db.commit()
    return {"counters": counters, "inbox": inbox}


# ======================================================================== 站内信


INBOX_SPEC = ListSpec(
    fields={
        "category": Field(NotificationInbox.category, "str", ("eq", "in"), choices=frozenset(CATEGORIES)),
        "read_at": Field(NotificationInbox.read_at, "datetime", ("null",)),
        "created_at": Field(NotificationInbox.created_at, "datetime", ("range",), sortable=True),
    },
    id_column=NotificationInbox.id,
    default_sort="-created_at",
)


def message_to_dict(m: NotificationInbox) -> dict[str, Any]:
    return {"id": m.id, "scene": m.scene, "category": m.category, "title": m.title, "body": m.body, "page": m.page,
            "read": m.read_at is not None, "read_at": _aware(m.read_at), "created_at": _aware(m.created_at)}


def _owner(rtype: str, rid: int):
    return (NotificationInbox.recipient_type == rtype, NotificationInbox.recipient_id == rid,
            NotificationInbox.deleted_at.is_(None))


def list_inbox(db: Session, rtype: str, rid: int, params: Mapping[str, Any]) -> dict[str, Any]:
    stmt = select(NotificationInbox).where(*_owner(rtype, rid))
    return INBOX_SPEC.cursor(db, stmt, params, get_config().cursor_secret, serialize=message_to_dict)


def unread_counts(db: Session, rtype: str, rid: int) -> dict[str, Any]:
    rows = db.execute(select(NotificationInbox.category, func.count())
                      .where(*_owner(rtype, rid), NotificationInbox.read_at.is_(None))
                      .group_by(NotificationInbox.category)).all()
    by = {c: n for c, n in rows}
    return {"total": sum(by.values()), "by_category": by}


def mark_read(db: Session, rtype: str, rid: int, message_id: int, now: datetime | None = None) -> dict[str, Any]:
    now = now or clock_now()
    row = db.execute(select(NotificationInbox).where(NotificationInbox.id == message_id, *_owner(rtype, rid))).scalar_one_or_none()
    if row is None:
        raise AppError(ErrorCode.NOT_FOUND, "message not found")
    db.execute(update(NotificationInbox).where(NotificationInbox.id == message_id, NotificationInbox.read_at.is_(None))
               .values(read_at=now))
    return {"id": message_id, "read": True}


def mark_all_read(db: Session, rtype: str, rid: int, category: str | None = None, now: datetime | None = None) -> dict[str, int]:
    now = now or clock_now()
    if category is not None and category not in CATEGORIES:
        raise _bad("unknown category")
    conds = [*_owner(rtype, rid), NotificationInbox.read_at.is_(None)]
    if category:
        conds.append(NotificationInbox.category == category)
    res = db.execute(update(NotificationInbox).where(*conds).values(read_at=now))
    return {"updated": res.rowcount}


def delete_message(db: Session, rtype: str, rid: int, message_id: int, now: datetime | None = None) -> dict[str, Any]:
    now = now or clock_now()
    res = db.execute(update(NotificationInbox).where(NotificationInbox.id == message_id, *_owner(rtype, rid))
                     .values(deleted_at=now, read_at=func.coalesce(NotificationInbox.read_at, now)))
    if res.rowcount != 1:
        raise AppError(ErrorCode.NOT_FOUND, "message not found")
    return {"id": message_id, "deleted": True}


# ======================================================================== 后台：投递日志、重发、统计、导出


def _delivery_fields() -> dict[str, Field]:
    return {
        "scene": Field(NotificationDelivery.scene, "str", ("eq", "in"), sortable=True),
        "channel": Field(NotificationDelivery.channel, "str", ("eq", "in"), choices=frozenset(CHANNELS)),
        "status": Field(NotificationDelivery.status, "str", ("eq", "in"), choices=frozenset(DELIVERY_STATUSES)),
        "errcode": Field(NotificationDelivery.errcode, "str", ("eq", "in", "null")),
        "audience": Field(NotificationDelivery.audience, "str", ("eq",), choices=frozenset(AUDIENCES)),
        "recipient_id": Field(NotificationDelivery.recipient_id, "int", ("eq",)),
        "store_id": Field(NotificationDelivery.store_id, "int", ("eq", "in")),
        "attempts": Field(NotificationDelivery.attempts, "int", ("range",), sortable=True),
        "created_at": Field(NotificationDelivery.created_at, "datetime", ("range",), sortable=True),
        "sent_at": Field(NotificationDelivery.sent_at, "datetime", ("range",), sortable=True),
    }


_KEYWORDS = (NotificationDelivery.recipient_ref, NotificationDelivery.scene, NotificationDelivery.provider_msg_id)
DELIVERY_SPEC = ListSpec(fields=_delivery_fields(), id_column=NotificationDelivery.id, keyword_columns=_KEYWORDS)
DELIVERY_EXPORT_SPEC = ListSpec(fields=_delivery_fields(), id_column=NotificationDelivery.id, keyword_columns=_KEYWORDS,
                                max_range_days=31)
STATS_SPEC = ListSpec(
    fields={k: v for k, v in _delivery_fields().items() if k in ("scene", "channel", "audience", "store_id", "created_at")},
    id_column=NotificationDelivery.id, max_range_days=92,
)


def delivery_to_dict(d: NotificationDelivery) -> dict[str, Any]:
    return {"id": d.id, "request_id": d.request_id, "scene": d.scene, "channel": d.channel, "audience": d.audience,
            "recipient_id": d.recipient_id, "store_id": d.store_id, "template_ref": d.template_ref, "status": d.status,
            "attempts": d.attempts, "errcode": d.errcode, "errmsg": d.errmsg, "provider_msg_id": d.provider_msg_id,
            "next_attempt_at": _aware(d.next_attempt_at), "sent_at": _aware(d.sent_at),
            "fallback_from_id": d.fallback_from_id, "created_at": _aware(d.created_at)}


def list_deliveries(db: Session, params: Mapping[str, Any]) -> dict[str, Any]:
    return DELIVERY_SPEC.page(db, select(NotificationDelivery), params, serialize=delivery_to_dict)


EXPORT_SYNC_LIMIT = 5000
EXPORT_HEADERS = ("ID", "场景", "渠道", "受众", "收件人", "门店", "状态", "尝试次数", "错误码", "创建时间", "发送时间")


def export_deliveries(db: Session, staff_id: int, params: Mapping[str, Any]):
    """导出投递日志 CSV（与列表同一筛选；≤31 天；≤5000 行，超出 4225；写审计）。返回 CSV 字节。"""
    stream = export_csv(db, DELIVERY_EXPORT_SPEC, select(NotificationDelivery), params, EXPORT_HEADERS,
                        lambda d: (d.id, d.scene, d.channel, d.audience, d.recipient_ref, d.store_id, d.status,
                                   d.attempts, d.errcode, _aware(d.created_at), _aware(d.sent_at)),
                        hard_limit=EXPORT_SYNC_LIMIT)
    content = b"".join(stream)  # 同步导出 ≤ 5000 行（SPEC §3.4），在会话关闭前生成完
    write_audit(db, "staff", staff_id, "notification.delivery.export", "notification_delivery", "-",
                after={k: str(v) for k, v in params.items()})
    db.commit()
    return content


def resend_delivery(db: Session, staff_id: int, delivery_id: int, reason: str | None = None,
                    now: datetime | None = None) -> dict[str, Any]:
    """后台重发：只允许 failed → pending（其他状态 4090）；清零尝试次数，立即到期。不提交。"""
    now = now or clock_now()
    d = db.get(NotificationDelivery, delivery_id)
    if d is None:
        raise AppError(ErrorCode.NOT_FOUND, "delivery not found")
    before = {"status": d.status, "attempts": d.attempts, "errcode": d.errcode}
    DELIVERY_SM.advance(db, NotificationDelivery, d.id, d.status, ST_PENDING,
                        attempts=0, next_attempt_at=now, errcode=None, errmsg=None, updated_at=now)
    write_audit(db, "staff", staff_id, "notification.delivery.resend", "notification_delivery", d.id, before,
                {"status": ST_PENDING}, reason=reason)
    return {"id": d.id, "status": ST_PENDING}


def stats(db: Session, params: Mapping[str, Any]) -> dict[str, Any]:
    """按渠道 × 状态计数、失败错误码分布、订阅额度不足率（no_quota + 43101 占订阅投递的比例）。"""
    by_status = db.execute(STATS_SPEC.filter(
        select(NotificationDelivery.channel, NotificationDelivery.status, func.count())
        .group_by(NotificationDelivery.channel, NotificationDelivery.status), params)).all()
    channels_out: dict[str, dict[str, int]] = {}
    for channel, status, n in by_status:
        channels_out.setdefault(channel, {s: 0 for s in DELIVERY_STATUSES})[status] = n
    errs = db.execute(STATS_SPEC.filter(
        select(NotificationDelivery.channel, NotificationDelivery.errcode, func.count())
        .where(NotificationDelivery.status.in_((ST_FAILED, ST_SKIPPED)), NotificationDelivery.errcode.is_not(None))
        .group_by(NotificationDelivery.channel, NotificationDelivery.errcode)
        .order_by(func.count().desc()).limit(50), params)).all()
    sub = channels_out.get(CHANNEL_SUBSCRIBE, {})
    sub_total = sum(v for k, v in sub.items() if k in (ST_SENT, ST_FAILED, ST_SKIPPED))
    exhausted = sum(n for ch, code, n in errs if ch == CHANNEL_SUBSCRIBE and code in ("no_quota", "43101"))
    sent = sum(v.get(ST_SENT, 0) for v in channels_out.values())
    failed = sum(v.get(ST_FAILED, 0) for v in channels_out.values())
    return {
        "sent": sent, "failed": failed, "by_channel": channels_out,
        "failed_by_errcode": [{"channel": ch, "errcode": code, "count": n} for ch, code, n in errs],
        "quota_exhaustion_rate": round(exhausted / sub_total, 4) if sub_total else 0.0,
        "subscribe_total": sub_total, "quota_exhausted": exhausted,
    }
