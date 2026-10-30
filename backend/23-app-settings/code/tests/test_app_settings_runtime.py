"""运行配置接口：内部接口只对内网且持有运维令牌开放（外网 404、缺令牌 403）、重载成功与被拒（旧版本继续生效）、
config_loads 加载记录（启动补写、热加载、被拒）、客户端下发只含可见键并按端与版本过滤、门店覆盖、ETag 与 304、维护模式。
"""
from __future__ import annotations

import json

import pytest
from fastapi import APIRouter
from fastapi.testclient import TestClient
from sqlalchemy import select

import app_settings
import app_settings.api
from app_settings.rules import client_values, etag_matches
from luzback.config import Settings
from luzback.db import get_session_factory
from luzback.runtime_config import KeyInfo, RuntimeConfig, registry
from luzback.testing import dump_toml, make_config_store, make_test_app

OPS = "o" * 40
OVERRIDES = {
    "store_7": {"stores": [7], "set": {"features.maintenance_mode": True}},
    "old_mp": {"channels": ["miniprogram"], "client_version_max": "1.9.9", "set": {"upload.doc_max_mb": 2}},
}


@pytest.fixture()
def env(tmp_path):
    store = make_config_store(tmp_path / "cfg", values={"overrides": OVERRIDES, "client": {"min_version_app": "2.0.0"}})
    write_router = APIRouter(prefix="/api/v1/app")

    @write_router.post("/orders")
    def create_order():
        return {"code": 0, "message": "ok", "data": {"created": True}}

    app = make_test_app([*app_settings.api.routers, write_router], "app_settings.models",
                        url=f"sqlite:///{(tmp_path / 'rt.db').as_posix()}", settings=Settings(ops_token=OPS),
                        config=store)
    app_settings.install(store)  # 进程入口做的事：登记加载记录并补写启动那次
    internal = TestClient(app, client=("127.0.0.1", 50000))
    yield store, app, internal
    store.remove_listener(app_settings.record_load)


def _bearer(token: str = OPS) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _loads() -> list:
    with get_session_factory()() as db:
        return list(db.execute(select(app_settings.ConfigLoad).order_by(app_settings.ConfigLoad.id)).scalars())


def test_internal_endpoints_require_internal_network_and_ops_token(env):
    store, app, internal = env
    outside = TestClient(app, client=("203.0.113.9", 4000))
    assert outside.get("/internal/config", headers=_bearer()).status_code == 404       # 外网：不暴露端点存在
    assert outside.post("/internal/config/reload", headers=_bearer()).status_code == 404
    via_proxy = TestClient(app, client=("127.0.0.1", 4000))                             # 经本机代理转发的外网请求
    assert via_proxy.get("/internal/config", headers={**_bearer(), "X-Forwarded-For": "203.0.113.9"}).status_code == 404
    denied = internal.get("/internal/config")
    assert denied.status_code == 403 and denied.json()["data"]["reason"] == "ops_token_required"
    assert internal.get("/internal/config", headers=_bearer("x" * 40)).status_code == 403
    no_token_app = make_test_app(app_settings.api.routers, settings=Settings(ops_token=""), config=store,
                                 url="sqlite://")
    assert TestClient(no_token_app, client=("127.0.0.1", 1)).get("/internal/config", headers=_bearer("")).status_code == 403
    status = internal.get("/internal/config", headers=_bearer()).json()["data"]
    assert status["config_version"] == store.current().version and status["restart_pending"] == []
    assert status["instance"] == "test-instance" and "values" not in status and "rate_limit" not in json.dumps(status)


def test_reload_api_loads_rejects_and_records(env):
    store, _app, internal = env
    first = store.current().version
    assert [r.result for r in _loads()] == ["loaded"]  # 启动那次在数据库就绪后补写
    same = internal.post("/internal/config/reload", headers=_bearer()).json()["data"]
    assert same["result"] == "unchanged" and same["config_version"] == first
    (store.config_dir / "dev.toml").write_text(dump_toml({"overrides": OVERRIDES, "upload": {"doc_max_mb": 9}}), encoding="utf-8")
    loaded = internal.post("/internal/config/reload", headers=_bearer()).json()["data"]
    assert loaded["result"] == "loaded" and loaded["changed_keys"] == ["client.min_version_app", "upload.doc_max_mb"]
    second = loaded["config_version"]
    (store.config_dir / "dev.toml").write_text("[upload]\ndoc_max_mb = 0\n", encoding="utf-8")
    rejected = internal.post("/internal/config/reload", headers=_bearer())
    body = rejected.json()
    assert rejected.status_code == 422 and body["code"] == 4220 and body["data"]["reason"] == "config_invalid"
    assert body["data"]["config_version"] == second and body["data"]["errors"][0]["key"] == "upload.doc_max_mb"
    assert store.current().version == second  # 旧配置继续生效
    rows = _loads()
    assert [r.result for r in rows] == ["loaded", "unchanged", "loaded", "rejected"]
    assert [r.trigger for r in rows] == ["startup", "reload_api", "reload_api", "reload_api"]
    assert json.loads(rows[2].changed_keys_json) == ["client.min_version_app", "upload.doc_max_mb"]
    assert rows[3].config_version == second and "upload.doc_max_mb" in rows[3].error_summary
    assert all(r.instance_id == "test-instance" for r in rows)
    status = internal.get("/internal/config", headers=_bearer()).json()["data"]
    assert status["last_failure"]["result"] == "rejected" and status["reload_failures_total"] == 1


def test_client_config_only_visible_keys_filtered_by_platform_and_version(env):
    store, _app, internal = env
    resp = internal.get("/api/v1/app/config", params={"platform": "app", "client_version": "2.1.0"})
    data = resp.json()["data"]
    values = data["values"]
    assert data["config_version"] == store.current().version and resp.headers["ETag"] == f'"{data["config_version"]}"'
    visible = {k for k, info in registry().items() if info.client_visible and "app" in info.platforms}
    assert set(values) == visible  # 只下发标了 client_visible 的键
    assert {"upload.doc_max_mb", "client.min_version_app", "identity.sms_resend_seconds", "prepaid.stop_prepaid"} <= set(values)
    assert not any(k.startswith(("rate_limit.", "auth.", "logging.", "db.", "wechat_pay.", "admin_rbac.")) for k in values)
    assert values["client.min_version_app"] == "2.0.0"
    mp = internal.get("/api/v1/app/config", params={"platform": "miniprogram", "client_version": "1.2.0"}).json()["data"]
    assert "client.min_version_app" not in mp["values"] and "client.min_version_miniprogram" in mp["values"]
    assert mp["values"]["upload.doc_max_mb"] == 2      # 旧版本小程序命中覆盖规则
    newer = internal.get("/api/v1/app/config", params={"platform": "miniprogram", "client_version": "2.0.0"}).json()
    assert newer["data"]["values"]["upload.doc_max_mb"] == 5
    store7 = internal.get("/api/v1/app/config", params={"platform": "h5", "store_id": 7}).json()["data"]["values"]
    assert store7["features.maintenance_mode"] is True
    assert internal.get("/api/v1/app/config", params={"platform": "h5"}).json()["data"]["values"]["features.maintenance_mode"] is False
    assert internal.get("/api/v1/app/config", params={"platform": "tv"}).status_code == 400
    common = internal.get("/api/v1/app/config").json()["data"]["values"]  # 不带端：只给各端通用的键（发布冒烟用）
    assert "upload.doc_max_mb" in common and not any(k.startswith("client.min_version") for k in common)


def test_client_config_returns_304_when_version_matches(env):
    store, _app, internal = env
    version = store.current().version
    by_param = internal.get("/api/v1/app/config", params={"platform": "h5", "config_version": version})
    assert by_param.status_code == 304 and by_param.headers["ETag"] == f'"{version}"' and not by_param.content
    by_header = internal.get("/api/v1/app/config", params={"platform": "h5"}, headers={"If-None-Match": f'W/"{version}"'})
    assert by_header.status_code == 304
    stale = internal.get("/api/v1/app/config", params={"platform": "h5", "config_version": "000000000000"})
    assert stale.status_code == 200


def test_client_values_respect_client_since():
    keys = [KeyInfo("upload.doc_max_mb", "upload", "doc_max_mb", "", 5, True, True, "high", "media",
                    ("global",), ("miniprogram",), "1.5.0", ""),
            KeyInfo("http.max_body_kb", "http", "max_body_kb", "", 256, True, False, "high", "framework",
                    ("global",), (), "0.0.0", "")]
    config = RuntimeConfig()
    assert client_values(config, keys, "miniprogram", "1.4.9") == {}            # 新键只给认识它的版本
    assert client_values(config, keys, "miniprogram", "1.5.0") == {"upload.doc_max_mb": 5}
    assert client_values(config, keys, "app", "9.0.0") == {}
    assert client_values(config, registry().values(), "h5", None)["upload.doc_max_mb"] == 5
    assert etag_matches('"a", W/"b"', "b") and not etag_matches(None, "b") and etag_matches("*", "x")


def test_maintenance_mode_blocks_customer_writes_only(env):
    store, app, internal = env
    (store.config_dir / "dev.toml").write_text(dump_toml({"features": {"maintenance_mode": True}}), encoding="utf-8")
    assert store.reload("file_change").result == "loaded"
    blocked = internal.post("/api/v1/app/orders")
    assert blocked.status_code == 503 and blocked.json()["code"] == 5030 and blocked.headers["Retry-After"] == "60"
    assert internal.get("/api/v1/app/config", params={"platform": "h5"}).json()["data"]["values"]["features.maintenance_mode"] is True
    (store.config_dir / "dev.toml").write_text(dump_toml({"overrides": OVERRIDES}), encoding="utf-8")
    store.reload("file_change")
    assert internal.post("/api/v1/app/orders").json()["data"] == {"created": True}
    assert internal.post("/api/v1/app/orders", params={"store_id": 7}).status_code == 200  # 门店覆盖要显式取 config_for(store_id)


def test_record_load_failure_does_not_break_reload(env, caplog):
    store, _app, _internal = env
    with get_session_factory()() as db:
        db.execute(app_settings.ConfigLoad.__table__.delete())
        db.commit()
    app_settings.ConfigLoad.__table__.drop(get_session_factory()().get_bind())
    try:
        assert store.reload("reload_api").result == "unchanged"  # 记录失败只写日志
        assert any(getattr(r, "fields", {}).get("event") == "config.load_record_failed" for r in caplog.records)
    finally:
        app_settings.ConfigLoad.__table__.create(get_session_factory()().get_bind())
