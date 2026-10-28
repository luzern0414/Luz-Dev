"""APIv3 签名 / 验签 / 加解密 / 事实核对与纯函数规则。官方测试向量来自 4012365336（官方公开的测试私钥）。"""
import base64
import json
from datetime import date, datetime, timedelta, timezone

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.x509.oid import NameOID

from luzback import AppError, ErrorCode
from wechat_pay import apiv3, rules
from wechat_pay.fake import FAKE_APIV3_KEY, fake_keypair

# 官方文档 4012365336 公开的测试私钥（apiclient_test_key.pem），不是任何真实商户的密钥
OFFICIAL_TEST_KEY = b"""-----BEGIN PRIVATE KEY-----
MIIEvAIBADANBgkqhkiG9w0BAQEFAASCBKYwggSiAgEAAoIBAQCm2mb6q8gMKH/3
CNTbpJAIrbqiBiQGEOtjGcBrDYltsGynWgNscqT7WvfzU14FQbYcQUC5T4Wvva7m
i3fIp3OgX8VqMDNA0qebnr38Pe6kqiLyZgFpJPXlSKDyPyqhRbVTbXssvSMQeVKc
dXeVxoNNeoOlNFHgF/P0io6AmAVnz+hN8SiZKuOsth5/zUTLGvtkgxBcQooQrtXh
RcpLT798OyIb9xeJ2HO3xRtMv2+perEzb4gMibI74UBz+2QEbnkubPE+2jU2rRZu
dnNEz/BPOt3Qj/w2V6/G0VumGDh6+UeMU0jv4aupHztWITC4Akn0l7lBCNy3lgl8
VFaJnkIxAgMBAAECggEAYGL8aESB7NwciDWW2UdoWUsa7GxFtSdjAz2mFXGdeTsY
mVh7b9OOkRGM+Qio4LqEHDBp1mMk5E/cUJwy1zw8pGGO5nfvs7u9TT3XnHaefIs4
YvUgTYAneIuLRkXNN5rQU+CD7mVYczTSz0Vgjqo9wa1LjUz7G0xbBmJgTdMEFGJs
eJjy6AbJo0CGIwp6HJbTm4CmOUgXnnDAIbEGTIRImkZFH/rzneIeR7oZ77FVwxr1
CZB2gfRCov/yRPbw8vnryYkmvQ7D/ze3j5097vRg/MoDGBSdoOwcmo75vyofr0AS
zytMjmHYyifqkf5slPropSiJeGf4p/7gtKyF6dE/XQKBgQDVAlJ+4U5ZVGOuDc3+
sAhz8CTzgFNlq9vKuSoFK6hOz2L+cwj+E7NXGkOe2DsHHZNy2Xqxk7caKhPEp1z9
hhpMpyLVMoFt6CKemyoRBWDCQwLLwem9SZF/IAyovBkLiH36P42Jm26gUkNMKC/5
Zhtqxf6RZgRQzbVudJi47vIRCwKBgQDIh0+v27Oo+DM3fhObH4I1NrXpWOEGH7OQ
G1dEsMuFYF4hjGhg0kBEP3w9vVdl2+mRllZKTsx9oqjb8OibPLLIH8xsdbAB0WLf
JvjLu4wl/ILUzN1RI03dWnnv2EnEeQn6c3hizvrJ9wR5U4ue9RPVnQooJ0hZF1PU
uCL5fWK3MwKBgElReU/PAYbh80WP3t3Rfbdaa32dKBeQ5iCLR5lsA4zM+YgX1HqQ
EWTj126vgvHaDkyz6vWAoL/Sx+cirHFfXWIRDX5Q2hgYlQH+6qXdMgbrxeSYpHnQ
/tHBGFpkFELSAnrGsVMyOwvYBO4LzyeLK9i+ufcWJFoj1FVmsMLHDG8tAoGARdbi
iQQCoYG4DMarO2aQ6cmhN6EN1h0qY7EyBqlwaIZ0okiNfdMcMOjPc41DKCWcRmlO
qlihXcxN9TQFPzO3rH1urAOdBjUPs1qWYhZyrDQyuLyVBBJApyxAtajloDjrob+f
mQIvVDHk7ACN6xG+E7K6+9salnTKbJapD618uQMCgYBNy6XUvzLkP/A1U/UZdtcx
l8GwU/dturLxz4CyGbqDw4ubaYY2e13lnqHUqQgPtiSyH51nq3tdo8G0YAJdfkSv
KvnfslW91fyEBUKnkdW1o3/1UFU/wprZ7ixVL/F42A4xDu7OFE8EnweJOZ0jWceE
OdhCkaIGBCfRnlECRK8UyQ==
-----END PRIVATE KEY-----
"""
OFFICIAL_BODY = ('{"appid":"wxd678efh567hg6787","mchid":"1900007291","description":"Image形象店-深圳腾大-QQ公仔",'
                 '"out_trade_no":"1217752501201407033233368018","notify_url":"https://www.weixin.qq.com/wxpay/pay.php",'
                 '"amount":{"total":100,"currency":"CNY"},"payer":{"openid":"oUpF8uMuAJO_M2pxb1Q9zNjWeS6o"}}')
OFFICIAL_SIGNATURE = (
    "jnks4dlrPw3ZX+ozVvSK39oa0t7OMBsg83BHAwd8BRdUFiVaQNTLTvci+wURgP1OQBbKYhFGvt7iqYpDSTQkp7Uq1sltaQKyncCyrA1g88m5bsKERQ"
    "fPyT0ahSwKTYJ1CAn9QiJuSJRq1QsQs07eehbU/k9BCS51jTyc1Jpsi2H77HF9f/BnjXAOP3/sPObg6V5Ee4EzwLox684hhuMuIwHo7D8KFk3LIHOK"
    "DcNI4It1aCXydFWNpNK+SG86VUDe5kwoDpw4Ulqfu9z8OFDGbDs9TCxEv8iqQzbpxOlEVoOe2kalSYM5kApQb3nZcxdUtoE0liJGW3RGUNE0t4v01A==")

PLATFORM = fake_keypair("platform")
MERCHANT = fake_keypair("merchant")
SERIAL = "PUB_KEY_ID_0116571234562024052000123400000000"
NOW = 1_800_000_000


def transaction(**overrides):
    data = {"appid": "wx-a", "mchid": "1900000001", "out_trade_no": "P2610281200ABCDEFGHJK", "transaction_id": "4200001",
            "trade_type": "JSAPI", "trade_state": "SUCCESS", "amount": {"total": 8800, "currency": "CNY"}}
    data.update(overrides)
    return data


def notify(payload=None, *, timestamp=NOW, signature=None, serial=SERIAL, tamper=False, associated_data="transaction"):
    body = json.dumps({"id": "EV-1", "event_type": "TRANSACTION.SUCCESS", "resource_type": "encrypt-resource",
                       "resource": apiv3.encrypt_resource(payload or transaction(), FAKE_APIV3_KEY,
                                                          associated_data=associated_data)}).encode()
    headers = apiv3.response_headers(body, serial=serial, private_key=PLATFORM, timestamp=timestamp, signature=signature)
    if tamper:
        body = body.replace(b'"EV-1"', b'"EV-2"')
    return headers, body


def parse(headers, body, now=NOW, keys=None):
    return apiv3.parse_notify(headers=headers, body=body, public_keys=keys or {SERIAL: PLATFORM.public_key()},
                              apiv3_key=FAKE_APIV3_KEY, now=now)


# ---------------------------------------------------------------- 请求签名


def test_official_request_signature_vector():
    key = apiv3.load_private_key(OFFICIAL_TEST_KEY)
    header = apiv3.build_authorization(method="post", url_path="/v3/pay/transactions/jsapi", body=OFFICIAL_BODY,
                                       mchid="1900007291", serial_no="408B07E79B8269FEC3D5D3E6AB8ED163A6A380DB",
                                       private_key=key, timestamp=1554208460, nonce="593BEC0C930BF1AFEB40B4A08C8FB242")
    assert header == (f'WECHATPAY2-SHA256-RSA2048 mchid="1900007291",nonce_str="593BEC0C930BF1AFEB40B4A08C8FB242",'
                      f'signature="{OFFICIAL_SIGNATURE}",timestamp="1554208460",'
                      f'serial_no="408B07E79B8269FEC3D5D3E6AB8ED163A6A380DB"')
    parts = apiv3.verify_request(header, "POST", "/v3/pay/transactions/jsapi", OFFICIAL_BODY, key.public_key())
    assert parts["mchid"] == "1900007291"


def test_get_request_signs_empty_body_line_and_rejects_other_path():
    header = apiv3.build_authorization(method="GET", url_path="/v3/refund/domestic/refunds/R1", body="", mchid="1",
                                       serial_no="S", private_key=MERCHANT, timestamp=NOW, nonce="N" * 32)
    apiv3.verify_request(header, "GET", "/v3/refund/domestic/refunds/R1", "", MERCHANT.public_key())
    with pytest.raises(apiv3.WechatPayError):
        apiv3.verify_request(header, "GET", "/v3/refund/domestic/refunds/R2", "", MERCHANT.public_key())


def test_nonce_is_32_alphanumerics():
    nonce = apiv3.new_nonce()
    assert len(nonce) == 32 and nonce.isalnum()


def test_pay_sign_follows_official_four_line_message():
    params = apiv3.jsapi_pay_params(appid="wx-a", prepay_id="wx201410272009395522657a690389285100",
                                    private_key=MERCHANT, timestamp=1414561699, nonce="5K8264ILTKCH16CQ2502SI8ZNMTM67VS")
    assert params["package"] == "prepay_id=wx201410272009395522657a690389285100" and params["signType"] == "RSA"
    message = b"wx-a\n1414561699\n5K8264ILTKCH16CQ2502SI8ZNMTM67VS\nprepay_id=wx201410272009395522657a690389285100\n"
    MERCHANT.public_key().verify(base64.b64decode(params["paySign"]), message, padding.PKCS1v15(), hashes.SHA256())


# ---------------------------------------------------------------- 回调 / 应答验签


def test_valid_notification_is_verified_and_decrypted():
    envelope, data = parse(*notify())
    assert envelope["event_type"] == "TRANSACTION.SUCCESS" and data["out_trade_no"] == "P2610281200ABCDEFGHJK"


def test_empty_associated_data_is_supported():
    _, data = parse(*notify(associated_data=""))
    assert data["transaction_id"] == "4200001"


@pytest.mark.parametrize("case,reason", [
    ("tamper", "bad_signature"), ("probe", "sign_probe"), ("stale", "stale_timestamp"), ("future", "stale_timestamp"),
    ("serial", "unknown_serial"), ("base64", "bad_signature"),
])
def test_bad_notifications_are_rejected(case, reason):
    kwargs = {"tamper": dict(tamper=True), "probe": dict(signature="WECHATPAY/SIGNTEST/abc"),
              "stale": dict(timestamp=NOW - 301), "future": dict(timestamp=NOW + 301),
              "serial": dict(serial="PUB_KEY_ID_9999"), "base64": dict(signature="%%%notbase64")}[case]
    with pytest.raises(apiv3.WechatPayError) as error:
        parse(*notify(**kwargs))
    assert error.value.reason == reason


def test_timestamp_within_five_minutes_is_accepted():
    parse(*notify(timestamp=NOW - 300))
    parse(*notify(timestamp=NOW + 299))


def test_reserialized_json_fails_verification():
    headers, body = notify()
    reserialized = json.dumps(json.loads(body), separators=(",", ":")).encode()
    with pytest.raises(apiv3.WechatPayError):
        parse(headers, reserialized)


def test_missing_headers_rejected():
    _, body = notify()
    with pytest.raises(apiv3.WechatPayError) as error:
        parse({"Wechatpay-Serial": SERIAL}, body)
    assert error.value.reason == "missing_headers"


def _self_signed_cert(key) -> bytes:
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Tenpay.com fake platform")])
    now = datetime.now(timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(0x5157F09EFDC096DE15EBE81A47057A7232F4A4A7).not_valid_before(now - timedelta(days=1))
            .not_valid_after(now + timedelta(days=365)).sign(key, hashes.SHA256()))
    return cert.public_bytes(serialization.Encoding.PEM)


def test_public_key_mode_and_platform_certificate_mode_coexist():
    """灰度期回调签名在公钥与平台证书之间随机：按 Wechatpay-Serial 分派。"""
    cert_key = fake_keypair("platform-cert")
    store = apiv3.PublicKeyStore()
    store.add("5157F09EFDC096DE15EBE81A47057A7232F4A4A7", apiv3.load_public_key(_self_signed_cert(cert_key)))
    pem = PLATFORM.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
    store.add(SERIAL, apiv3.load_public_key(pem))
    parse(*notify(), keys=store)
    body = b'{"code":"OK"}'
    headers = apiv3.response_headers(body, serial="5157F09EFDC096DE15EBE81A47057A7232F4A4A7", private_key=cert_key, timestamp=NOW)
    apiv3.verify_signature(headers=apiv3.NotifyHeaders.from_mapping(headers), body=body, public_keys=store, now=NOW)
    assert store.encryption_key()[0] == SERIAL  # 敏感字段加密优先用微信支付公钥


# ---------------------------------------------------------------- 解密与敏感字段


def test_decrypt_rejects_wrong_key_algorithm_and_short_key():
    resource = apiv3.encrypt_resource(transaction(), FAKE_APIV3_KEY)
    with pytest.raises(apiv3.WechatPayError) as wrong:
        apiv3.decrypt_resource(resource, b"x" * 32)
    assert wrong.value.reason == "decrypt_failed"
    with pytest.raises(apiv3.WechatPayError) as algo:
        apiv3.decrypt_resource({**resource, "algorithm": "AES_128"}, FAKE_APIV3_KEY)
    assert algo.value.reason == "bad_algorithm"
    with pytest.raises(apiv3.WechatPayError) as short:
        apiv3.decrypt_resource(resource, b"short")
    assert short.value.reason == "bad_apiv3_key"
    with pytest.raises(apiv3.WechatPayError):
        apiv3.decrypt_resource({**resource, "associated_data": "refund"}, FAKE_APIV3_KEY)


def test_sensitive_field_oaep_round_trip_and_limit():
    cipher = apiv3.encrypt_sensitive(PLATFORM.public_key(), "6222020202020202")
    assert apiv3.decrypt_sensitive(PLATFORM, cipher) == "6222020202020202"
    with pytest.raises(apiv3.WechatPayError):
        apiv3.encrypt_sensitive(PLATFORM.public_key(), "x" * 215)


# ---------------------------------------------------------------- 事实核对


@pytest.mark.parametrize("override,reason", [
    ({"appid": "wx-other"}, "merchant_mismatch"), ({"mchid": "1"}, "merchant_mismatch"),
    ({"out_trade_no": "OTHER123"}, "order_mismatch"), ({"amount": {"total": 1, "currency": "CNY"}}, "amount_mismatch"),
    ({"amount": {"total": 8800, "currency": "USD"}}, "amount_mismatch"), ({"transaction_id": ""}, "missing_transaction_id"),
    ({"trade_state": "WHAT"}, "unknown_state"),
])
def test_check_transaction_rejects_mismatch(override, reason):
    with pytest.raises(apiv3.WechatPayError) as error:
        apiv3.check_transaction(transaction(**override), appid="wx-a", mchid="1900000001",
                                out_trade_no="P2610281200ABCDEFGHJK", amount_cents=8800)
    assert error.value.reason == reason


@pytest.mark.parametrize("state,local", [("SUCCESS", "paid"), ("REFUND", "paid"), ("NOTPAY", "paying"), ("CLOSED", "closed")])
def test_trade_state_mapping(state, local):
    assert apiv3.check_transaction(transaction(trade_state=state), appid="wx-a", mchid="1900000001",
                                   out_trade_no="P2610281200ABCDEFGHJK", amount_cents=8800) == local


# ---------------------------------------------------------------- 纯函数规则


def test_out_trade_no_format_and_uniqueness():
    now = datetime(2026, 10, 28, 4, 0, tzinfo=timezone.utc)
    values = {rules.new_out_trade_no("P", now) for _ in range(200)}
    assert len(values) == 200
    sample = next(iter(values))
    assert rules.OUT_TRADE_NO_RE.match(sample) and sample.startswith("P261028120000")
    with pytest.raises(ValueError):
        rules.new_out_trade_no("TOOLONG", now)
    assert rules.out_refund_no(sample, 50) == f"R{sample}50" and len(rules.out_refund_no(sample, 1)) <= 64


def test_sanitize_strips_four_byte_chars_and_truncates():
    assert rules.sanitize_text("鲜花🌹礼盒\x00", 127) == "鲜花礼盒"
    assert len(rules.sanitize_text("字" * 200, 127)) == 127


def test_goods_kind_rule():
    rules.assert_wechat_pay_allowed("physical")
    rules.assert_wechat_pay_allowed("offline_service")
    with pytest.raises(AppError) as error:
        rules.assert_wechat_pay_allowed("virtual_content")
    assert error.value.code == ErrorCode.BUSINESS_RULE
    assert error.value.data["reason"] == "virtual_goods_require_virtual_payment"
    with pytest.raises(AppError) as bad:
        rules.assert_wechat_pay_allowed("coins")
    assert bad.value.code == ErrorCode.BAD_REQUEST


def test_validate_expire_bounds():
    now = datetime(2026, 10, 28, tzinfo=timezone.utc)
    rules.validate_expire(now + timedelta(minutes=2), now)
    with pytest.raises(AppError):
        rules.validate_expire(now + timedelta(seconds=30), now)
    with pytest.raises(AppError):
        rules.validate_expire(now + timedelta(days=16), now)


def test_query_and_refund_query_cadence():
    schedule = (5, 30, 60, 180, 300, 600, 1800)
    assert [rules.next_query_delay(i, schedule) for i in (0, 1, 6, 20)] == [5, 30, 1800, 1800]
    assert rules.refund_query_delay(0, 1) == 60 and rules.refund_query_delay(240, 4) == 60
    assert [rules.refund_query_delay(300 + i, 5 + i) for i in range(6)] == [300, 600, 1200, 1800, 1800, 1800]


def test_refund_block_reasons():
    now = datetime(2026, 10, 28, tzinfo=timezone.utc)
    limits = rules.RefundLimits()
    base = dict(status="paid", amount_cents=1000, refunded_cents=0, refund_count=0, last_refund_at=None,
                paid_at=now - timedelta(days=1), amount=100, now=now, limits=limits)
    assert rules.refund_block_reason(**base) is None
    assert rules.refund_block_reason(**{**base, "amount": 1001}).data["reason"] == "exceeds_refundable"
    assert rules.refund_block_reason(**{**base, "refund_count": 50}).code == ErrorCode.LIMIT_REACHED
    assert rules.refund_block_reason(**{**base, "paid_at": now - timedelta(days=366)}).data["reason"] == "refund_window_expired"
    assert rules.refund_block_reason(**{**base, "last_refund_at": now - timedelta(seconds=30)}).data["reason"] == "refund_interval"
    assert rules.refund_block_reason(**{**base, "status": "paying"}).code == ErrorCode.CONFLICT


def test_parse_trade_bill_with_backticks_and_summary():
    content = ("﻿交易时间,公众账号ID,商户号,微信订单号,商户订单号,交易状态,微信退款单号,商户退款单号,退款金额,退款状态,订单金额,申请退款金额\r\n"
               "`2026-10-27 10:00:00,`wx-a,`1900000001,`4200001,`P1ABCDEF,`SUCCESS,`0,`0,`0.00,`,`88.00,`0.00\r\n"
               "`2026-10-27 11:00:00,`wx-a,`1900000001,`4200001,`P1ABCDEF,`REFUND,`5000001,`RP1ABCDEF01,`10.50,`SUCCESS,`88.00,`10.50\r\n"
               "总交易单数,应结订单总金额,退款总金额\r\n`2,`88.00,`10.50\r\n")
    details, summary = rules.parse_trade_bill(content)
    lines = rules.bill_lines(details)
    assert summary["总交易单数"] == "2"
    assert [(l.kind, l.amount_cents, l.status) for l in lines] == [("payment", 8800, "SUCCESS"), ("refund", 1050, "SUCCESS")]
    assert rules.yuan_to_cents("0.01") == 1 and rules.yuan_to_cents("") is None and rules.yuan_to_cents("x") is None


def test_state_machines_terminal_and_edges():
    assert rules.PAYMENT_MACHINE.can("created", "paying") and rules.PAYMENT_MACHINE.can("closed", "paid")
    assert not rules.PAYMENT_MACHINE.can("paid", "closed") and not rules.PAYMENT_MACHINE.can("failed", "paid")
    with pytest.raises(AppError) as error:
        rules.PAYMENT_MACHINE.check("paid", "paying")
    assert error.value.code == ErrorCode.CONFLICT
    assert rules.REFUND_MACHINE.sources_of("closed") == {"pending", "processing", "abnormal"}
    assert not rules.REFUND_MACHINE.can("succeeded", "closed")


def test_local_day_bounds_use_store_timezone():
    start, end = rules.local_day_bounds(date(2026, 10, 27))
    assert start == datetime(2026, 10, 26, 16, tzinfo=timezone.utc) and end - start == timedelta(days=1)
