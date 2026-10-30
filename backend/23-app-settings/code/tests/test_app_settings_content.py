"""文案与展示：种子校验与同步（幂等、默认文案变化、删键、隐藏键）、小程序下发（含隐藏键、门店覆盖全局、304）、
后台列表不含隐藏键、修改校验（超长、未知变量、内容安全、隐藏键、门店范围、权限）、版本冲突（含并发）、
恢复默认与历史恢复、审计与修改历史。
"""
from __future__ import annotations

import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import sessionmaker

import app_settings
import app_settings.api
from admin_rbac.testing import staff_headers
from app_settings import content
from app_settings.seed import SeedText, load_seed, sync_texts
from luzback import AuditLog, Base
from luzback.db import get_session_factory
from luzback.testing import make_config_store, make_test_app

READ, WRITE = "settings:read", "settings:write"


@pytest.fixture()
def env(tmp_path):
    app = make_test_app(app_settings.api.routers, "app_settings.models", "admin_rbac.models",
                        url=f"sqlite:///{(tmp_path / 'content.db').as_posix()}", config=make_config_store(tmp_path / "cfg"))
    db = get_session_factory()()
    sync_texts(db)
    db.commit()
    tokens: dict = {}

    def staff(*codes, stores=None) -> dict:
        key = (codes or (READ, WRITE), tuple(stores) if stores is not None else None)
        if key not in tokens:
            tokens[key] = staff_headers(db, *key[0], store_ids=stores)
        return tokens[key]

    counter = iter(range(1, 10_000))

    def idem() -> dict:
        return {"Idempotency-Key": f"content-key-{next(counter):06d}"}

    content.set_text_checker(None)
    with TestClient(app) as client:
        yield client, db, staff, idem
    content.set_text_checker(None)
    db.close()


def _version(client) -> int:
    return client.get("/api/v1/app/content").json()["data"]["content_version"]


# ---------------------------------------------------------------- 种子

def test_repository_seed_is_valid_and_has_hidden_keys():
    seed = load_seed()
    keys = {s.key for s in seed}
    assert "order_confirm.pay_hint" in keys and len(keys) == len(seed)
    assert {s.key for s in seed if not s.admin_visible} >= {"common.network_error", "wallet.prepaid_notice"}


def test_bad_seed_lists_every_problem(tmp_path):
    path = Path(tmp_path) / "texts.toml"
    path.write_text('[texts."home.title"]\npage = "menu"\nslot = "x"\ndefault = "欢迎{name}来到本店"\nmax_length = 4\n'
                    'colour = "red"\n[other]\n', encoding="utf-8")
    with pytest.raises(ValueError) as exc:
        load_seed(path)
    text = str(exc.value)
    for expected in ("unknown fields", "page must be the key prefix", "content_text_too_long", "content_variable_unknown",
                     "unknown table [other]"):
        assert expected in text, expected


def test_sync_is_idempotent_and_versions_effective_changes(env):
    client, db, _staff, _idem = env
    seed = load_seed()
    assert sync_texts(db, seed)["unchanged"] == len(seed)
    db.commit()
    v0 = _version(client)
    changed = [SeedText(**{**s.__dict__, "default_text": "立即支付"}) if s.key == "order_confirm.pay_hint" else s
               for s in seed if s.key != "menu.empty"]
    counts = sync_texts(db, changed)
    db.commit()
    assert counts["updated"] == 1 and counts["deleted"] == 1
    texts = client.get("/api/v1/app/content").json()["data"]
    assert texts["texts"]["order_confirm.pay_hint"] == "立即支付" and "menu.empty" not in texts["texts"]
    assert texts["content_version"] == v0 + 2  # 生效文案变化与删键各一条 migration 记录
    sources = db.execute(select(app_settings.ContentRevision.source).order_by(app_settings.ContentRevision.id.desc())
                         .limit(2)).scalars().all()
    assert sources == ["migration", "migration"]


# ---------------------------------------------------------------- 小程序下发

def test_app_content_includes_hidden_texts_and_supports_304(env):
    client, *_ = env
    resp = client.get("/api/v1/app/content", params={"platform": "miniprogram", "client_version": "1.0.0"})
    data = resp.json()["data"]
    assert data["texts"]["common.network_error"] == "网络不太稳定，请稍后重试"  # 隐藏键照常下发
    assert data["texts"]["order_confirm.pay_hint"].startswith("请在 {minutes} 分钟")
    assert data["display"]["list.page_size"] == 20 and data["display"]["product.show_sales"] is True
    version = data["content_version"]
    assert resp.headers["ETag"] == f'"{version}"'
    assert client.get("/api/v1/app/content", params={"content_version": version}).status_code == 304
    assert client.get("/api/v1/app/content", headers={"If-None-Match": f'"{version}"'}).status_code == 304
    assert client.get("/api/v1/app/content", params={"content_version": version - 1}).status_code == 200


def test_display_store_value_overrides_global(env):
    client, db, staff, idem = env
    put = lambda key, value, version, store=None: client.put(  # noqa: E731
        f"/api/v1/admin/content/display/{key}", json={"value": value, "version": version, "store_id": store},
        headers={**staff(), **idem()})
    assert put("product.show_sales", False, 0).json()["data"]["source"] == "global"
    store_resp = put("product.show_sales", True, 0, store=5).json()["data"]
    assert store_resp["source"] == "store" and store_resp["global_value"] is False and store_resp["version"] == 1
    assert client.get("/api/v1/app/content", params={"store_id": 5}).json()["data"]["display"]["product.show_sales"] is True
    assert client.get("/api/v1/app/content", params={"store_id": 6}).json()["data"]["display"]["product.show_sales"] is False
    assert client.get("/api/v1/app/content").json()["data"]["display"]["product.show_sales"] is False
    reset = client.post("/api/v1/admin/content/display/product.show_sales/reset", json={"store_id": 5},
                        headers={**staff(), **idem()}).json()["data"]
    assert reset["source"] == "global" and reset["value"] is False  # 取消门店覆盖，回到全局值
    assert client.get("/api/v1/app/content", params={"store_id": 5}).json()["data"]["display"]["product.show_sales"] is False
    rows = db.execute(select(app_settings.DisplaySetting)).scalars().all()
    assert [(r.store_id, r.key, r.admin_visible) for r in rows] == [(None, "product.show_sales", True)]


# ---------------------------------------------------------------- 后台：文案

def test_admin_text_list_excludes_hidden_keys_and_filters(env):
    client, _db, staff, _idem = env
    data = client.get("/api/v1/admin/content/texts", headers=staff(READ)).json()["data"]
    keys = {item["key"] for item in data["items"]}
    assert "order_confirm.pay_hint" in keys and "common.network_error" not in keys and "wallet.prepaid_notice" not in keys
    assert data["page_size"] == 20 and data["total"] == len(keys)
    page = client.get("/api/v1/admin/content/texts", params={"page_name": "order_confirm"}, headers=staff(READ)).json()["data"]
    assert [i["key"] for i in page["items"]] == ["order_confirm.pay_hint", "order_confirm.remark_placeholder"]
    found = client.get("/api/v1/admin/content/texts", params={"keyword": "起送"}, headers=staff(READ)).json()["data"]
    assert [i["key"] for i in found["items"]] == ["cart.min_order_hint"]
    item = page["items"][0]
    assert item["variables"] == ["minutes"] and item["max_length"] == 40 and item["version"] == 1


def test_update_text_validates_and_takes_effect_with_audit(env):
    client, db, staff, idem = env
    url = "/api/v1/admin/content/texts/order_confirm.pay_hint"
    v0 = _version(client)
    too_long = client.put(url, json={"text": "请" * 41, "version": 1}, headers={**staff(), **idem()})
    assert too_long.status_code == 400 and too_long.json()["data"]["reason"] == "content_text_too_long"
    unknown = client.put(url, json={"text": "请在 {mins} 分钟内支付", "version": 1}, headers={**staff(), **idem()})
    assert unknown.status_code == 400 and unknown.json()["data"]["reason"] == "content_variable_unknown"
    assert unknown.json()["data"]["unknown"] == ["mins"]
    ok = client.put(url, json={"text": "  {minutes} 分钟内付款，超时自动取消  ", "version": 1}, headers={**staff(), **idem()})
    data = ok.json()["data"]
    assert ok.status_code == 200 and data["text"] == "{minutes} 分钟内付款，超时自动取消" and data["version"] == 2
    assert data["content_version"] == v0 + 1
    app_texts = client.get("/api/v1/app/content").json()["data"]
    assert app_texts["texts"]["order_confirm.pay_hint"] == data["text"] and app_texts["content_version"] == v0 + 1
    audit = db.execute(select(AuditLog).where(AuditLog.action == "content.text.update")).scalars().all()
    assert len(audit) == 1 and audit[0].resource_id == "order_confirm.pay_hint" and "超时自动取消" in audit[0].after_json
    revision = db.get(app_settings.ContentRevision, data["content_version"])
    assert revision.source == "admin" and revision.target_type == "text" and revision.changed_by == int(audit[0].actor_id)


def test_hidden_keys_cannot_be_changed_from_admin(env):
    client, _db, staff, idem = env
    resp = client.put("/api/v1/admin/content/texts/common.network_error", json={"text": "出错了", "version": 1},
                      headers={**staff(), **idem()})
    assert resp.status_code == 403 and resp.json()["data"]["reason"] == "content_key_hidden"
    reset = client.post("/api/v1/admin/content/texts/common.network_error/reset", headers={**staff(), **idem()})
    assert reset.json()["data"]["reason"] == "content_key_hidden"
    display = client.put("/api/v1/admin/content/display/list.page_size", json={"value": 30, "version": 0},
                         headers={**staff(), **idem()})
    assert display.status_code == 403 and display.json()["data"]["reason"] == "content_key_hidden"
    listed = client.get("/api/v1/admin/content/display", headers=staff(READ)).json()["data"]["items"]
    assert "list.page_size" not in {i["key"] for i in listed} and "product.show_sales" in {i["key"] for i in listed}


def test_display_values_are_validated(env):
    client, _db, staff, idem = env
    bad = client.put("/api/v1/admin/content/display/product.low_stock_threshold", json={"value": 0, "version": 0},
                     headers={**staff(), **idem()})
    assert bad.status_code == 400 and bad.json()["data"]["reason"] == "validation_failed"
    order = client.put("/api/v1/admin/content/display/home.modules", json={"value": ["banner", "ads"], "version": 0},
                       headers={**staff(), **idem()})
    assert order.json()["data"]["reason"] == "validation_failed" and "ads" in order.json()["message"]
    good = client.put("/api/v1/admin/content/display/home.modules", json={"value": ["coupons", "banner"], "version": 0},
                      headers={**staff(), **idem()})
    assert good.json()["data"]["value"] == ["coupons", "banner"]
    label = client.put("/api/v1/admin/content/display/category.hot_label", json={"value": "超级无敌热销款", "version": 0},
                       headers={**staff(), **idem()})
    assert label.json()["data"]["reason"] == "validation_failed"


def test_conflicting_versions_are_rejected_with_current_content(env):
    client, _db, staff, idem = env
    url = "/api/v1/admin/content/texts/menu.empty"
    assert client.put(url, json={"text": "暂无商品", "version": 1}, headers={**staff(), **idem()}).status_code == 200
    stale = client.put(url, json={"text": "别的说法", "version": 1}, headers={**staff(), **idem()})
    body = stale.json()
    assert stale.status_code == 409 and body["data"]["reason"] == "content_conflict"
    assert body["data"]["current"]["text"] == "暂无商品" and body["data"]["current"]["version"] == 2
    display = "/api/v1/admin/content/display/product.show_reviews"
    assert client.put(display, json={"value": False, "version": 0}, headers={**staff(), **idem()}).status_code == 200
    again = client.put(display, json={"value": True, "version": 0}, headers={**staff(), **idem()})
    assert again.status_code == 409 and again.json()["data"]["current"]["version"] == 1


def test_concurrent_updates_only_one_wins(tmp_path):
    url = f"sqlite:///{(tmp_path / 'race.db').as_posix()}"
    engine = create_engine(url, connect_args={"check_same_thread": False, "timeout": 30})

    @event.listens_for(engine, "connect")
    def _pragmas(conn, _record):
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")

    import admin_rbac.models  # noqa: F401
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as db:
        sync_texts(db)
        db.commit()
    results: list[str] = []
    barrier = threading.Barrier(12)

    def worker(i: int) -> None:
        db = factory()
        try:
            barrier.wait()
            content.update_text(db, 1, "home.search_placeholder", f"搜索 {i}", 1)
            db.commit()
            results.append("ok")
        except Exception as exc:  # noqa: BLE001
            db.rollback()
            results.append(getattr(exc, "data", {}).get("reason", type(exc).__name__))
        finally:
            db.close()

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results.count("ok") == 1 and results.count("content_conflict") == 11, results
    with factory() as db:
        row = db.execute(select(app_settings.UiText).where(app_settings.UiText.key == "home.search_placeholder")).scalar_one()
        assert row.version == 2
        revisions = db.execute(select(app_settings.ContentRevision).where(
            app_settings.ContentRevision.target_key == "home.search_placeholder",
            app_settings.ContentRevision.source == "admin")).scalars().all()
        assert len(revisions) == 1
    engine.dispose()


def test_reset_and_restore_from_history(env):
    client, db, staff, idem = env
    url = "/api/v1/admin/content/texts/payment.failed"
    first = client.put(url, json={"text": "支付失败，请重试", "version": 1}, headers={**staff(), **idem()}).json()["data"]
    second = client.put(url, json={"text": "支付没成功，订单还在", "version": 2}, headers={**staff(), **idem()}).json()["data"]
    history = client.get("/api/v1/admin/content/revisions", params={"key": "payment.failed"}, headers=staff(READ)).json()["data"]
    assert [i["new_value"] for i in history["items"]][:2] == ["支付没成功，订单还在", "支付失败，请重试"]
    assert history["items"][-1]["source"] == "migration" and history["items"][-1]["restorable"] is False  # 种子写入
    restored = client.post(f"/api/v1/admin/content/revisions/{second['content_version']}/restore", json={"version": 3},
                           headers={**staff(), **idem()}).json()["data"]
    assert restored["text"] == "支付失败，请重试" and restored["version"] == 4  # 恢复为这次修改之前的值，生成新版本
    reset = client.post(f"{url}/reset", headers={**staff(), **idem()}).json()["data"]
    assert reset["text"] is None and reset["effective_text"] == reset["default_text"] and reset["version"] == 5
    noop = client.post(f"{url}/reset", headers={**staff(), **idem()}).json()["data"]
    assert noop["version"] == 5 and noop["content_version"] == reset["content_version"]  # 已是默认：不生成新版本
    assert first["version"] == 2
    sources = [i["source"] for i in client.get("/api/v1/admin/content/revisions", params={"key": "payment.failed"},
                                               headers=staff(READ)).json()["data"]["items"]]
    assert sources == ["admin", "restore", "admin", "admin", "migration"]
    actions = set(db.execute(select(AuditLog.action)).scalars())
    assert {"content.text.update", "content.text.restore", "content.text.reset"} <= actions
    migration = db.execute(select(app_settings.ContentRevision).where(
        app_settings.ContentRevision.source == "migration")).scalars().first()
    blocked = client.post(f"/api/v1/admin/content/revisions/{migration.id}/restore", json={"version": 1},
                          headers={**staff(), **idem()})
    assert blocked.status_code == 409 and blocked.json()["data"]["reason"] == "revision_not_restorable"


def test_display_restore_removes_store_override(env):
    client, _db, staff, idem = env
    url = "/api/v1/admin/content/display/home.recommend_limit"
    made = client.put(url, json={"value": 12, "version": 0, "store_id": 3}, headers={**staff(), **idem()}).json()["data"]
    restored = client.post(f"/api/v1/admin/content/revisions/{made['content_version']}/restore", json={"version": 1},
                           headers={**staff(), **idem()}).json()["data"]
    assert restored["source"] == "default" and restored["value"] == 6 and restored["store_value"] is None


def test_content_safety_hook(env):
    client, _db, staff, idem = env
    content.set_text_checker(lambda text: "contains banned words" if "违禁" in text else True)
    url = "/api/v1/admin/content/texts/home.welcome"
    unsafe = client.put(url, json={"text": "违禁词{store_name}", "version": 1}, headers={**staff(), **idem()})
    assert unsafe.status_code == 422 and unsafe.json()["data"]["reason"] == "content_unsafe"

    def broken(_text):
        raise TimeoutError("service down")

    content.set_text_checker(broken)
    down = client.put(url, json={"text": "欢迎", "version": 1}, headers={**staff(), **idem()})
    assert down.status_code == 502  # 检测服务不可用：不保存未检测的文案
    content.set_text_checker(None)
    empty = client.put(url, json={"text": "", "version": 1}, headers={**staff(), **idem()}).json()["data"]
    assert empty["text"] == "" and empty["effective_text"] == ""  # 可留空的键：该位置不显示


def test_permissions_and_store_scope(env):
    client, _db, staff, idem = env
    url = "/api/v1/admin/content/texts/menu.empty"
    assert client.get("/api/v1/admin/content/texts").status_code == 401
    assert client.put(url, json={"text": "无", "version": 1}, headers={**staff(READ), **idem()}).status_code == 403
    store_staff = staff(READ, WRITE, stores=[5])
    scoped = client.put(url, json={"text": "无", "version": 1}, headers={**store_staff, **idem()})
    assert scoped.status_code == 403 and scoped.json()["code"] == 4031  # 文案对全部门店生效
    own = client.put("/api/v1/admin/content/display/product.show_stock", json={"value": True, "version": 0, "store_id": 5},
                     headers={**store_staff, **idem()})
    assert own.status_code == 200 and own.json()["data"]["store_value"] is True
    other = client.put("/api/v1/admin/content/display/product.show_stock", json={"value": True, "version": 0, "store_id": 6},
                       headers={**store_staff, **idem()})
    assert other.json()["code"] == 4031
    not_per_store = client.put("/api/v1/admin/content/display/product.image_ratio", json={"value": "4:3", "version": 0,
                                                                                      "store_id": 5},
                               headers={**staff(), **idem()})
    assert not_per_store.json()["data"]["reason"] == "content_key_hidden"
    missing_key = client.put(url, json={"text": "无", "version": 1})
    assert missing_key.status_code == 401
