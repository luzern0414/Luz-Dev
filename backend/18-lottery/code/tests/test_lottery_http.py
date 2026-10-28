"""HTTP：路由、鉴权 4010、权限 4030、参数 4001、幂等键、信封、完整流程。"""
from datetime import timedelta

import pytest
from fastapi import Depends
from fastapi.security import HTTPBearer
from fastapi.testclient import TestClient

import lottery
from lottery import deps, hooks
from lottery.api import routers
from lottery.hooks import FixedRandom
from admin_rbac.testing import staff_headers
from lottery_testkit import HIT, NOW, key, prizes, setup_hooks
from luzback import AppError, ErrorCode, create_token, decode_token, get_settings, utcnow
from luzback.db import get_session_factory
from luzback.testing import make_test_app

ALL = ["lottery:read", "lottery:write", "lottery:publish", "lottery:grant", "lottery:fulfil", "lottery:export"]


def _member_override(creds=Depends(HTTPBearer(auto_error=False))) -> int:
    """会员端用框架令牌直接解析会员 id（会员 id 固定好写断言）；生产走 identity（见 test_member_session_revoked）。"""
    if creds is None:
        raise AppError(ErrorCode.UNAUTHORIZED, "missing bearer token")
    s = get_settings()
    return int(decode_token(creds.credentials, s.jwt_secret, kind="access", issuer=s.jwt_issuer)["sub"])


_DB: dict = {}
_tokens: dict = {}


@pytest.fixture()
def client():
    setup_hooks()
    app = make_test_app(routers, lottery, "admin_rbac.models", "identity.models")
    app.dependency_overrides[deps.current_member] = _member_override
    _DB["db"] = get_session_factory()()
    _tokens.clear()
    with TestClient(app) as c:
        yield c
    _DB.pop("db").close()
    setup_hooks()


def member(uid: int) -> dict:
    s = get_settings()
    return {"Authorization": f"Bearer {create_token(str(uid), s.jwt_secret, 3600, kind='access', issuer=s.jwt_issuer)}"}


def staff(perms=ALL, sid: int = 1, stores=None) -> dict:
    """真实 admin_rbac 会话令牌（同一组权限 / 门店复用同一员工）。"""
    k = (tuple(perms), sid, tuple(stores) if stores is not None else None)
    db = _DB["db"]
    if k not in _tokens:
        _tokens[k] = staff_headers(db, *perms, store_ids=stores)
    return _tokens[k]


def idem(headers: dict) -> dict:
    return {**headers, "Idempotency-Key": key("http")}


def body(**over):
    data = {"name": "转盘", "kind": "grid", "start_at": (NOW - timedelta(hours=1)).isoformat(),
            "end_at": (NOW + timedelta(days=3650)).isoformat(), "rules_text": "规则全文", "free_daily_chances": 1,
            "budget_cents": 1_000_000, "prizes": prizes()}
    data.update(over)
    return data


def create_published(client) -> dict:
    r = client.post("/api/v1/admin/lottery/activities", json=body(), headers=idem(staff()))
    assert r.status_code == 200, r.text
    aid = r.json()["data"]["id"]
    r = client.post(f"/api/v1/admin/lottery/activities/{aid}/publish", headers=idem(staff()))
    assert r.json()["data"]["status"] == "published", r.text
    return r.json()["data"]


def test_auth_permission_and_validation(client):
    r = client.post("/api/v1/app/lottery/activities/1/draw", json={})
    assert r.status_code == 401 and r.json()["code"] == 4010
    assert client.get("/api/v1/admin/lottery/activities").json()["code"] == 4010
    r = client.get("/api/v1/admin/lottery/activities", headers=staff(perms=["order:read"]))
    assert r.status_code == 403 and r.json()["code"] == 4030
    r = client.post("/api/v1/admin/lottery/activities", json=body(), headers=staff())  # 无幂等键
    assert r.json()["code"] == 4001
    bad = body()
    bad["prizes"][0]["weight_bp"] = 0
    r = client.post("/api/v1/admin/lottery/activities", json=bad, headers=idem(staff()))
    assert r.status_code == 400 and r.json()["code"] == 4001 and r.json()["data"]["errors"]
    r = client.get("/api/v1/admin/lottery/records?nope=1", headers=staff())
    assert r.json()["code"] == 4001
    r = client.post("/api/v1/admin/lottery/activities", json=body(extra_field=1), headers=idem(staff()))
    assert r.json()["code"] == 4001


def test_cap_over_50000_yuan_rejected(client):
    over = prizes()
    over[2]["value_cents"] = 5_000_001
    r = client.post("/api/v1/admin/lottery/activities", json=body(prizes=over, budget_cents=20_000_000),
                    headers=idem(staff()))
    assert r.status_code == 422 and r.json()["code"] == 4220


def test_full_flow(client):
    act = create_published(client)
    aid = act["id"]
    # 顾客端：详情公示概率
    detail = client.get(f"/api/v1/app/lottery/activities/{aid}").json()
    assert detail["code"] == 0 and detail["message"] == "ok"
    assert [p["probability"] for p in detail["data"]["prizes"]] == ["10.00%", "20.00%", "1.00%", "9.00%", "60.00%"]
    assert "ref_id" not in detail["data"]["prizes"][0] and "budget_cents" not in detail["data"]
    lst = client.get("/api/v1/app/lottery/activities").json()["data"]
    assert [a["id"] for a in lst["items"]] == [aid] and "has_more" in lst
    chances = client.get(f"/api/v1/app/lottery/activities/{aid}/chances", headers=member(5)).json()["data"]
    assert chances["available"] == 1 and chances["open"] is True
    # 抽奖（固定命中实物），幂等重放
    hooks.set_random_source(FixedRandom(HIT[3]))
    h = idem(member(5))
    r1 = client.post(f"/api/v1/app/lottery/activities/{aid}/draw", json={}, headers=h).json()
    r2 = client.post(f"/api/v1/app/lottery/activities/{aid}/draw", json={}, headers=h).json()
    assert r1 == r2 and r1["data"]["prize"]["kind"] == "physical"
    rid = r1["data"]["id"]
    again = client.post(f"/api/v1/app/lottery/activities/{aid}/draw", json={}, headers=idem(member(5))).json()
    assert again["code"] == 4225 and again["data"]["reason"] == "no_chance"
    # 我的记录；他人看不到
    mine = client.get("/api/v1/app/lottery/records", headers=member(5)).json()["data"]
    assert [i["id"] for i in mine["items"]] == [rid]
    assert client.get(f"/api/v1/app/lottery/records/{rid}", headers=member(6)).json()["code"] == 4040
    # 领奖 → 后台发货
    claim = {"name": "张三", "phone": "13800138000", "address": "上海市浦东新区某路 1 号"}
    r = client.post(f"/api/v1/app/lottery/records/{rid}/claim", json=claim, headers=idem(member(5))).json()
    assert r["data"]["delivery_status"] == "claimed"
    contact = client.get(f"/api/v1/admin/lottery/records/{rid}/contact", headers=staff()).json()["data"]
    assert contact["phone"] == "13800138000"
    r = client.post(f"/api/v1/admin/lottery/records/{rid}/ship", json={"carrier": "SF", "tracking_no": "SF1234567"},
                    headers=idem(staff())).json()
    assert r["data"]["delivery_status"] == "shipped"
    # 公示（> 500 元，脱敏）
    winners = client.get(f"/api/v1/app/lottery/activities/{aid}/winners").json()["data"]["items"]
    assert winners[0]["member"] == "用户****0005" and winners[0]["delivery_status"] == "shipped"
    # 后台列表 / 详情 / 导出 / 统计 / 模拟
    page = client.get(f"/api/v1/admin/lottery/records?activity_id={aid}&delivery_status=shipped&sort=-created_at",
                      headers=staff()).json()["data"]
    assert page["total"] == 1 and page["page"] == 1
    rec = client.get(f"/api/v1/admin/lottery/records/{rid}", headers=staff()).json()["data"]
    assert rec["replay_ok"] is True and rec["claim"]["phone"] == "138****8000"
    csv = client.get(f"/api/v1/admin/lottery/records/export?activity_id={aid}", headers=staff())
    assert csv.status_code == 200 and csv.headers["content-type"].startswith("text/csv")
    assert "SF1234567" in csv.content.decode("utf-8-sig") and "13800138000" not in csv.content.decode("utf-8-sig")
    assert client.get(f"/api/v1/admin/lottery/records/export", headers=staff(perms=["lottery:read"])).json()["code"] \
        == 4030
    stats = client.get(f"/api/v1/admin/lottery/activities/{aid}/stats", headers=staff()).json()["data"]
    assert stats["draws"] == 1 and stats["cost_cents"] == 80_000
    sim = client.post(f"/api/v1/admin/lottery/activities/{aid}/simulate", json={"n": 10000, "seed": 1},
                      headers=staff()).json()["data"]
    assert sim["n"] == 10000
    acts = client.get("/api/v1/admin/lottery/activities?status=published,running&keyword=转盘",
                      headers=staff()).json()["data"]
    assert acts["total"] == 1


def test_admin_edits_after_publish(client):
    act = create_published(client)
    aid = act["id"]
    r = client.patch(f"/api/v1/admin/lottery/activities/{aid}", json={"name": "改名"}, headers=idem(staff())).json()
    assert r["code"] == 4090
    r = client.put(f"/api/v1/admin/lottery/activities/{aid}/prizes", json={"prizes": prizes()},
                   headers=idem(staff())).json()
    assert r["code"] == 4090
    r = client.patch(f"/api/v1/admin/lottery/activities/{aid}", json={"free_daily_chances": 3},
                     headers=idem(staff())).json()
    assert r["code"] == 0 and r["data"]["amendments"][0]["field"] == "free_daily_chances"
    pid = act["prizes"][0]["id"]
    r = client.post(f"/api/v1/admin/lottery/activities/{aid}/prizes/{pid}/stock", json={"add": 5},
                    headers=idem(staff())).json()
    assert r["data"]["prizes"][0]["stock_total"] == 105
    r = client.post(f"/api/v1/admin/lottery/activities/{aid}/grants",
                    json={"member_id": 9, "chances": 2, "reason": "补偿"}, headers=idem(staff())).json()
    assert r["data"]["chances"] == 2
    grants = client.get(f"/api/v1/admin/lottery/grants?activity_id={aid}", headers=staff()).json()["data"]
    assert grants["total"] == 1
    r = client.post(f"/api/v1/admin/lottery/activities/{aid}/end", json={"reason": "x"}, headers=idem(staff())).json()
    assert r["code"] == 4220  # 未公示可提前结束
    r = client.delete(f"/api/v1/admin/lottery/activities/{aid}", headers=idem(staff())).json()
    assert r["code"] == 4090


def test_store_scope_over_http(client):
    r = client.post("/api/v1/admin/lottery/activities", json=body(store_id=3), headers=idem(staff(stores=[4])))
    assert r.status_code == 403 and r.json()["code"] == 4031
    r = client.post("/api/v1/admin/lottery/activities", json=body(store_id=4), headers=idem(staff(stores=[4])))
    aid = r.json()["data"]["id"]
    assert client.get(f"/api/v1/admin/lottery/activities/{aid}", headers=staff(stores=[5])).json()["code"] == 4031


def test_forged_admin_claims_rejected(client):
    """令牌里自带 perms 声明不算数：没有服务端会话一律 4010（失败即关闭）。"""
    s = get_settings()
    forged = create_token("1", s.jwt_secret, 3600, kind="admin", extra={"perms": ALL, "sid": 999, "rv": 999},
                          issuer=s.jwt_issuer)
    r = client.get("/api/v1/admin/lottery/activities", headers={"Authorization": f"Bearer {forged}"})
    assert r.json()["code"] == 4010


def test_wildcard_role_and_member_session_revoked(client):
    db = _DB["db"]
    assert client.get("/api/v1/admin/lottery/activities", headers=staff_headers(db, "lottery:*")).json()["code"] == 0
    # 顾客端默认依赖走 identity：会话被吊销后立即 4010
    from identity import service as ids
    from identity.testing import member_tokens

    client.app.dependency_overrides.pop(deps.current_member)
    pair = member_tokens(db)
    h = {"Authorization": f"Bearer {pair['access_token']}"}
    assert client.get("/api/v1/app/lottery/records", headers=h).json()["code"] == 0
    ids.revoke_sessions(db, pair["user_id"], "test", utcnow())
    db.commit()
    assert client.get("/api/v1/app/lottery/records", headers=h).json()["code"] == 4010
