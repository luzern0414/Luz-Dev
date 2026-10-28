"""APIv3 验签、签名探测、解密与事实核对（对应 references/standards.json PAY-005~PAY-011）。"""
import base64
import json
import time

import pytest

pytest.importorskip("cryptography")

from cryptography.hazmat.primitives import hashes  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import padding, rsa  # noqa: E402

from wechat_pay.apiv3 import (  # noqa: E402
    WechatPayError,
    build_authorization,
    check_transaction,
    decrypt_resource,
    encrypt_resource,
    jsapi_pay_params,
    parse_notify,
)

APIV3_KEY = b"0123456789abcdef0123456789abcdef"  # 32 字节测试密钥
SERIAL = "PUB_KEY_ID_0000000001"
APPID, MCHID = "wx_test_app", "1900000001"


@pytest.fixture(scope="module")
def wechat_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture(scope="module")
def merchant_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def transaction(**overrides):
    data = {
        "appid": APPID, "mchid": MCHID, "out_trade_no": "P20261026ABCDEF", "transaction_id": "4200000000001",
        "trade_type": "JSAPI", "trade_state": "SUCCESS", "amount": {"total": 8800, "currency": "CNY"},
    }
    data.update(overrides)
    return data


def signed_notify(wechat_key, payload, *, timestamp=None, tamper=False, signature=None):
    body = json.dumps({
        "id": "EV-1", "create_time": "2026-10-26T10:00:00+08:00", "event_type": "TRANSACTION.SUCCESS",
        "resource_type": "encrypt-resource", "summary": "支付成功",
        "resource": encrypt_resource(payload, APIV3_KEY, nonce="abcdefghijkl"),
    }, separators=(",", ":")).encode()
    ts = str(int(time.time()) if timestamp is None else timestamp)
    nonce = "NONCE123"
    message = ts.encode() + b"\n" + nonce.encode() + b"\n" + body + b"\n"
    sig = base64.b64encode(wechat_key.sign(message, padding.PKCS1v15(), hashes.SHA256())).decode()
    if tamper:
        body = body.replace(b'"id":"EV-1"', b'"id":"EV-2"')
    headers = {"Wechatpay-Serial": SERIAL, "Wechatpay-Signature": signature or sig,
               "Wechatpay-Timestamp": ts, "Wechatpay-Nonce": nonce}
    return headers, body


def keys(wechat_key):
    return {SERIAL: wechat_key.public_key()}


def test_valid_notify_is_verified_and_decrypted(wechat_key):
    headers, body = signed_notify(wechat_key, transaction())
    envelope, data = parse_notify(headers=headers, body=body, public_keys=keys(wechat_key), apiv3_key=APIV3_KEY)
    assert envelope["event_type"] == "TRANSACTION.SUCCESS"
    assert check_transaction(data, appid=APPID, mchid=MCHID, out_trade_no="P20261026ABCDEF", amount_cents=8800) == "success"


def test_tampered_body_is_rejected(wechat_key):
    headers, body = signed_notify(wechat_key, transaction(), tamper=True)
    with pytest.raises(WechatPayError) as error:
        parse_notify(headers=headers, body=body, public_keys=keys(wechat_key), apiv3_key=APIV3_KEY)
    assert error.value.reason == "bad_signature"


def test_reserialized_json_fails_verification(wechat_key):
    """验签必须使用原始字节：重新序列化（加空格）后签名不再匹配。"""
    headers, body = signed_notify(wechat_key, transaction())
    reserialized = json.dumps(json.loads(body)).encode()
    with pytest.raises(WechatPayError):
        parse_notify(headers=headers, body=reserialized, public_keys=keys(wechat_key), apiv3_key=APIV3_KEY)


def test_sign_probe_is_rejected(wechat_key):
    headers, body = signed_notify(wechat_key, transaction(), signature="WECHATPAY/SIGNTEST/abc==")
    with pytest.raises(WechatPayError) as error:
        parse_notify(headers=headers, body=body, public_keys=keys(wechat_key), apiv3_key=APIV3_KEY)
    assert error.value.reason == "sign_probe"


def test_unknown_serial_and_stale_timestamp_rejected(wechat_key):
    headers, body = signed_notify(wechat_key, transaction())
    with pytest.raises(WechatPayError) as error:
        parse_notify(headers={**headers, "Wechatpay-Serial": "PUB_KEY_ID_999"}, body=body,
                     public_keys=keys(wechat_key), apiv3_key=APIV3_KEY)
    assert error.value.reason == "unknown_serial"
    headers, body = signed_notify(wechat_key, transaction(), timestamp=int(time.time()) - 3600)
    with pytest.raises(WechatPayError) as error:
        parse_notify(headers=headers, body=body, public_keys=keys(wechat_key), apiv3_key=APIV3_KEY)
    assert error.value.reason == "stale_timestamp"


def test_missing_headers_rejected(wechat_key):
    _, body = signed_notify(wechat_key, transaction())
    with pytest.raises(WechatPayError) as error:
        parse_notify(headers={}, body=body, public_keys=keys(wechat_key), apiv3_key=APIV3_KEY)
    assert error.value.reason == "missing_headers"


def test_decrypt_rejects_wrong_key_and_algorithm():
    resource = encrypt_resource(transaction(), APIV3_KEY, nonce="abcdefghijkl")
    with pytest.raises(WechatPayError) as error:
        decrypt_resource(resource, b"x" * 32)
    assert error.value.reason == "decrypt_failed"
    with pytest.raises(WechatPayError) as error:
        decrypt_resource({**resource, "algorithm": "AES_CBC"}, APIV3_KEY)
    assert error.value.reason == "bad_algorithm"
    with pytest.raises(WechatPayError) as error:
        decrypt_resource(resource, b"short")
    assert error.value.reason == "bad_apiv3_key"


@pytest.mark.parametrize("overrides,reason", [
    ({"amount": {"total": 1, "currency": "CNY"}}, "amount_mismatch"),
    ({"mchid": "other"}, "merchant_mismatch"),
    ({"appid": "other"}, "merchant_mismatch"),
    ({"out_trade_no": "OTHER"}, "order_mismatch"),
    ({"trade_state": "WHATEVER"}, "unknown_state"),
])
def test_fact_mismatch_is_rejected(overrides, reason):
    with pytest.raises(WechatPayError) as error:
        check_transaction(transaction(**overrides), appid=APPID, mchid=MCHID, out_trade_no="P20261026ABCDEF", amount_cents=8800)
    assert error.value.reason == reason


@pytest.mark.parametrize("state,local", [("SUCCESS", "success"), ("REFUND", "success"), ("NOTPAY", "pending"), ("CLOSED", "closed")])
def test_trade_state_mapping(state, local):
    data = transaction(trade_state=state)
    assert check_transaction(data, appid=APPID, mchid=MCHID, out_trade_no="P20261026ABCDEF", amount_cents=8800) == local


def test_authorization_and_pay_sign_verify_with_merchant_public_key(merchant_key):
    header = build_authorization(method="POST", url_path="/v3/pay/transactions/jsapi", body='{"a":1}',
                                 mchid=MCHID, serial_no="SERIAL", private_key=merchant_key, timestamp=1700000000, nonce="N1")
    assert header.startswith('WECHATPAY2-SHA256-RSA2048 mchid="1900000001",nonce_str="N1",signature="')
    signature = header.split('signature="')[1].split('"')[0]
    merchant_key.public_key().verify(base64.b64decode(signature), b'POST\n/v3/pay/transactions/jsapi\n1700000000\nN1\n{"a":1}\n',
                                     padding.PKCS1v15(), hashes.SHA256())
    params = jsapi_pay_params(appid=APPID, prepay_id="wx2026", private_key=merchant_key, timestamp=1700000000, nonce="N2")
    assert params["package"] == "prepay_id=wx2026" and params["signType"] == "RSA"
    merchant_key.public_key().verify(base64.b64decode(params["paySign"]), f"{APPID}\n1700000000\nN2\nprepay_id=wx2026\n".encode(),
                                     padding.PKCS1v15(), hashes.SHA256())
