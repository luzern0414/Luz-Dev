"""wechat_mp：接口调用、错误映射、access_token 中控（单飞 / 提前刷新 / 被动失效）、弹性、消息推送验签与加解密。"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import struct
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

import wechat_mp
from luzback import AppError, ErrorCode, UpstreamError, create_app
from luzback.db import get_session_factory
from luzback.resilience import CircuitOpenError
from luzback.testing import FakeClock, memory_session
from wechat_mp import (
    AccessTokenCache, DbTokenStore, FakeMiniProgram, FakeWechatServer, InvalidCode, PushCrypto, RateLimited,
    SubscriptionRefused, TemplateInvalid,
)
from wechat_mp.api import router

AES_KEY = "abcdefghijklmnopqrstuvwxyz0123456789ABCDEFG"  # 43 位
TOKEN = "pushtoken"


@pytest.fixture
def mp():
    return FakeMiniProgram()


# ---------------------------------------------------------------- 登录与 session_key

def test_code2session_success_and_code_single_use(mp):
    mp.add_login_code("c1", "o-1", unionid="u-1", session_key="sk-1")
    result = mp.code2session("c1")
    assert (result.openid, result.unionid, result.session_key) == ("o-1", "u-1", "sk-1")
    with pytest.raises(InvalidCode) as exc:
        mp.code2session("c1")
    assert exc.value.errcode == 40163 and exc.value.code == ErrorCode.UNAUTHORIZED


def test_code2session_invalid_and_blocked(mp):
    with pytest.raises(InvalidCode) as exc:
        mp.code2session("nope")
    assert exc.value.errcode == 40029
    mp.server.fail_next("/sns/jscode2session", 40226)
    with pytest.raises(wechat_mp.CodeBlocked) as exc:
        mp.code2session("x")
    assert exc.value.code == ErrorCode.FORBIDDEN


def test_code_expires_after_five_minutes():
    clock = FakeClock(1000.0)
    server = FakeWechatServer(clock=clock)
    mp = FakeMiniProgram(server, clock=clock)
    mp.add_login_code("c", "o")
    clock.advance(301)
    with pytest.raises(InvalidCode):
        mp.code2session("c")


def test_code2session_is_not_retried_on_timeout(mp):
    mp.add_login_code("c", "o")
    mp.server.fail_next("/sns/jscode2session", "timeout")
    with pytest.raises(UpstreamError):
        mp.code2session("c")
    assert mp.server.calls.count("/sns/jscode2session") == 1


def test_check_and_reset_session_key(mp):
    mp.add_login_code("c", "o", session_key="sk-a")
    mp.code2session("c")
    assert mp.check_session_key("o", "sk-a") is True
    assert mp.check_session_key("o", "wrong") is False
    new_key = mp.reset_session_key("o", "sk-a")
    assert new_key != "sk-a"
    assert mp.check_session_key("o", "sk-a") is False and mp.check_session_key("o", new_key) is True


def test_session_signature_is_hmac_sha256_of_empty_string():
    import hmac
    assert wechat_mp.session_signature("key") == hmac.new(b"key", b"", hashlib.sha256).hexdigest()


# ---------------------------------------------------------------- 手机号

def test_phone_number_with_openid_binding(mp):
    mp.add_phone_code("p1", "13800138000", openid="o-1")
    info = mp.get_phone_number("p1", openid="o-1")
    assert (info.pure_phone_number, info.country_code, info.watermark_appid) == ("13800138000", "86", mp.config.appid)
    mp.add_phone_code("p2", "13800138001", openid="o-1")
    with pytest.raises(InvalidCode):
        mp.get_phone_number("p2", openid="o-other")


# ---------------------------------------------------------------- access_token 中控

def test_token_cached_and_long_token_stored():
    memory_session(wechat_mp)
    mp = FakeMiniProgram(token_store=DbTokenStore(get_session_factory()))
    mp.add_phone_code("p", "13800138000")
    mp.get_phone_number("p")
    mp.add_phone_code("q", "13800138000")
    mp.get_phone_number("q")
    assert mp.server.calls.count("/cgi-bin/stable_token") == 1
    stored = mp.tokens.store.get(mp.tokens.key)
    assert len(stored.token) >= 512


def test_token_single_flight_under_concurrency():
    calls = []

    def fetch(force):
        calls.append(force)
        time.sleep(0.05)
        return "T" * 600, 7200

    cache = AccessTokenCache(fetch, key="k")
    with ThreadPoolExecutor(max_workers=20) as pool:
        tokens = list(pool.map(lambda _: cache.get(), range(40)))
    assert calls == [False]
    assert set(tokens) == {"T" * 600}


def test_token_refreshed_before_expiry_with_margin():
    clock = FakeClock(0.0)
    issued = iter([("A", 7200), ("B", 7200)])
    cache = AccessTokenCache(lambda force: next(issued), key="k", margin_seconds=300, clock=clock)
    assert cache.get() == "A"
    clock.advance(7200 - 301)
    assert cache.get() == "A"
    clock.advance(2)  # 剩余不足 5 分钟
    assert cache.get() == "B"


def test_token_short_remaining_rechecks_later_not_every_call():
    clock = FakeClock(0.0)
    calls = []

    def fetch(force):
        calls.append(force)
        return ("OLD", 200) if len(calls) == 1 else ("NEW", 7200)

    cache = AccessTokenCache(fetch, key="k", margin_seconds=300, clock=clock)
    assert cache.get() == "OLD"
    assert cache.get() == "OLD" and len(calls) == 1
    clock.advance(31)
    assert cache.get() == "NEW"


def test_token_refresh_failure_falls_back_to_valid_stale_token():
    clock = FakeClock(0.0)
    state = {"fail": False}

    def fetch(force):
        if state["fail"]:
            raise UpstreamError("down")
        return "A", 7200

    cache = AccessTokenCache(fetch, key="k", clock=clock)
    cache.get()
    clock.advance(7000)
    state["fail"] = True
    assert cache.get() == "A"
    clock.advance(300)
    with pytest.raises(UpstreamError):
        cache.get()


def test_invalid_token_is_invalidated_and_call_retried_once(mp):
    mp.add_phone_code("p", "13800138000")
    first = mp.access_token()
    mp.server.revoke_token(first)  # 例如另一系统强制刷新
    mp.server.current_token = None
    info = mp.get_phone_number("p")
    assert info.pure_phone_number == "13800138000"
    assert mp.access_token() != first
    assert mp.server.calls.count("/wxa/business/getuserphonenumber") == 2


def test_expired_token_42001_recovers(mp):
    mp.add_phone_code("p", "13800138000")
    mp.access_token()
    mp.server.expire_all_tokens()
    assert mp.get_phone_number("p").pure_phone_number == "13800138000"


def test_invalidate_uses_force_refresh_when_platform_returns_same_token():
    clock = FakeClock(0.0)
    calls = []

    def fetch(force):
        calls.append(force)
        return ("SAME", 7200) if not force else ("FORCED", 7200)

    cache = AccessTokenCache(fetch, key="k", clock=clock, force_min_interval=30)
    assert cache.get() == "SAME"
    assert cache.invalidate("SAME") == "FORCED"
    assert calls == [False, False, True]
    assert cache.invalidate("OTHER") == "FORCED"  # 缓存已是新 token：不再取
    cache.store.set("k", wechat_mp.TokenEntry("SAME", 7200, 6900))
    with pytest.raises(UpstreamError):  # 30 秒内不允许第二次强制刷新
        cache.invalidate("SAME")


def test_stable_token_wrong_secret_is_configuration_error():
    server = FakeWechatServer()
    mp = FakeMiniProgram(server, config=wechat_mp.WechatMpConfig(appid=server.appid, secret="bad", api_base="https://x"))
    with pytest.raises(wechat_mp.ConfigurationError):
        mp.access_token()


# ---------------------------------------------------------------- 订阅消息

def test_subscribe_send_consumes_quota_then_43101(mp):
    mp.grant_subscription("o-1", "TPL-ORDER", times=1)
    mp.send_subscribe_message("o-1", "TPL-ORDER", {"thing1": {"value": "剁椒鱼头"}, "amount2": {"value": "88.00元"}},
                              page="/pages/order/detail?id=1")
    assert mp.server.sent[0]["page"] == "pages/order/detail?id=1"
    assert mp.server.sent[0]["miniprogram_state"] == "formal" and mp.server.sent[0]["lang"] == "zh_CN"
    with pytest.raises(SubscriptionRefused) as exc:
        mp.send_subscribe_message("o-1", "TPL-ORDER", {"thing1": {"value": "x"}})
    assert exc.value.errcode == 43101 and exc.value.code == ErrorCode.BUSINESS_RULE


def test_subscribe_invalid_template_and_local_data_rules(mp):
    with pytest.raises(TemplateInvalid):
        mp.send_subscribe_message("o", "NOPE", {"thing1": {"value": "x"}})
    for bad in ({"thing1": {"value": "x" * 21}}, {"phrase1": {"value": "配送中啊啊啊"}}, {"number1": {"value": "1a"}},
                {"thing1": {"value": "a\nb"}}, {"bad": {"value": "x"}}, {}):
        with pytest.raises(AppError) as exc:
            wechat_mp.validate_subscribe_data(bad)
        assert exc.value.code == ErrorCode.BAD_REQUEST


def test_subscribe_send_not_retried_on_timeout(mp):
    mp.grant_subscription("o", "TPL-ORDER", 2)
    mp.access_token()
    mp.server.fail_next("/cgi-bin/message/subscribe/send", "timeout")
    with pytest.raises(UpstreamError):
        mp.send_subscribe_message("o", "TPL-ORDER", {"thing1": {"value": "x"}})
    assert mp.server.calls.count("/cgi-bin/message/subscribe/send") == 1


def test_rate_limit_errcode_maps_to_4290(mp):
    mp.grant_subscription("o", "TPL-ORDER")
    mp.access_token()
    mp.server.fail_next("/cgi-bin/message/subscribe/send", 45011)
    with pytest.raises(RateLimited) as exc:
        mp.send_subscribe_message("o", "TPL-ORDER", {"thing1": {"value": "x"}})
    assert exc.value.code == ErrorCode.RATE_LIMITED and exc.value.retry_after


# ---------------------------------------------------------------- 内容安全

def test_msg_sec_check_results(mp):
    assert mp.msg_sec_check("o", "你好", scene=1).passed
    assert mp.msg_sec_check("o", "违禁内容", scene=2).risky
    assert mp.msg_sec_check("o", "待审内容", scene=2).suggest == "review"
    with pytest.raises(AppError):
        mp.msg_sec_check("o", "x" * 2501, scene=1)
    with pytest.raises(AppError):
        mp.msg_sec_check("o", "x", scene=9)


def test_idempotent_call_retried_once_on_timeout_and_system_busy(mp):
    mp.access_token()
    mp.server.fail_next("/wxa/msg_sec_check", "timeout")
    assert mp.msg_sec_check("o", "hi", scene=1).passed
    mp.server.fail_next("/wxa/msg_sec_check", -1)
    assert mp.msg_sec_check("o", "hi", scene=1).passed
    assert mp.server.calls.count("/wxa/msg_sec_check") == 4


def test_real_timeout_is_enforced(mp):
    mp.access_token()
    mp.add_phone_code("p", "13800138000")
    mp.config = wechat_mp.WechatMpConfig(appid=mp.config.appid, secret=mp.config.secret, api_base=mp.config.api_base,
                                         timeout_s=0.05)
    mp.server.fail_next("/wxa/business/getuserphonenumber", lambda: time.sleep(0.3))
    with pytest.raises(UpstreamError):
        mp.get_phone_number("p")


def test_breaker_opens_after_repeated_transient_failures(mp):
    mp.access_token()
    for _ in range(10):
        mp.server.fail_next("/wxa/media_check_async", "http500")
        with pytest.raises(UpstreamError):
            mp.media_check_async("o", "https://cdn/x.jpg", 2, 1)
    with pytest.raises(CircuitOpenError) as exc:
        mp.media_check_async("o", "https://cdn/x.jpg", 2, 1)
    assert exc.value.code == ErrorCode.SERVICE_UNAVAILABLE


def test_media_check_async_and_event_parsing(mp):
    trace_id = mp.media_check_async("o", "https://cdn/x.jpg", media_type=2, scene=1)
    event = wechat_mp.parse_media_check_event(mp.media_result(trace_id, "risky", 20002))
    assert event.trace_id == trace_id and event.risky and not event.passed
    failed = wechat_mp.parse_media_check_event(mp.media_result(trace_id, "pass", errcode=-1008))
    assert not failed.valid and not failed.passed


# ---------------------------------------------------------------- 小程序码

def test_unlimited_qrcode(mp):
    assert mp.get_unlimited_qrcode("t=12&s=3", page="pages/index/index").startswith(b"\x89PNG")
    with pytest.raises(AppError) as exc:
        mp.get_unlimited_qrcode("x" * 33)
    assert exc.value.code == ErrorCode.BAD_REQUEST
    with pytest.raises(AppError):
        mp.get_unlimited_qrcode("中文")
    with pytest.raises(AppError):
        mp.get_unlimited_qrcode("a", page="/pages/index/index")
    mp.server.fail_next("/wxa/getwxacodeunlimit", 41030)
    with pytest.raises(wechat_mp.InvalidArgument):
        mp.get_unlimited_qrcode("ok")


def test_get_client_requires_configuration(monkeypatch):
    wechat_mp.set_client(None)
    monkeypatch.delenv("WECHAT_APPID", raising=False)
    wechat_mp.reload_config()
    with pytest.raises(AppError) as exc:
        wechat_mp.get_client()
    assert exc.value.code == ErrorCode.UPSTREAM_FAILED
    fake = FakeMiniProgram()
    wechat_mp.set_client(fake)
    assert wechat_mp.get_client() is fake
    wechat_mp.set_client(None)


# ---------------------------------------------------------------- 消息推送：签名与 AES 向量

def test_url_signature_vector():
    expected = hashlib.sha1("".join(sorted(["pushtoken", "1409659813", "1372623149"])).encode()).hexdigest()
    assert wechat_mp.url_signature("pushtoken", "1409659813", "1372623149") == expected
    assert wechat_mp.verify_url_signature("pushtoken", expected, "1409659813", "1372623149")
    assert not wechat_mp.verify_url_signature("pushtoken", expected, "1409659814", "1372623149")
    assert not wechat_mp.verify_url_signature("", expected, "1409659813", "1372623149")


def _reference_encrypt(key43: str, appid: str, plaintext: bytes, random16: bytes) -> str:
    """独立参考实现（cryptography 自带 PKCS7(256)）：用于交叉核对 PushCrypto。"""
    from cryptography.hazmat.primitives import padding
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    key = base64.b64decode(key43 + "=")
    raw = random16 + struct.pack(">I", len(plaintext)) + plaintext + appid.encode()
    padder = padding.PKCS7(256).padder()
    padded = padder.update(raw) + padder.finalize()
    enc = Cipher(algorithms.AES(key), modes.CBC(key[:16])).encryptor()
    return base64.b64encode(enc.update(padded) + enc.finalize()).decode()


def test_aes_vector_matches_reference_and_round_trips():
    crypto = PushCrypto(TOKEN, AES_KEY, "wx-fake-appid")
    random16 = b"0123456789abcdef"
    body = '{"Event":"wxa_media_check","trace_id":"t1"}'
    ours = crypto.encrypt(body, random16)
    assert ours == _reference_encrypt(AES_KEY, "wx-fake-appid", body.encode(), random16)
    assert len(base64.b64decode(ours)) % 32 == 0
    assert crypto.decrypt(ours) == body
    reply = crypto.encrypt_reply("success", timestamp="1700000000", nonce="n1", random16=random16)
    assert reply["MsgSignature"] == wechat_mp.msg_signature(TOKEN, "1700000000", "n1", reply["Encrypt"])
    assert crypto.verify_and_decrypt(reply["MsgSignature"], "1700000000", "n1", reply["Encrypt"]) == "success"


def test_aes_rejects_wrong_appid_bad_signature_and_tampering():
    crypto = PushCrypto(TOKEN, AES_KEY, "wx-fake-appid")
    other = PushCrypto(TOKEN, AES_KEY, "wx-other")
    with pytest.raises(AppError) as exc:
        crypto.decrypt(other.encrypt("x"))
    assert exc.value.code == ErrorCode.UNAUTHORIZED
    enc = crypto.encrypt("hello")
    with pytest.raises(AppError):
        crypto.verify_and_decrypt("0" * 40, "1", "n", enc)
    raw = bytearray(base64.b64decode(enc))
    raw[-1] ^= 0xFF
    with pytest.raises(AppError):
        crypto.decrypt(base64.b64encode(bytes(raw)).decode())
    with pytest.raises(ValueError):
        PushCrypto(TOKEN, "short", "wx")


def test_normalize_list_and_xml_parsing():
    assert wechat_mp.normalize_list({"a": 1}) == [{"a": 1}]
    assert wechat_mp.normalize_list([1, 2]) == [1, 2]
    assert wechat_mp.normalize_list(None) == []
    data = wechat_mp.parse_body("<xml><Event>e</Event><List><TemplateId>a</TemplateId></List>"
                                "<List><TemplateId>b</TemplateId></List></xml>")
    assert [item["TemplateId"] for item in wechat_mp.normalize_list(data["List"])] == ["a", "b"]
    with pytest.raises(AppError):
        wechat_mp.parse_body('<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "b">]><xml>&a;</xml>')


# ---------------------------------------------------------------- 消息推送：HTTP

@pytest.fixture
def push_app(monkeypatch, tmp_path):
    monkeypatch.setenv("WECHAT_APPID", "wx-fake-appid")
    monkeypatch.setenv("WECHAT_SECRET", "fake-secret")
    monkeypatch.setenv("WECHAT_MP_PUSH_TOKEN", TOKEN)
    monkeypatch.setenv("WECHAT_MP_PUSH_AES_KEY", AES_KEY)
    wechat_mp.reload_config()
    from luzback.testing import make_test_app

    app = make_test_app([router], wechat_mp, url=f"sqlite:///{tmp_path / 'push.db'}")
    received = []

    def handler(db, message):
        received.append(message)

    wechat_mp.register_push_handler("wxa_media_check", handler)
    yield TestClient(app), received
    wechat_mp.unregister_push_handler("wxa_media_check", handler)
    monkeypatch.delenv("WECHAT_MP_PUSH_TOKEN")
    monkeypatch.delenv("WECHAT_MP_PUSH_AES_KEY")
    wechat_mp.reload_config()


def _signed_query(ts="1700000000", nonce="n1"):
    return {"signature": wechat_mp.url_signature(TOKEN, ts, nonce), "timestamp": ts, "nonce": nonce}


def test_push_url_verification(push_app):
    client, _ = push_app
    resp = client.get("/api/v1/webhooks/wechat-mp/push", params={**_signed_query(), "echostr": "hello"})
    assert resp.status_code == 200 and resp.text == "hello"
    bad = client.get("/api/v1/webhooks/wechat-mp/push", params={"signature": "x", "timestamp": "1", "nonce": "n", "echostr": "e"})
    assert bad.status_code == 401 and bad.json()["code"] == 4010


def test_push_security_mode_json_dispatches_once(push_app):
    client, received = push_app
    crypto = PushCrypto(TOKEN, AES_KEY, "wx-fake-appid")
    inner = json.dumps({"FromUserName": "o-1", "CreateTime": 1700000000, "MsgType": "event", "Event": "wxa_media_check",
                        "trace_id": "t-1", "errcode": 0, "result": {"suggest": "pass", "label": 100}})
    encrypt = crypto.encrypt(inner)
    q = {**_signed_query(), "encrypt_type": "aes", "msg_signature": wechat_mp.msg_signature(TOKEN, "1700000000", "n1", encrypt)}
    for _ in range(2):  # 平台重推：只处理一次
        resp = client.post("/api/v1/webhooks/wechat-mp/push", params=q, content=json.dumps({"Encrypt": encrypt}))
        assert resp.status_code == 200 and resp.text == "success"
    assert len(received) == 1 and received[0]["trace_id"] == "t-1"


def test_push_security_mode_xml_and_rejections(push_app):
    client, received = push_app
    crypto = PushCrypto(TOKEN, AES_KEY, "wx-fake-appid")
    inner = ("<xml><FromUserName>o-2</FromUserName><CreateTime>1700000001</CreateTime><MsgType>event</MsgType>"
             "<Event>wxa_media_check</Event><trace_id>t-2</trace_id><errcode>0</errcode>"
             "<result><suggest>risky</suggest><label>20002</label></result></xml>")
    encrypt = crypto.encrypt(inner)
    q = {**_signed_query(), "encrypt_type": "aes", "msg_signature": wechat_mp.msg_signature(TOKEN, "1700000000", "n1", encrypt)}
    body = f"<xml><ToUserName>gh</ToUserName><Encrypt><![CDATA[{encrypt}]]></Encrypt></xml>"
    assert client.post("/api/v1/webhooks/wechat-mp/push", params=q, content=body).text == "success"
    assert wechat_mp.parse_media_check_event(received[-1]).risky
    bad_sig = client.post("/api/v1/webhooks/wechat-mp/push", params={**q, "msg_signature": "0" * 40}, content=body)
    assert bad_sig.json()["code"] == 4010
    plain = client.post("/api/v1/webhooks/wechat-mp/push", params=_signed_query(), content=inner)
    assert plain.json()["code"] == 4010  # 安全模式下拒绝明文推送
    assert len(received) == 1


def test_db_token_store_shared_between_instances():
    memory_session(wechat_mp)
    store = DbTokenStore(get_session_factory())
    store.set("k", wechat_mp.TokenEntry("X" * 512, 10.0, 5.0))
    store.set("k", wechat_mp.TokenEntry("Y" * 700, 20.0, 15.0))
    assert store.get("k").token == "Y" * 700


def test_distributed_lock_waits_for_other_instance():
    from luzback.scheduler import InMemoryLock

    lock = InMemoryLock()
    store = wechat_mp.MemoryTokenStore()
    lock.acquire("lock:k", 10)  # 另一实例持锁
    waited = []

    def wait(seconds):
        waited.append(seconds)
        store.set("k", wechat_mp.TokenEntry("FROM-OTHER", time.time() + 7200, time.time() + 6900))

    cache = AccessTokenCache(lambda force: ("MINE", 7200), key="k", store=store, lock=lock, wait=wait)
    assert cache.get() == "FROM-OTHER" and waited


def test_env_config_validation(monkeypatch):
    monkeypatch.setenv("WECHAT_MP_STATE", "bogus")
    with pytest.raises(ValueError):
        wechat_mp.reload_config()
    monkeypatch.delenv("WECHAT_MP_STATE")
    wechat_mp.reload_config()


def test_threads_share_one_client_safely(mp):
    for i in range(20):
        mp.add_phone_code(f"p{i}", f"138001380{i:02d}")
    with ThreadPoolExecutor(max_workers=10) as pool:
        phones = list(pool.map(lambda i: mp.get_phone_number(f"p{i}").pure_phone_number, range(20)))
    assert len(set(phones)) == 20
    assert mp.server.calls.count("/cgi-bin/stable_token") == 1
    assert threading.active_count() >= 1 and os.getpid()
