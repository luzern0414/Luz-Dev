"""notification 路由：会员端（订阅上报、站内信）、后台（投递日志、重发、统计、导出、模板映射、员工站内信）。

只做参数、鉴权、幂等与调用 service；业务规则全部在 service。
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from luzback import get_db, idempotency_key, ok, run_idempotent

from . import service
from .consumers import install_wechat_mp_push_handlers
from .models import AUDIENCE_MEMBER, AUDIENCE_STAFF
from .auth import current_member as current_user, current_staff, require_permission

app_router = APIRouter(prefix="/api/v1/app/notifications", tags=["notification"])
admin_router = APIRouter(prefix="/api/v1/admin/notifications", tags=["notification-admin"])
routers = [app_router, admin_router]

install_wechat_mp_push_handlers()


def _fp(body: Any) -> str:
    return hashlib.sha256(json.dumps(body, sort_keys=True, default=str, ensure_ascii=False).encode()).hexdigest()


def _params(request: Request) -> dict[str, str]:
    return dict(request.query_params)


# ---------------------------------------------------------------- 请求模型


class SubscriptionReportIn(BaseModel):
    results: dict[str, str] = Field(min_length=1, max_length=5)


class ReadAllIn(BaseModel):
    category: str | None = Field(default=None, max_length=16)


class SceneIn(BaseModel):
    scene: str = Field(min_length=3, max_length=64)
    name: str = Field(min_length=1, max_length=64)
    category: str = Field(default="transaction", max_length=16)
    audience: str = Field(default="member", max_length=8)
    urgent: bool = False
    sms_mode: str = Field(default="off", max_length=8)
    enabled: bool = True


class ScenePatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=64)
    category: str | None = Field(default=None, max_length=16)
    urgent: bool | None = None
    sms_mode: str | None = Field(default=None, max_length=8)
    enabled: bool | None = None


class TemplateIn(BaseModel):
    enabled: bool = True
    page: str | None = Field(default=None, max_length=256)
    template_id: str | None = Field(default=None, max_length=128)       # subscribe
    long_term: bool = False                                              # subscribe
    keywords: list[dict[str, Any]] | None = Field(default=None, max_length=20)  # subscribe
    template_code: str | None = Field(default=None, max_length=128)     # sms
    params: list[dict[str, Any]] | None = Field(default=None, max_length=10)    # sms
    title_tpl: str | None = Field(default=None, max_length=200)         # inapp
    body_tpl: str | None = Field(default=None, max_length=1000)         # inapp


class ResendIn(BaseModel):
    reason: str | None = Field(default=None, max_length=200)


# ---------------------------------------------------------------- 会员端


@app_router.get("/subscribe-plan")
def subscribe_plan(scenes: str, user_id: int = Depends(current_user), db: Session = Depends(get_db)):
    """wx.requestSubscribeMessage 前取模板 id（≤5，不混用一次性 / 长期）。scenes 逗号分隔。"""
    names = [s.strip() for s in scenes.split(",") if s.strip()]
    return ok(service.subscribe_request_plan(db, names))


@app_router.get("/subscriptions")
def my_subscriptions(user_id: int = Depends(current_user), db: Session = Depends(get_db)):
    return ok({"items": service.member_subscriptions(db, user_id)})


@app_router.post("/subscriptions")
def report_subscriptions(body: SubscriptionReportIn, user_id: int = Depends(current_user),
                         key: str = Depends(idempotency_key), db: Session = Depends(get_db)):
    data = body.model_dump()
    return ok(run_idempotent(db, "notification.subscriptions.report", key,
                             lambda: service.report_subscriptions(db, user_id, body.results, key),
                             actor=f"user:{user_id}", fingerprint=_fp(data), require_key=True))


@app_router.get("/messages")
def my_messages(request: Request, user_id: int = Depends(current_user), db: Session = Depends(get_db)):
    return ok(service.list_inbox(db, AUDIENCE_MEMBER, user_id, _params(request)))


@app_router.get("/messages/unread-count")
def my_unread(user_id: int = Depends(current_user), db: Session = Depends(get_db)):
    return ok(service.unread_counts(db, AUDIENCE_MEMBER, user_id))


@app_router.post("/messages/{message_id}/read")
def read_message(message_id: int, user_id: int = Depends(current_user), key: str = Depends(idempotency_key),
                 db: Session = Depends(get_db)):
    return ok(run_idempotent(db, f"notification.message.read:{message_id}", key,
                             lambda: service.mark_read(db, AUDIENCE_MEMBER, user_id, message_id),
                             actor=f"user:{user_id}", fingerprint=_fp({"id": message_id}), require_key=True))


@app_router.post("/messages/read-all")
def read_all(body: ReadAllIn, user_id: int = Depends(current_user), key: str = Depends(idempotency_key),
             db: Session = Depends(get_db)):
    return ok(run_idempotent(db, "notification.message.read_all", key,
                             lambda: service.mark_all_read(db, AUDIENCE_MEMBER, user_id, body.category),
                             actor=f"user:{user_id}", fingerprint=_fp(body.model_dump()), require_key=True))


@app_router.delete("/messages/{message_id}")
def delete_message(message_id: int, user_id: int = Depends(current_user), key: str = Depends(idempotency_key),
                   db: Session = Depends(get_db)):
    return ok(run_idempotent(db, f"notification.message.delete:{message_id}", key,
                             lambda: service.delete_message(db, AUDIENCE_MEMBER, user_id, message_id),
                             actor=f"user:{user_id}", fingerprint=_fp({"id": message_id}), require_key=True))


# ---------------------------------------------------------------- 后台：投递日志 / 统计 / 导出 / 重发


@admin_router.get("/deliveries")
def deliveries(request: Request, staff_id: int = Depends(require_permission("notification:read")),
               db: Session = Depends(get_db)):
    return ok(service.list_deliveries(db, _params(request)))


@admin_router.get("/deliveries/export")
def export_deliveries(request: Request, staff_id: int = Depends(require_permission("notification:export")),
                      db: Session = Depends(get_db)):
    content = service.export_deliveries(db, staff_id, _params(request))
    return Response(content=content, media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": 'attachment; filename="notification-deliveries.csv"'})


@admin_router.post("/deliveries/{delivery_id}/resend")
def resend(delivery_id: int, body: ResendIn, staff_id: int = Depends(require_permission("notification:resend")),
           key: str = Depends(idempotency_key), db: Session = Depends(get_db)):
    return ok(run_idempotent(db, f"notification.delivery.resend:{delivery_id}", key,
                             lambda: service.resend_delivery(db, staff_id, delivery_id, body.reason),
                             actor=f"staff:{staff_id}", fingerprint=_fp(body.model_dump()), require_key=True))


@admin_router.get("/stats")
def delivery_stats(request: Request, staff_id: int = Depends(require_permission("notification:read")),
                   db: Session = Depends(get_db)):
    return ok(service.stats(db, _params(request)))


# ---------------------------------------------------------------- 后台：场景与模板映射


@admin_router.get("/scenes")
def scenes(request: Request, staff_id: int = Depends(require_permission("notification:read")),
           db: Session = Depends(get_db)):
    return ok(service.list_scenes(db, _params(request)))


@admin_router.get("/scenes/{scene}")
def scene_detail(scene: str, staff_id: int = Depends(require_permission("notification:read")),
                 db: Session = Depends(get_db)):
    return ok(service.get_scene(db, scene))


@admin_router.post("/scenes")
def create_scene(body: SceneIn, staff_id: int = Depends(require_permission("notification:template")),
                 key: str = Depends(idempotency_key), db: Session = Depends(get_db)):
    data = body.model_dump()
    return ok(run_idempotent(db, "notification.scene.create", key, lambda: service.create_scene(db, staff_id, data),
                             actor=f"staff:{staff_id}", fingerprint=_fp(data), require_key=True))


@admin_router.put("/scenes/{scene}")
def update_scene(scene: str, body: ScenePatch, staff_id: int = Depends(require_permission("notification:template")),
                 key: str = Depends(idempotency_key), db: Session = Depends(get_db)):
    data = body.model_dump(exclude_unset=True)
    return ok(run_idempotent(db, f"notification.scene.update:{scene}", key,
                             lambda: service.update_scene(db, staff_id, scene, data),
                             actor=f"staff:{staff_id}", fingerprint=_fp(data), require_key=True))


@admin_router.delete("/scenes/{scene}")
def delete_scene(scene: str, staff_id: int = Depends(require_permission("notification:template")),
                 key: str = Depends(idempotency_key), db: Session = Depends(get_db)):
    return ok(run_idempotent(db, f"notification.scene.delete:{scene}", key,
                             lambda: service.delete_scene(db, staff_id, scene),
                             actor=f"staff:{staff_id}", fingerprint=_fp({"scene": scene}), require_key=True))


@admin_router.put("/scenes/{scene}/templates/{channel}")
def upsert_template(scene: str, channel: str, body: TemplateIn,
                    staff_id: int = Depends(require_permission("notification:template")),
                    key: str = Depends(idempotency_key), db: Session = Depends(get_db)):
    data = body.model_dump(exclude_unset=True)
    return ok(run_idempotent(db, f"notification.template.upsert:{scene}:{channel}", key,
                             lambda: service.upsert_template(db, staff_id, scene, channel, data),
                             actor=f"staff:{staff_id}", fingerprint=_fp(data), require_key=True))


@admin_router.delete("/scenes/{scene}/templates/{channel}")
def delete_template(scene: str, channel: str, staff_id: int = Depends(require_permission("notification:template")),
                    key: str = Depends(idempotency_key), db: Session = Depends(get_db)):
    return ok(run_idempotent(db, f"notification.template.delete:{scene}:{channel}", key,
                             lambda: service.delete_template(db, staff_id, scene, channel),
                             actor=f"staff:{staff_id}", fingerprint=_fp({"scene": scene, "channel": channel}),
                             require_key=True))


# ---------------------------------------------------------------- 后台：员工站内信（仅需登录）


@admin_router.get("/inbox")
def staff_inbox(request: Request, staff_id: int = Depends(current_staff), db: Session = Depends(get_db)):
    return ok(service.list_inbox(db, AUDIENCE_STAFF, staff_id, _params(request)))


@admin_router.get("/inbox/unread-count")
def staff_unread(staff_id: int = Depends(current_staff), db: Session = Depends(get_db)):
    return ok(service.unread_counts(db, AUDIENCE_STAFF, staff_id))


@admin_router.post("/inbox/{message_id}/read")
def staff_read(message_id: int, staff_id: int = Depends(current_staff), key: str = Depends(idempotency_key),
               db: Session = Depends(get_db)):
    return ok(run_idempotent(db, f"notification.inbox.read:{message_id}", key,
                             lambda: service.mark_read(db, AUDIENCE_STAFF, staff_id, message_id),
                             actor=f"staff:{staff_id}", fingerprint=_fp({"id": message_id}), require_key=True))


@admin_router.post("/inbox/read-all")
def staff_read_all(body: ReadAllIn, staff_id: int = Depends(current_staff), key: str = Depends(idempotency_key),
                   db: Session = Depends(get_db)):
    return ok(run_idempotent(db, "notification.inbox.read_all", key,
                             lambda: service.mark_all_read(db, AUDIENCE_STAFF, staff_id, body.category),
                             actor=f"staff:{staff_id}", fingerprint=_fp(body.model_dump()), require_key=True))


@admin_router.delete("/inbox/{message_id}")
def staff_delete(message_id: int, staff_id: int = Depends(current_staff), key: str = Depends(idempotency_key),
                 db: Session = Depends(get_db)):
    return ok(run_idempotent(db, f"notification.inbox.delete:{message_id}", key,
                             lambda: service.delete_message(db, AUDIENCE_STAFF, staff_id, message_id),
                             actor=f"staff:{staff_id}", fingerprint=_fp({"id": message_id}), require_key=True))
