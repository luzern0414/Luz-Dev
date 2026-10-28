"""identity：登录、令牌轮换、登出、短信绑定、资料、注销、冻结、服务签名。"""
from __future__ import annotations

import time
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

import identity
from identity import service
from identity.api import router, set_code2session, set_phone_resolver, set_sms_sender
from luzback import AppError, ErrorCode, create_app, get_settings, utcnow
from luzback.security import service_signature
from luzback.testing import memory_session


@pytest.fixture
def db():
    session = memory_session(identity, "luzback.audit")
    service.breakers.get("wechat_code2session").reset()
    yield session
    session.close()


def wx(code: str) -> dict:
    return {"openid": f"openid-{code}", "unionid": None}


def test_login_creates_user_once_and_issues_tokens(db):
    first = service.login_with_code(db, "c1", wx)
    second = service.login_with_code(db, "c1", wx)
    assert first["user_id"] == second["user_id"]
    assert service.current_user_id(first["access_token"]) == first["user_id"]
    assert db.execute(select(identity.User)).scalars().one().last_login_at is not None


def test_login_upstream_failure_is_5020_and_trips_breaker(db):
    def broken(code):
        raise ConnectionError("wechat down")

    for _ in range(5):
        with pytest.raises(AppError) as exc:
            service.login_with_code(db, "c", broken)
        assert exc.value.code == ErrorCode.UPSTREAM_FAILED
    assert service.breakers.get("wechat_code2session").snapshot()["recent_failures"] >= 5


def test_refresh_rotation_rejects_replay_and_logout_revokes(db):
    tokens = service.login_with_code(db, "c2", wx)
    rotated = service.refresh_tokens(db, tokens["refresh_token"])
    with pytest.raises(AppError) as exc:
        service.refresh_tokens(db, tokens["refresh_token"])
    assert exc.value.code == ErrorCode.UNAUTHORIZED
    service.logout(db, tokens["user_id"])
    with pytest.raises(AppError):
        service.refresh_tokens(db, rotated["refresh_token"])


def test_frozen_and_deactivated_users_cannot_login(db):
    tokens = service.login_with_code(db, "c3", wx)
    service.freeze(db, tokens["user_id"], True, staff_id=1, reason="fraud")
    with pytest.raises(AppError) as exc:
        service.login_with_code(db, "c3", wx)
    assert exc.value.code == ErrorCode.FORBIDDEN
    service.freeze(db, tokens["user_id"], False, staff_id=1, reason="cleared")
    claims = service.decode_access(service.login_with_code(db, "c3", wx)["access_token"])
    service.deactivate(db, tokens["user_id"], claims, reason="bye")
    with pytest.raises(AppError) as exc:
        service.login_with_code(db, "c3", wx)
    assert exc.value.code == ErrorCode.FORBIDDEN
    with pytest.raises(AppError) as exc:
        service.deactivate(db, tokens["user_id"], claims)
    assert exc.value.code in (ErrorCode.CONFLICT, ErrorCode.FORBIDDEN)


def test_deactivate_requires_fresh_token(db):
    tokens = service.login_with_code(db, "c4", wx)
    stale = {"sub": str(tokens["user_id"]), "iat": time.time() - get_settings().fresh_window_seconds - 10}
    with pytest.raises(AppError) as exc:
        service.deactivate(db, tokens["user_id"], stale)
    assert exc.value.data == {"reason": "fresh_required"}


def test_sms_bind_flow_rate_limit_attempts_and_uniqueness(db):
    sent: list[tuple[str, str]] = []
    sender = lambda phone, code: sent.append((phone, code))  # noqa: E731
    now = utcnow()
    a = service.login_with_code(db, "a", wx)["user_id"]
    b = service.login_with_code(db, "b", wx)["user_id"]
    service.send_sms_code(db, "13800001234", "bind", sender, now=now)
    with pytest.raises(AppError) as exc:  # 1 分钟内重发
        service.send_sms_code(db, "13800001234", "bind", sender, now=now + timedelta(seconds=30))
    assert exc.value.code == ErrorCode.RATE_LIMITED and exc.value.retry_after == 60
    with pytest.raises(AppError):
        service.send_sms_code(db, "12345", "bind", sender, now=now)
    code = sent[-1][1]
    for i in range(5):  # 错 5 次后锁定
        with pytest.raises(AppError) as exc:
            service.bind_phone_with_code(db, a, "13800001234", "000000", now=now)
        assert exc.value.code == ErrorCode.BAD_REQUEST and exc.value.data["attempts_left"] == 4 - i
    with pytest.raises(AppError) as exc:
        service.bind_phone_with_code(db, a, "13800001234", code, now=now)
    assert exc.value.code == ErrorCode.LIMIT_REACHED
    service.send_sms_code(db, "13800001234", "bind", sender, now=now + timedelta(minutes=2))
    code = sent[-1][1]
    with pytest.raises(AppError):  # 过期
        service.bind_phone_with_code(db, a, "13800001234", code, now=now + timedelta(minutes=10))
    result = service.bind_phone_with_code(db, a, "13800001234", code, now=now + timedelta(minutes=3))
    assert result == {"phone": "138****1234", "phone_verified": True}
    assert service.profile(db, a)["phone"] == "138****1234"
    assert service.profile(db, a, reveal_phone=True)["phone"] == "13800001234"
    service.send_sms_code(db, "13800001234", "bind", sender, now=now + timedelta(minutes=5))
    with pytest.raises(AppError) as exc:  # 另一账号绑定同号
        service.bind_phone_with_code(db, b, "13800001234", sent[-1][1], now=now + timedelta(minutes=5))
    assert exc.value.code == ErrorCode.CONFLICT
    # 每日上限
    for i in range(20):
        try:
            service.send_sms_code(db, "13900000000", "bind", sender, now=now + timedelta(minutes=2 * i))
        except AppError as err:
            assert err.code == ErrorCode.LIMIT_REACHED and i >= get_settings().sms_daily_limit
            break
    else:
        pytest.fail("daily limit not enforced")


def test_wechat_phone_bind_and_profile_validation(db):
    user_id = service.login_with_code(db, "p", wx)["user_id"]
    assert service.bind_phone_from_wechat(db, user_id, "pc", lambda c: "13712345678")["phone"] == "137****5678"
    with pytest.raises(AppError):
        service.bind_phone_from_wechat(db, user_id, "pc", lambda c: "bad")
    updated = service.update_profile(db, user_id, nickname=" 小花 ", avatar_url="https://cdn/x.png", gender=2, birthday="1990-01-02")
    assert updated["nickname"] == "小花" and updated["gender"] == 2
    for bad in ({"nickname": ""}, {"avatar_url": "http://insecure"}, {"gender": 5}, {"birthday": "1990/01/02"}):
        with pytest.raises(AppError):
            service.update_profile(db, user_id, **bad)


def test_http_routes_and_service_signature(db):
    set_code2session(wx)
    set_sms_sender(lambda phone, code: None)
    set_phone_resolver(lambda code: "13611112222")
    client = TestClient(create_app([router]), raise_server_exceptions=False)
    login = client.post("/api/v1/app/auth/login", json={"code": "h1"}).json()
    assert login["code"] == 0
    headers = {"Authorization": f"Bearer {login['data']['access_token']}"}
    assert client.get("/api/v1/app/me", headers=headers).json()["data"]["phone_verified"] is False
    assert client.get("/api/v1/app/me").json()["code"] == 4010
    assert client.post("/api/v1/app/auth/phone/bind", json={"phone_code": "x"}, headers=headers).json()["data"]["phone"] == "136****2222"
    assert client.put("/api/v1/app/me", json={"nickname": "阿里"}, headers=headers).json()["data"]["nickname"] == "阿里"
    assert client.post("/api/v1/app/auth/logout", headers=headers).json()["code"] == 0
    assert client.post("/api/v1/app/auth/refresh", json={"refresh_token": login["data"]["refresh_token"]}).json()["code"] == 4010
    # 服务间签名依赖
    from identity.api import service_call
    from fastapi import APIRouter, Depends

    r = APIRouter()

    @r.post("/internal")
    def internal(svc: str | None = Depends(service_call)):
        return {"code": 0, "message": "ok", "data": svc}

    app = create_app([r])
    c = TestClient(app, raise_server_exceptions=False)
    assert c.post("/internal").json()["data"] is None
    ts = str(int(time.time()))
    sig = service_signature("dev-agent-secret", "POST", "/internal", ts, "n-1", b"")
    good = c.post("/internal", headers={"X-Service": "agent", "X-Timestamp": ts, "X-Nonce": "n-1", "X-Signature": sig})
    assert good.json()["data"] == "agent"
    replay = c.post("/internal", headers={"X-Service": "agent", "X-Timestamp": ts, "X-Nonce": "n-1", "X-Signature": sig})
    assert replay.json()["code"] == 4010
    assert c.post("/internal", headers={"X-Service": "ghost"}).json()["code"] == 4010
