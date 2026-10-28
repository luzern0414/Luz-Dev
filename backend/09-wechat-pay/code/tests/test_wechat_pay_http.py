"""HttpWechatPayClient 对接假服务器（httpx.MockTransport）：请求签名、应答验签、签名探测重试、错误分类、备域名、下载。"""
import gzip
from datetime import timedelta

import httpx
import pytest
from cryptography.hazmat.primitives import serialization

import wechat_pay.models
from luzback import AppError, ErrorCode
from luzback.testing import memory_session
from wechat_pay import service
from wechat_pay.client import HttpWechatPayClient, TransientWechatPayError, WechatPayApiError, WechatPayClient
from wechat_pay.fake import FAKE_MERCHANT_SERIAL, FAKE_PUBLIC_KEY_ID, FakeWechatPay, fake_keypair
from wechat_pay.testing import TEST_APPID, TEST_MCHID, test_config, wechat_pay_kit


@pytest.fixture()
def server(tmp_path):
    with wechat_pay_kit() as kit:
        pub = tmp_path / "wechatpay_pub.pem"
        pub.write_bytes(fake_keypair("platform").public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo))
        pem = fake_keypair("merchant").private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                                     serialization.NoEncryption()).decode()
        kit.http_config = test_config(client_mode="http", private_key_pem=pem, merchant_serial_no=FAKE_MERCHANT_SERIAL,
                                      public_key_id=FAKE_PUBLIC_KEY_ID, public_key_path=str(pub))
        kit.seen = []
        yield kit


def make_client(kit, probe_once=None, wrap=None):
    handler = kit.fake.http_handler(probe_once=probe_once)

    def record(request):
        kit.seen.append(request)
        return (wrap or handler)(request) if wrap is None else wrap(request, handler)

    http = httpx.Client(transport=httpx.MockTransport(record))
    return HttpWechatPayClient(kit.http_config, http=http, clock=kit.clock.ts)


def prepay_body(out_trade_no="P2610281200TESTHTTP01", total=100):
    return {"appid": TEST_APPID, "mchid": TEST_MCHID, "description": "测试", "out_trade_no": out_trade_no,
            "time_expire": "2026-10-28T12:15:00+08:00", "notify_url": "https://pay.example.org/n",
            "amount": {"total": total, "currency": "CNY"}, "payer": {"openid": "o-1"}}


def test_client_implements_protocol(server):
    assert isinstance(make_client(server), WechatPayClient)
    assert isinstance(FakeWechatPay(), WechatPayClient)


def test_signed_prepay_query_close_round_trip(server):
    client = make_client(server)
    prepay_id = client.jsapi_prepay(prepay_body())
    assert prepay_id.startswith("wx")
    request = server.seen[0]
    assert request.headers["Authorization"].startswith("WECHATPAY2-SHA256-RSA2048 ")
    assert request.headers["Wechatpay-Serial"] == FAKE_PUBLIC_KEY_ID and request.headers["User-Agent"]
    assert request.headers["Content-Type"] == "application/json"
    txn = client.query_by_out_trade_no("P2610281200TESTHTTP01")
    assert txn["trade_state"] == "NOTPAY" and server.seen[1].url.params["mchid"] == TEST_MCHID
    assert client.close("P2610281200TESTHTTP01") is None
    assert server.fake.orders["P2610281200TESTHTTP01"]["trade_state"] == "CLOSED"


def test_wrong_merchant_key_is_rejected_by_server(server):
    config = server.http_config.with_(private_key_pem=fake_keypair("someone-else").private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode())
    client = HttpWechatPayClient(config, http=httpx.Client(transport=httpx.MockTransport(server.fake.http_handler())),
                                 clock=server.clock.ts)
    with pytest.raises(WechatPayApiError) as error:
        client.jsapi_prepay(prepay_body())
    assert error.value.code == "SIGN_ERROR" and not error.value.retryable


def test_sign_probe_response_is_discarded_and_query_retried(server):
    service.set_client(make_client(server, probe_once={"/v3/pay/transactions/out-trade-no/P2610281200TESTHTTP01"}))
    server.fake.jsapi_prepay(prepay_body())
    with pytest.raises(TransientWechatPayError) as error:
        service.get_client().query_by_out_trade_no("P2610281200TESTHTTP01")
    assert error.value.code == "RESPONSE_SIGNATURE_INVALID"
    # 再来一次：微信不会对重试请求再做探测，经 resilient 的查单成功
    assert service._query("P2610281200TESTHTTP01")["trade_state"] == "NOTPAY"


def test_retry_through_resilient_for_query(server):
    from wechat_pay.config import set_config

    set_config(server.config.with_(query_attempts=3))
    service.set_client(make_client(server, probe_once={"/v3/pay/transactions/out-trade-no/P2610281200TESTHTTP01"}))
    server.fake.jsapi_prepay(prepay_body())
    assert service._query("P2610281200TESTHTTP01")["trade_state"] == "NOTPAY"
    assert len(server.seen) == 2


def test_error_classification(server):
    client = make_client(server)
    server.fake.fail_next("jsapi_prepay", "SYSTEM_ERROR")
    with pytest.raises(TransientWechatPayError):
        client.jsapi_prepay(prepay_body())
    server.fake.fail_next("jsapi_prepay", "PARAM_ERROR")
    with pytest.raises(WechatPayApiError) as error:
        client.jsapi_prepay(prepay_body())
    assert error.value.http_status == 400 and not error.value.retryable and error.value.request_id
    server.fake.fail_next("create_refund", "FREQUENCY_LIMITED")
    with pytest.raises(TransientWechatPayError) as limited:
        client.create_refund({"out_trade_no": "x", "out_refund_no": "R1", "amount": {"refund": 1, "total": 1, "currency": "CNY"}})
    assert limited.value.http_status == 429


def test_out_trade_no_reuse_with_different_params(server):
    client = make_client(server)
    client.jsapi_prepay(prepay_body())
    assert client.jsapi_prepay(prepay_body())  # 同参数：可重复下单
    with pytest.raises(WechatPayApiError) as error:
        client.jsapi_prepay(prepay_body(total=200))
    assert error.value.code == "OUT_TRADE_NO_USED"


def test_timeout_is_transient_and_connect_error_uses_backup_domain(server):
    def flaky(request, handler):
        if request.url.host == "api.mch.weixin.qq.com" and request.url.path.endswith("/jsapi"):
            raise httpx.ConnectError("down", request=request)
        return handler(request)

    client = make_client(server, wrap=flaky)
    assert client.jsapi_prepay(prepay_body())
    assert [r.url.host for r in server.seen] == ["api.mch.weixin.qq.com", "api2.mch.weixin.qq.com"]
    server.fake.timeout_next("query")
    with pytest.raises(TransientWechatPayError) as error:
        client.query_by_out_trade_no("P2610281200TESTHTTP01")
    assert error.value.code == "TIMEOUT"


def test_bill_download_skips_signature_and_returns_bytes(server):
    client = make_client(server)
    server.fake.jsapi_prepay(prepay_body())
    server.clock.advance(hours=-20)  # 让支付发生在 10-27
    server.fake.pay("P2610281200TESTHTTP01")
    server.clock.advance(hours=20)
    info = client.apply_trade_bill("2026-10-27")
    raw = client.download_bill(info["download_url"])
    content = gzip.decompress(raw)
    from wechat_pay import apiv3

    assert apiv3.sha1_hex(content) == info["hash_value"] and "P2610281200TESTHTTP01".encode() in content
    assert server.seen[-1].url.path == "/v3/billdownload/file" and "Authorization" in server.seen[-1].headers


def test_abnormal_refund_sends_wechatpay_serial_and_encrypted_fields(server):
    client = make_client(server)
    server.fake.jsapi_prepay(prepay_body())
    server.fake.pay("P2610281200TESTHTTP01")
    data = client.create_refund({"out_trade_no": "P2610281200TESTHTTP01", "out_refund_no": "RP01",
                                 "amount": {"refund": 100, "total": 100, "currency": "CNY"}})
    server.fake.settle_refund("RP01", "ABNORMAL")
    body = {"out_refund_no": "RP01", "type": "USER_BANK_CARD", "bank_type": "ICBC_DEBIT",
            "bank_account": client.encrypt_sensitive("6222000011112222"), "real_name": client.encrypt_sensitive("李四")}
    assert client.apply_abnormal_refund(data["refund_id"], body)["status"] == "PROCESSING"
    assert server.seen[-1].headers["Wechatpay-Serial"] == FAKE_PUBLIC_KEY_ID
    assert server.fake.abnormal_requests[0]["real_name"] == "李四"
    assert client.query_refund("RP01")["status"] == "PROCESSING"


def test_full_payment_flow_over_http_client(server):
    db = memory_session(wechat_pay.models)
    service.set_client(make_client(server))
    service.register_on_paid("http_order", lambda db, info: None)
    result = service.create_payment(db, "http_order", 1, 7, 1, 500, server.clock.now + timedelta(minutes=15), "HTTP",
                                    now=server.clock.now)
    server.fake.pay(result["out_trade_no"])
    headers, body = server.fake.payment_notification(result["out_trade_no"])
    assert service.handle_pay_notify(db, headers, body, now=server.clock.now).result == "processed"
    info = service.create_refund(db, result["payment_id"], 200, "部分退款", "admin:1", now=server.clock.now)
    assert info.status == "processing"
    db.close()


def test_production_config_checks(server):
    from wechat_pay.config import production_problems

    assert any("WECHAT_PAY_CLIENT" in p for p in production_problems(test_config()))
    good = server.http_config.with_(notify_url="https://pay.luz.example.cn/api/v1/webhooks/wechat/pay",
                                    refund_notify_url="https://pay.luz.example.cn/api/v1/webhooks/wechat/refund")
    assert production_problems(good) == []
    assert production_problems(good.with_(notify_url="https://pay.luz.example.cn/n?x=1"))
    assert production_problems(good.with_(notify_url="http://pay.luz.example.cn/n"))
    assert production_problems(good.with_(apiv3_key="short"))


def test_unavailable_channel_maps_to_upstream_error(server):
    db = memory_session(wechat_pay.models)

    def down(request, handler):
        raise httpx.ConnectError("down", request=request)

    service.set_client(make_client(server, wrap=down))
    with pytest.raises(AppError) as error:
        service.create_payment(db, "http_order", 1, 7, 1, 500, server.clock.now + timedelta(minutes=15), "HTTP",
                               now=server.clock.now)
    assert error.value.code == ErrorCode.UPSTREAM_FAILED
    db.close()
