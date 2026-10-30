"""经营设置：登记与校验、默认值、后台列表与修改（版本冲突、门店覆盖、隐藏键、高风险键要求重新验证、审计）、
业务模块读到后台的值（储值退款手续费、退券策略）。"""
from __future__ import annotations

from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

import app_settings
import app_settings.api
import coupon  # noqa: F401 - 导入即登记 coupon.* 经营设置
import member_card  # noqa: F401
import wallet  # noqa: F401
from admin_rbac import config as admin_config
from admin_rbac.testing import staff_headers
from luzback import AuditLog, utcnow
from luzback.business import BusinessSetting, business_value, declare_business_settings, get_business_setting
from luzback.db import get_session_factory
from luzback.testing import make_config_store, make_test_app, override_business

READ, WRITE = "settings:read", "settings:write"

declare_business_settings(
    BusinessSetting("demo.secret_ratio", "演示隐藏键", "demo", "int", 3, minimum=1, maximum=9, admin_visible=False),
)


@pytest.fixture()
def env(tmp_path):
    app = make_test_app(app_settings.api.routers, "app_settings.models", "admin_rbac.models",
                        url=f"sqlite:///{(tmp_path / 'biz.db').as_posix()}", config=make_config_store(tmp_path / "cfg"))
    db = get_session_factory()()
    counter = iter(range(1, 10_000))

    def idem() -> dict:
        return {"Idempotency-Key": f"business-key-{next(counter):06d}"}

    with TestClient(app) as client:
        yield client, db, idem
    admin_config.set_clock(None)
    db.close()


def _item(client, headers, key, store_id=None):
    params = {"store_id": store_id} if store_id else {}
    items = client.get("/api/v1/admin/settings", headers=headers, params=params).json()["data"]["items"]
    return next(i for i in items if i["key"] == key)


def test_registry_validates_defaults_and_values():
    item = get_business_setting("wallet.customer_refund_fee_bp")
    assert item.default == 0 and item.risk == "high" and item.store_scope
    with pytest.raises(ValueError):
        item.validate(10_001)
    with pytest.raises(ValueError):
        get_business_setting("coupon.return_policy").validate("sometimes")
    with pytest.raises(ValueError):
        declare_business_settings(BusinessSetting("bad.key", "x", "bad", "int", 99, minimum=0, maximum=10))
    assert business_value(None, "coupon.return_policy") == "full_refund"  # 没有存储时用登记的默认值
    override_business(**{"coupon.return_policy": "never"})
    assert business_value(None, "coupon.return_policy") == "never"


def test_admin_lists_visible_settings_and_updates_with_version(env):
    client, db, idem = env
    headers = staff_headers(db, READ, WRITE)
    items = client.get("/api/v1/admin/settings", headers=headers).json()["data"]["items"]
    keys = {i["key"] for i in items}
    assert {"wallet.split_policy", "wallet.customer_refund_fee_bp", "coupon.return_policy",
            "coupon.return_window_hours", "member_card.replace_fee_cents"} <= keys
    assert "demo.secret_ratio" not in keys  # 隐藏键不在后台列出
    fee = _item(client, headers, "wallet.customer_refund_fee_bp")
    assert fee["source"] == "default" and fee["version"] == 0 and fee["unit"] == "万分比"
    r = client.put("/api/v1/admin/settings/wallet.customer_refund_fee_bp", headers={**headers, **idem()},
                   json={"value": 300, "version": 0, "reason": "协议改为 3%"})
    assert r.status_code == 200, r.text
    assert r.json()["data"]["value"] == 300 and r.json()["data"]["version"] == 1
    db.expire_all()
    assert business_value(db, "wallet.customer_refund_fee_bp") == 300
    stale = client.put("/api/v1/admin/settings/wallet.customer_refund_fee_bp", headers={**headers, **idem()},
                       json={"value": 200, "version": 0})
    assert stale.status_code == 409 and stale.json()["data"]["reason"] == "setting_conflict"
    audit = db.scalars(select(AuditLog).where(AuditLog.action == "business.setting.update")).all()
    assert len(audit) == 1 and audit[0].reason == "协议改为 3%"


def test_store_override_hidden_keys_and_validation(env):
    client, db, idem = env
    headers = staff_headers(db, READ, WRITE)
    client.put("/api/v1/admin/settings/member_card.replace_fee_cents", headers={**headers, **idem()},
               json={"value": 1000, "version": 0})
    r = client.put("/api/v1/admin/settings/member_card.replace_fee_cents", headers={**headers, **idem()},
                   json={"value": 500, "version": 0, "store_id": 3})
    assert r.status_code == 200 and r.json()["data"]["source"] == "store"
    db.expire_all()
    assert business_value(db, "member_card.replace_fee_cents", 3) == 500
    assert business_value(db, "member_card.replace_fee_cents", 4) == 1000  # 没有门店值时用全局值
    not_per_store = client.put("/api/v1/admin/settings/coupon.return_policy", headers={**headers, **idem()},
                               json={"value": "never", "version": 0, "store_id": 3})
    assert not_per_store.json()["data"]["reason"] == "store_override_not_allowed"
    hidden = client.put("/api/v1/admin/settings/demo.secret_ratio", headers={**headers, **idem()},
                        json={"value": 4, "version": 0})
    assert hidden.status_code == 403 and hidden.json()["data"]["reason"] == "business_key_hidden"
    bad = client.put("/api/v1/admin/settings/coupon.return_policy", headers={**headers, **idem()},
                     json={"value": "sometimes", "version": 0})
    assert bad.status_code == 400 and bad.json()["data"]["reason"] == "validation_failed"
    reset = client.post("/api/v1/admin/settings/member_card.replace_fee_cents/reset", headers={**headers, **idem()},
                        json={"store_id": 3})
    assert reset.json()["data"]["source"] == "global"
    store_staff = staff_headers(db, READ, WRITE, store_ids=[7])
    global_edit = client.put("/api/v1/admin/settings/coupon.return_window_hours", headers={**store_staff, **idem()},
                             json={"value": 24, "version": 0})
    assert global_edit.status_code == 403  # 全局值只有全门店员工能改


def test_high_risk_settings_require_recent_authentication(env):
    client, db, idem = env
    headers = staff_headers(db, READ, WRITE)
    later = utcnow() + timedelta(seconds=400)  # 超过重新验证窗口（300 秒），仍在会话空闲期（900 秒）内
    admin_config.set_clock(lambda: later)
    risky = client.put("/api/v1/admin/settings/wallet.split_policy", headers={**headers, **idem()},
                       json={"value": "principal_first", "version": 0})
    assert risky.json()["data"]["reason"] == "reauth_required"
    low = client.put("/api/v1/admin/settings/coupon.return_window_hours", headers={**headers, **idem()},
                     json={"value": 24, "version": 0})
    assert low.status_code == 200, low.text  # 低风险键不要求重新验证


def test_modules_read_the_admin_values(env):
    _client, db, _idem = env
    from coupon import service as coupon_service  # noqa: PLC0415

    app_settings.business.update_setting(db, 1, "coupon.return_policy", "never", 0)
    db.flush()
    assert business_value(db, "coupon.return_policy") == "never"
    result = coupon_service.return_on_refund(db, 999, True)
    assert result == {"returned": [], "expired": [], "kept": []}  # 没有用过券的订单照常返回
