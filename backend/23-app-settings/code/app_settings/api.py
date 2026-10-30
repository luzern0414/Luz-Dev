"""app_settings 接口（薄层）：运行配置的内部接口与客户端下发、文案与展示的小程序下发与后台维护。

| 接口 | 鉴权 |
|---|---|
| `GET /internal/config`、`POST /internal/config/reload` | 内网（含本机回环）+ 运维令牌 `LUZ_OPS_TOKEN`；外网 404，内网缺令牌 403 |
| `GET /api/v1/app/config`、`GET /api/v1/app/content` | 无；带版本或 If-None-Match 且未变化返回 304 |
| `/api/v1/admin/content/*`（文案、展示、修改历史） | 后台员工 `settings:read` / `settings:write`；写接口带 Idempotency-Key，审计在 service 同事务完成 |

文案对全部门店生效，只有全门店员工能改；展示设置的门店覆盖按员工门店范围校验（越界 4031）。
`/api/v1/admin/settings` 是门店模块的经营设置，不在本模块。
"""
from __future__ import annotations

import hashlib
import logging
from typing import Any, Literal

from fastapi import APIRouter, Depends, Path, Query, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from admin_rbac import Staff, assert_in_scope, require_permission, store_scope
from luzback import AppError, ErrorCode, get_db, idempotency_key, ok, run_idempotent
from luzback import log_events as ev
from luzback.app import client_ip, ops_access
from luzback.logging import log_event
from luzback.runtime_config import registry
from luzback.runtime_loader import ConfigStore, config_store, current_snapshot

from . import content
from .models import ContentRevision, UiText
from .rules import client_values, etag_matches

log = logging.getLogger("luz.app_settings")

Platform = Literal["miniprogram", "app", "h5"]
VERSION_PATTERN = r"^[0-9A-Za-z.+-]{1,32}$"

internal_router = APIRouter(prefix="/internal", tags=["internal"], include_in_schema=False)
app_router = APIRouter(prefix="/api/v1/app", tags=["app-settings"])
admin_router = APIRouter(prefix="/api/v1/admin", tags=["app-settings-admin"])
routers = [internal_router, app_router, admin_router]


# ================================================================ 运行配置：内部接口

def _store(request: Request) -> ConfigStore:
    return getattr(request.app.state, "config_store", None) or config_store()


def _require_ops(request: Request) -> None:
    access = ops_access(request)
    if access == "ok":
        return
    log_event(log, ev.SECURITY_AUTHZ_FAIL, logging.WARNING, outcome="failure", source_ip=client_ip(request),
              reason="not_internal" if access == "hidden" else "ops_token_invalid", **{"http.route": request.url.path})
    if access == "hidden":
        raise AppError(ErrorCode.NOT_FOUND, "not found")
    raise AppError(ErrorCode.FORBIDDEN, "ops token required", {"reason": "ops_token_required"})


@internal_router.get("/config")
def internal_config(request: Request):
    """本实例的配置状态：版本、加载时间、来源提交、待重启生效的键、最近一次失败；不含任何值。"""
    _require_ops(request)
    return ok(_store(request).status())


@internal_router.post("/config/reload")
def internal_reload(request: Request):
    """立即重新加载（发布脚本同步完文件后调用）；校验失败保留旧配置并返回每条错误。"""
    _require_ops(request)
    result = _store(request).reload("reload_api")
    data = {"result": result.result, "config_version": result.version, "previous_version": result.previous_version,
            "changed_keys": list(result.changed_keys), "restart_keys": list(result.restart_keys),
            "errors": [e.as_dict() for e in result.errors]}
    if result.result == "rejected":
        return JSONResponse(status_code=422, content={"code": int(ErrorCode.BUSINESS_RULE),
                                                      "message": "config rejected, the previous config is still in use",
                                                      "data": {"reason": "config_invalid", **data}})
    return ok(data)


def _not_modified(etag: str) -> Response:
    return Response(status_code=304, headers={"ETag": etag, "Cache-Control": "no-cache"})


def _cacheable(data: dict[str, Any], etag: str) -> JSONResponse:
    return JSONResponse(content={"code": 0, "message": "ok", "data": jsonable_encoder(data)},
                        headers={"ETag": etag, "Cache-Control": "no-cache"})


@app_router.get("/config")
def app_config(request: Request, platform: Platform | None = None,
               client_version: str | None = Query(default=None, pattern=VERSION_PATTERN),
               config_version: str | None = Query(default=None, max_length=16),
               store_id: int | None = Query(default=None, ge=1)):
    """客户端可见的运行参数（白名单）：按端与客户端版本过滤，命中门店、渠道与版本的覆盖规则；版本未变返回 304。

    不带 platform 时只给各端通用的键（发布脚本的冒烟检查用这个地址）。
    """
    snapshot = getattr(request.state, "config_snapshot", None) or current_snapshot()
    etag = f'"{snapshot.version}"'
    if config_version == snapshot.version or etag_matches(request.headers.get("If-None-Match"), snapshot.version):
        return _not_modified(etag)
    resolved = snapshot.resolve(store_id=store_id, channel=platform, client_version=client_version)
    values = client_values(resolved, registry().values(), platform, client_version)
    return _cacheable({"config_version": snapshot.version, "values": values}, etag)


# ================================================================ 文案与展示：小程序下发

@app_router.get("/content")
def app_content(request: Request, platform: Platform | None = None,
                client_version: str | None = Query(default=None, pattern=VERSION_PATTERN),
                content_version: int | None = Query(default=None, ge=0),
                store_id: int | None = Query(default=None, ge=1), db: Session = Depends(get_db)):
    """文案（含隐藏键）与公开的展示配置（门店覆盖全局）；内容版本未变返回 304。旧版本端忽略不认识的键。"""
    current = content.content_version(db)
    etag = f'"{current}"'
    if content_version == current or etag_matches(request.headers.get("If-None-Match"), str(current)):
        return _not_modified(etag)
    data = content.public_content(db, store_id)
    return _cacheable(data, f'"{data["content_version"]}"')


# ================================================================ 文案与展示：后台

class TextBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str | None = Field(default=None, max_length=2000)   # 空 = 用默认文案（可留空的键：该位置不显示）
    version: int = Field(ge=1)


class DisplayBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    store_id: int | None = Field(default=None, ge=1)          # 空 = 全局
    value: Any
    version: int = Field(ge=0)                                # 这一级当前的版本；还没有值时为 0


class ScopeBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    store_id: int | None = Field(default=None, ge=1)


class RestoreBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: int = Field(ge=0)                                # 目标当前的版本（条件更新）


def _audit(staff: Staff) -> dict[str, Any]:
    return {"ip": staff.ip, "user_agent": staff.user_agent, "trace_id": staff.trace_id}


def _write(db: Session, scope: str, key: str, staff: Staff, body: BaseModel | None, fn):
    fingerprint = hashlib.sha256((body.model_dump_json() if body is not None else scope).encode()).hexdigest()
    return ok(run_idempotent(db, scope, key, lambda: jsonable_encoder(fn()), actor=f"staff:{staff.id}",
                             fingerprint=fingerprint, require_key=True))


@admin_router.get("/content/texts")
def admin_texts(request: Request, staff: Staff = Depends(require_permission("settings:read")),
                db: Session = Depends(get_db)):
    """按页面列出后台可见的文案键（隐藏键不出现）；筛选 page_name（小程序页面），关键词匹配键、位置说明与文案。"""
    stmt = select(UiText).where(UiText.admin_visible.is_(True))
    return ok(jsonable_encoder(content.TEXT_SPEC.page(db, stmt, request.query_params, serialize=content.text_view)))


@admin_router.put("/content/texts/{key}")
def admin_update_text(body: TextBody, key: str = Path(max_length=80),
                      staff: Staff = Depends(require_permission("settings:write")),
                      idem: str = Depends(idempotency_key), db: Session = Depends(get_db)):
    assert_in_scope(staff, None)  # 文案对全部门店生效
    return _write(db, f"content.text.update:{key}", idem, staff, body,
                  lambda: content.update_text(db, staff.id, key, body.text, body.version, **_audit(staff)))


@admin_router.post("/content/texts/{key}/reset")
def admin_reset_text(key: str = Path(max_length=80), staff: Staff = Depends(require_permission("settings:write")),
                     idem: str = Depends(idempotency_key), db: Session = Depends(get_db)):
    assert_in_scope(staff, None)
    return _write(db, f"content.text.reset:{key}", idem, staff, None,
                  lambda: content.reset_text(db, staff.id, key, **_audit(staff)))


@admin_router.get("/content/revisions")
def admin_revisions(request: Request, staff: Staff = Depends(require_permission("settings:read")),
                    db: Session = Depends(get_db)):
    """修改历史（只含后台可见的键）；门店员工只看全局与自己门店的记录。"""
    texts, displays = content.visible_keys(db)
    stmt = select(ContentRevision).where(or_(
        (ContentRevision.target_type == "text") & ContentRevision.target_key.in_(texts or [""]),
        (ContentRevision.target_type == "display") & ContentRevision.target_key.in_(displays or [""])))
    scope = store_scope(staff)
    if scope is not None:
        stmt = stmt.where(or_(ContentRevision.store_id.is_(None), ContentRevision.store_id.in_(scope or [0])))
    return ok(jsonable_encoder(content.REVISION_SPEC.page(db, stmt, request.query_params, serialize=content.revision_view)))


@admin_router.post("/content/revisions/{revision_id}/restore")
def admin_restore_revision(body: RestoreBody, revision_id: int = Path(ge=1),
                           staff: Staff = Depends(require_permission("settings:write")),
                           idem: str = Depends(idempotency_key), db: Session = Depends(get_db)):
    """撤销这次修改：目标恢复为修改之前的值，并生成新版本。"""
    revision = db.get(ContentRevision, revision_id)
    if revision is None:
        raise AppError(ErrorCode.NOT_FOUND, "revision not found")
    assert_in_scope(staff, revision.store_id)
    return _write(db, f"content.restore:{revision_id}", idem, staff, body,
                  lambda: content.restore_revision(db, staff.id, revision_id, body.version, **_audit(staff)))


@admin_router.get("/content/display")
def admin_display(store_id: int | None = Query(default=None, ge=1),
                  staff: Staff = Depends(require_permission("settings:read")), db: Session = Depends(get_db)):
    """展示设置（只列后台可见的键）：默认值、全局值、门店值、生效值与来源、这一级的版本。"""
    if store_id is not None:
        assert_in_scope(staff, store_id)
    return ok(jsonable_encoder({"store_id": store_id, "items": content.list_display(db, store_id),
                                "content_version": content.content_version(db)}))


@admin_router.put("/content/display/{key}")
def admin_update_display(body: DisplayBody, key: str = Path(max_length=64),
                         staff: Staff = Depends(require_permission("settings:write")),
                         idem: str = Depends(idempotency_key), db: Session = Depends(get_db)):
    assert_in_scope(staff, body.store_id)  # 全局值只有全门店员工能改
    return _write(db, f"content.display.update:{key}", idem, staff, body,
                  lambda: content.update_display(db, staff.id, key, body.value, body.version, body.store_id,
                                                 **_audit(staff)))


@admin_router.post("/content/display/{key}/reset")
def admin_reset_display(body: ScopeBody, key: str = Path(max_length=64),
                        staff: Staff = Depends(require_permission("settings:write")),
                        idem: str = Depends(idempotency_key), db: Session = Depends(get_db)):
    """全局：恢复登记的默认值；门店：取消门店覆盖，回到全局值。"""
    assert_in_scope(staff, body.store_id)
    return _write(db, f"content.display.reset:{key}", idem, staff, body,
                  lambda: content.reset_display(db, staff.id, key, body.store_id, **_audit(staff)))
