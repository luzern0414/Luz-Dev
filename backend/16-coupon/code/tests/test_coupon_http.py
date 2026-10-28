"""HTTP：鉴权 4010 / 权限 4030 / 参数 4001 / 幂等重放与 4093 / 信封；领券中心游标、领取、我的券、兑换码限流、
购物车可用券、后台模板 / 统计 / 导出 / 批量发放 / 兑换码导出 / 会员券列表与作废。"""
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import coupon
import coupon.api
from admin_rbac.testing import staff_headers
from coupon import CartLine, jobs, service
from luzback import AppError, ErrorCode, create_token, get_settings
from luzback.db import get_session_factory
from luzback.testing import make_test_app

from coupon_fixtures import *  # noqa: F401,F403

ALL = ["coupon:read", "coupon:write", "coupon:issue", "coupon:code", "coupon:export"]


_STATE: dict = {}


@pytest.fixture()
def client(tmp_path):
    app = make_test_app([coupon.api.router], coupon, "admin_rbac.models", url=f"sqlite:///{tmp_path / 'http.db'}")
    _STATE["db"] = get_session_factory()()
    _STATE["tokens"] = {}
    with TestClient(app) as test_client:
        yield test_client
    _STATE.pop("db").close()


def member(uid: int) -> dict:
    s = get_settings()
    return {"Authorization": f"Bearer {create_token(str(uid), s.jwt_secret, 3600, kind='access', issuer=s.jwt_issuer)}"}


def staff(sid: int = 1, perms=tuple(ALL), stores=None) -> dict:
    """真实 admin_rbac 会话（同一 sid / 权限 / 门店复用同一员工）。"""
    k = (sid, tuple(perms), tuple(stores) if stores is not None else None)
    if k not in _STATE["tokens"]:
        _STATE["tokens"][k] = staff_headers(_STATE["db"], *perms, store_ids=stores)
    return _STATE["tokens"][k]


def key(k: str) -> dict:
    return {"Idempotency-Key": f"key-{k}-0001"}


TEMPLATE = {"name": "满100减20", "type": "amount_off", "amount_cents": 2000, "threshold_cents": 10000,
            "valid_type": "relative", "valid_days": 30, "total_stock": 100, "per_member_limit": 1,
            "channels": ["claim_center", "admin", "code"]}


def create_published(client, body=None, k="tpl") -> int:
    resp = client.post("/api/v1/admin/coupon-templates", json=body or TEMPLATE, headers={**staff(), **key(k)})
    assert resp.status_code == 200, resp.text
    tid = resp.json()["data"]["id"]
    resp = client.post(f"/api/v1/admin/coupon-templates/{tid}/publish", headers={**staff(), **key(f"pub{k}")})
    assert resp.json()["data"]["status"] == "active"
    return tid


def test_auth_permission_validation_and_envelope(client):
    resp = client.get("/api/v1/app/coupons")
    assert resp.status_code == 401 and resp.json()["code"] == 4010 and set(resp.json()) == {"code", "message", "data"}
    assert client.get("/api/v1/app/coupons", headers=staff()).json()["code"] == 4010  # 员工令牌不能当会员令牌
    assert client.get("/api/v1/admin/coupon-templates").json()["code"] == 4010
    assert client.get("/api/v1/admin/coupon-templates", headers=member(1)).json()["code"] == 4010
    resp = client.get("/api/v1/admin/coupon-templates", headers=staff(perms=["coupon:code"]))
    assert resp.status_code == 403 and resp.json()["code"] == 4030
    assert client.get("/api/v1/admin/coupon-templates", headers=staff(perms=["coupon:*"])).json()["code"] == 0
    assert client.post("/api/v1/admin/coupon-templates", json=TEMPLATE, headers=staff()).json()["code"] == 4001
    bad = client.post("/api/v1/admin/coupon-templates", json={**TEMPLATE, "rate_bp": 50}, headers={**staff(), **key("b")})
    assert bad.status_code == 400 and bad.json()["data"]["errors"][0]["field"] == "rate_bp"
    semantic = client.post("/api/v1/admin/coupon-templates", json={**TEMPLATE, "type": "percent_off", "rate_bp": 99},
                           headers={**staff(), **key("c")})
    assert semantic.json()["code"] == 4001
    extra = client.post("/api/v1/admin/coupon-templates", json={**TEMPLATE, "issued_count": 5},
                        headers={**staff(), **key("d")})
    assert extra.json()["code"] == 4001  # 防批量赋值


def test_forged_claims_rejected_and_store_scope(client):
    s = get_settings()
    forged = create_token("1", s.jwt_secret, 3600, kind="access", issuer=s.jwt_issuer,
                          extra={"role": "staff", "perms": ["*"]})
    assert client.get("/api/v1/admin/coupon-templates", headers={"Authorization": f"Bearer {forged}"}).json()["code"] == 4010
    forged_admin = create_token("1", s.jwt_secret, 3600, kind="admin", issuer=s.jwt_issuer, extra={"perms": ["*"], "sid": 99})
    assert client.get("/api/v1/admin/coupon-templates",
                      headers={"Authorization": f"Bearer {forged_admin}"}).json()["code"] == 4010
    # 门店员工：只能建 / 改自己门店的模板；全局模板可看不可改；列表只含自己门店与全局
    store3 = staff(3, stores=[3])
    r = client.post("/api/v1/admin/coupon-templates", json={**TEMPLATE, "store_id": 4}, headers={**store3, **key("s4")})
    assert r.status_code == 403 and r.json()["code"] == 4031
    assert client.post("/api/v1/admin/coupon-templates", json=TEMPLATE, headers={**store3, **key("g")}).json()["code"] == 4031
    mine = client.post("/api/v1/admin/coupon-templates", json={**TEMPLATE, "store_id": 3},
                       headers={**store3, **key("s3")}).json()["data"]["id"]
    glob = create_published(client, k="glob")
    other = client.post("/api/v1/admin/coupon-templates", json={**TEMPLATE, "store_id": 5},
                        headers={**staff(), **key("s5")}).json()["data"]["id"]
    ids = {i["id"] for i in client.get("/api/v1/admin/coupon-templates", headers=store3).json()["data"]["items"]}
    assert ids == {mine, glob}
    assert client.get(f"/api/v1/admin/coupon-templates/{glob}", headers=store3).json()["code"] == 0
    assert client.get(f"/api/v1/admin/coupon-templates/{other}", headers=store3).json()["code"] == 4031
    assert client.post(f"/api/v1/admin/coupon-templates/{glob}/pause", headers={**store3, **key("p")}).json()["code"] == 4031
    assert client.post(f"/api/v1/admin/coupon-templates/{mine}/publish",
                       headers={**store3, **key("pm")}).json()["data"]["status"] == "active"
    wildcard = staff(9, perms=["coupon:*"])
    assert client.get("/api/v1/admin/user-coupons/export", headers=wildcard).status_code == 200


def test_member_keyword_phone_uses_identity_hash_lookup(client, monkeypatch):
    tid = create_published(client)
    client.post("/api/v1/admin/coupon-issue-batches", json={"template_id": tid, "user_ids": [21, 22], "reason": "回馈"},
                headers={**staff(perms=["coupon:issue"]), **key("batch")})
    with get_session_factory()() as db:
        jobs.process_issue_batches(db)
    lookups = []

    def fake_lookup(db, phone):
        lookups.append(phone)
        return SimpleNamespace(id=22) if phone == "13800138022" else None

    monkeypatch.setattr(service, "user_id_by_phone",
                        lambda db, phone: (lambda u: u.id if u else None)(fake_lookup(db, phone)))
    rows = client.get("/api/v1/admin/user-coupons?keyword=13800138022", headers=staff()).json()["data"]
    assert [r["user_id"] for r in rows["items"]] == [22]
    assert client.get("/api/v1/admin/user-coupons?keyword=13900000000", headers=staff()).json()["data"]["total"] == 0
    assert lookups == ["13800138022", "13900000000"]
    assert client.get("/api/v1/admin/user-coupons?keyword=21", headers=staff()).json()["data"]["total"] == 1  # 会员 id


def test_admin_template_crud_list_export_and_idempotency(client):
    headers = {**staff(), **key("create")}
    first = client.post("/api/v1/admin/coupon-templates", json=TEMPLATE, headers=headers).json()
    replay = client.post("/api/v1/admin/coupon-templates", json=TEMPLATE, headers=headers).json()
    assert first == replay and first["data"]["status"] == "draft"
    mismatch = client.post("/api/v1/admin/coupon-templates", json={**TEMPLATE, "name": "x"}, headers=headers)
    assert mismatch.status_code == 422 and mismatch.json()["code"] == 4093
    tid = first["data"]["id"]
    patched = client.patch(f"/api/v1/admin/coupon-templates/{tid}", json={"version": 1, "amount_cents": 2500},
                           headers={**staff(), **key("patch")}).json()
    assert patched["data"]["rules"]["amount_cents"] == 2500 and patched["data"]["version"] == 2
    stale = client.patch(f"/api/v1/admin/coupon-templates/{tid}", json={"version": 1, "name": "n"},
                         headers={**staff(), **key("patch2")}).json()
    assert stale["code"] == 4090
    client.post(f"/api/v1/admin/coupon-templates/{tid}/publish", headers={**staff(), **key("pub")})
    other = create_published(client, {**TEMPLATE, "name": "八折券", "type": "percent_off", "rate_bp": 8000}, k="t2")
    listed = client.get("/api/v1/admin/coupon-templates?status=active&keyword=满100&sort=-created_at",
                        headers=staff()).json()["data"]
    assert [i["id"] for i in listed["items"]] == [tid] and listed["total"] == 1
    assert listed["items"][0]["rules_text"] == "满100元减25元；全场通用；不可与其他优惠券同时使用"
    assert client.get("/api/v1/admin/coupon-templates?bogus=1", headers=staff()).json()["code"] == 4001
    detail = client.get(f"/api/v1/admin/coupon-templates/{other}", headers=staff()).json()["data"]
    assert detail["rules_text"].startswith("满100元享8折")
    csv_resp = client.get("/api/v1/admin/coupon-templates/export?type=percent_off", headers=staff())
    assert csv_resp.status_code == 200 and csv_resp.content.startswith(b"\xef\xbb\xbfID,")
    assert "八折券" in csv_resp.text and "满100减20" not in csv_resp.text
    assert len(csv_resp.text.splitlines()) == 2  # 表头 + 1 行
    paused = client.post(f"/api/v1/admin/coupon-templates/{tid}/pause", headers={**staff(), **key("pause")}).json()
    assert paused["data"]["status"] == "paused"
    assert client.delete(f"/api/v1/admin/coupon-templates/{tid}", headers={**staff(), **key("del")}).json()["code"] == 4090
    stats = client.get(f"/api/v1/admin/coupon-templates/{tid}/stats", headers=staff()).json()["data"]
    assert stats["issued"] == 0 and stats["conversion_bp"] == 0
    assert client.get("/api/v1/admin/coupon-templates/999", headers=staff()).json()["code"] == 4040


def test_member_claim_center_claim_and_my_coupons(client):
    tid = create_published(client)
    second = create_published(client, {**TEMPLATE, "name": "无门槛5元", "threshold_cents": 0, "amount_cents": 500}, k="t2")
    center = client.get("/api/v1/app/coupon-templates?limit=1").json()["data"]  # 匿名可看
    assert len(center["items"]) == 1 and center["has_more"] and "claimed_by_me" not in center["items"][0]
    page2 = client.get(f"/api/v1/app/coupon-templates?limit=1&cursor={center['next_cursor']}").json()["data"]
    assert {center["items"][0]["id"], page2["items"][0]["id"]} == {tid, second}
    claimed = client.post(f"/api/v1/app/coupon-templates/{tid}/claim", headers={**member(7), **key("claim")}).json()
    replay = client.post(f"/api/v1/app/coupon-templates/{tid}/claim", headers={**member(7), **key("claim")}).json()
    assert claimed["code"] == 0 and replay == claimed
    again = client.post(f"/api/v1/app/coupon-templates/{tid}/claim", headers={**member(7), **key("claim2")})
    assert again.status_code == 422 and again.json()["code"] == 4225
    client.post(f"/api/v1/app/coupon-templates/{second}/claim", headers={**member(7), **key("claim3")})
    mine = client.get("/api/v1/app/coupon-templates", headers=member(7)).json()["data"]["items"]
    assert all(i["claimed_by_me"] == 1 and i["can_claim"] is False for i in mine)
    tab = client.get("/api/v1/app/coupons?tab=available&limit=1&sort=valid_to", headers=member(7)).json()["data"]
    assert len(tab["items"]) == 1 and tab["has_more"]
    assert client.get("/api/v1/app/coupons/counts", headers=member(7)).json()["data"] == {
        "available": 2, "used": 0, "expired": 0}
    assert client.get("/api/v1/app/coupons?tab=used", headers=member(7)).json()["data"]["items"] == []
    assert client.get("/api/v1/app/coupons?tab=nope", headers=member(7)).json()["code"] == 4001
    cid = claimed["data"]["id"]
    assert client.get(f"/api/v1/app/coupons/{cid}", headers=member(7)).json()["data"]["id"] == cid
    assert client.get(f"/api/v1/app/coupons/{cid}", headers=member(8)).json()["code"] == 4040  # 他人券
    assert client.post("/api/v1/app/coupon-templates/999/claim", headers={**member(7), **key("x")}).json()["code"] == 4040


def test_redeem_code_and_brute_force_throttle(client):
    coupon.set_config(code_max_failures=3)
    tid = create_published(client, {**TEMPLATE, "channels": ["code"]})
    batch = client.post("/api/v1/admin/coupon-code-batches", json={"template_id": tid, "count": 2, "name": "门店活动"},
                        headers={**staff(), **key("codes")}).json()["data"]
    assert client.get(f"/api/v1/admin/coupon-code-batches/{batch['id']}/export",
                      headers=staff(perms=["coupon:read"])).json()["code"] == 4030
    exported = client.get(f"/api/v1/admin/coupon-code-batches/{batch['id']}/export", headers=staff())
    codes = [line.split(",")[0] for line in exported.text.strip().splitlines()[1:]]
    assert len(codes) == 2
    ok = client.post("/api/v1/app/coupons/redeem-code", json={"code": codes[0]}, headers={**member(7), **key("r1")})
    assert ok.json()["code"] == 0 and ok.json()["data"]["channel"] == "code"
    for n in range(3):
        resp = client.post("/api/v1/app/coupons/redeem-code", json={"code": "WRONGCODE999"},
                           headers={**member(8), **key(f"w{n}")})
        assert resp.status_code == 404 and resp.json()["code"] == 4040
    blocked = client.post("/api/v1/app/coupons/redeem-code", json={"code": codes[1]}, headers={**member(8), **key("w9")})
    assert blocked.status_code == 429 and blocked.json()["code"] == 4290 and "Retry-After" in blocked.headers
    listed = client.get(f"/api/v1/admin/coupon-code-batches?template_id={tid}", headers=staff()).json()["data"]
    assert listed["items"][0]["exported_at"] is not None


def test_available_for_cart_uses_server_cart(client):
    assert client.get("/api/v1/app/coupons/available-for-cart", headers=member(7)).json()["code"] == 5030
    tid = create_published(client)
    client.post(f"/api/v1/app/coupon-templates/{tid}/claim", headers={**member(7), **key("c")})
    coupon.set_cart_lines_provider(lambda db, uid: [CartLine(sku_id=1, product_id=1, qty=1, unit_price_cents=15000)])
    data = client.get("/api/v1/app/coupons/available-for-cart", headers=member(7)).json()["data"]
    assert data["best"] == data["usable"][0]["id"] and data["usable"][0]["discount_cents"] == 2000
    coupon.set_cart_lines_provider(lambda db, uid: [CartLine(sku_id=1, product_id=1, qty=1, unit_price_cents=5000)])
    data = client.get("/api/v1/app/coupons/available-for-cart", headers=member(7)).json()["data"]
    assert data["usable"] == [] and data["unusable"][0]["unusable"]["message"] == "还差50元可用"


def test_admin_issue_batch_member_list_export_and_revoke(client):
    tid = create_published(client)
    batch = client.post("/api/v1/admin/coupon-issue-batches",
                        json={"template_id": tid, "user_ids": [11, 12, 13], "reason": "老会员回馈"},
                        headers={**staff(perms=["coupon:issue"]), **key("batch")}).json()["data"]
    assert batch["status"] == "pending" and batch["total"] == 3
    with get_session_factory()() as db:
        jobs.process_issue_batches(db)
    detail = client.get(f"/api/v1/admin/coupon-issue-batches/{batch['id']}", headers=staff()).json()["data"]
    assert (detail["issued"], detail["status"]) == (3, "done")
    assert client.get("/api/v1/admin/coupon-issue-batches?status=done", headers=staff()).json()["data"]["total"] == 1
    rows = client.get(f"/api/v1/admin/user-coupons?template_id={tid}&status=unused&channel=admin&sort=-claimed_at",
                      headers=staff()).json()["data"]
    assert rows["total"] == 3
    by_member = client.get("/api/v1/admin/user-coupons?keyword=12", headers=staff()).json()["data"]
    assert [r["user_id"] for r in by_member["items"]] == [12]
    target = rows["items"][0]["id"]
    revoked = client.post(f"/api/v1/admin/user-coupons/{target}/revoke", json={"reason": "误发"},
                          headers={**staff(), **key("revoke")}).json()
    assert revoked["data"]["status"] == "revoked"
    again = client.post(f"/api/v1/admin/user-coupons/{target}/revoke", json={"reason": "误发"},
                        headers={**staff(), **key("revoke2")}).json()
    assert again["code"] == 4090
    csv_resp = client.get("/api/v1/admin/user-coupons/export?status=revoked", headers=staff())
    assert csv_resp.text.count("\r\n") == 2  # 表头 + 1 行
    assert client.get("/api/v1/admin/user-coupons/export", headers=staff(perms=["coupon:read"])).json()["code"] == 4030
    bad_range = client.get("/api/v1/admin/user-coupons?claimed_at_from=2026-10-30&claimed_at_to=2026-10-01",
                           headers=staff()).json()
    assert bad_range["code"] == 4001


def test_member_keyword_phone_with_real_identity_lookup(client):
    """真实 01-identity：手机号只存加密 + 哈希，后台按完整手机号走 find_user_by_phone 哈希查找。"""
    import identity.models  # noqa: F401
    from identity import attach_phone
    from identity.testing import create_member
    from luzback.db import Base

    db = _STATE["db"]
    Base.metadata.create_all(db.get_bind())
    alice, bob = create_member(db), create_member(db)
    attach_phone(db, alice.id, "13800138031", "sms")
    tid = create_published(client, k="real")
    client.post("/api/v1/admin/coupon-issue-batches", json={"template_id": tid, "user_ids": [alice.id, bob.id], "reason": "回馈"},
                headers={**staff(perms=["coupon:issue"]), **key("batch-real")})
    with get_session_factory()() as s:
        jobs.process_issue_batches(s)
    rows = client.get("/api/v1/admin/user-coupons?keyword=13800138031", headers=staff()).json()["data"]
    assert [r["user_id"] for r in rows["items"]] == [alice.id]
    assert client.get("/api/v1/admin/user-coupons?keyword=13800138032", headers=staff()).json()["data"]["total"] == 0
