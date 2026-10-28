"""微信支付 APIv3 的签名、验签、解密与事实核对（纯函数，不做网络调用）。

依据（2026-10-26 经 Luz-Crawl 读取官方原文核对）：
- 支付成功回调通知 https://pay.weixin.qq.com/doc/v3/merchant/4012791861
  请求头 Wechatpay-Serial / Signature / Timestamp / Nonce；验签串 = 时间戳\\n随机串\\n原始报文\\n；
  Wechatpay-Serial 以 PUB_KEY_ID_ 开头时用微信支付公钥，否则用平台证书；
  Wechatpay-Signature 以 WECHATPAY/SIGNTEST/ 开头的是签名探测流量，必须验签失败；
  5 秒内应答：成功 200/204 无应答体，失败 4XX/5XX + {"code":"FAIL","message":...}。
- 如何解密回调报文 https://pay.weixin.qq.com/doc/v3/merchant/4012071382
  AEAD_AES_256_GCM，APIv3 密钥 32 字节，nonce + associated_data，ciphertext 为 Base64。
- 签名认证 https://pay.weixin.qq.com/doc/v3/merchant/4012365342
- JSAPI 调起支付 https://pay.weixin.qq.com/doc/v3/merchant/4012791857

生产接入优先使用官方 SDK；本模块用于自研客户端或核对 SDK 行为，测试见 tests/test_apiv3.py。
"""
from __future__ import annotations

import base64
import json
import secrets
import time
from dataclasses import dataclass
from typing import Any, Mapping

from cryptography.exceptions import InvalidSignature, InvalidTag
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

AUTH_SCHEMA = "WECHATPAY2-SHA256-RSA2048"
SIGN_PROBE_PREFIX = "WECHATPAY/SIGNTEST/"
PUBLIC_KEY_ID_PREFIX = "PUB_KEY_ID_"
ALGORITHM = "AEAD_AES_256_GCM"
DEFAULT_TIMESTAMP_SKEW = 300  # 秒；超过视为重放或时钟错误

# 小程序 / JSAPI 支付会返回的交易状态；REVOKED、USERPAYING、PAYERROR 仅付款码支付返回。
TRADE_STATE_MAP = {
    "SUCCESS": "success",
    "REFUND": "success",   # 已支付并转入退款：支付事实成立，退款由退款流程处理
    "NOTPAY": "pending",
    "CLOSED": "closed",
    "REVOKED": "closed",
    "USERPAYING": "pending",
    "PAYERROR": "failed",
}


class WechatPayError(Exception):
    """拒绝处理的原因；reason 是稳定的机器可读代码，不包含密钥或报文明文。"""

    def __init__(self, reason: str, message: str = ""):
        super().__init__(message or reason)
        self.reason = reason


@dataclass(frozen=True)
class NotifyHeaders:
    serial: str
    signature: str
    timestamp: str
    nonce: str

    @classmethod
    def from_mapping(cls, headers: Mapping[str, str]) -> "NotifyHeaders":
        lowered = {key.lower(): value for key, value in headers.items()}
        values = [lowered.get(f"wechatpay-{name}", "") for name in ("serial", "signature", "timestamp", "nonce")]
        if not all(values):
            raise WechatPayError("missing_headers", "回调缺少 Wechatpay-* 签名头")
        return cls(*values)


def load_private_key(pem: bytes) -> rsa.RSAPrivateKey:
    key = serialization.load_pem_private_key(pem, password=None)
    if not isinstance(key, rsa.RSAPrivateKey):
        raise WechatPayError("bad_private_key", "商户私钥必须是 RSA 私钥")
    return key


def load_public_key(pem: bytes) -> rsa.RSAPublicKey:
    """加载微信支付公钥（PEM 公钥）或平台证书（PEM 证书）。"""
    if b"BEGIN CERTIFICATE" in pem:
        from cryptography import x509

        key = x509.load_pem_x509_certificate(pem).public_key()
    else:
        key = serialization.load_pem_public_key(pem)
    if not isinstance(key, rsa.RSAPublicKey):
        raise WechatPayError("bad_public_key", "微信支付公钥 / 平台证书必须是 RSA")
    return key


def _sign(private_key: rsa.RSAPrivateKey, message: str) -> str:
    signature = private_key.sign(message.encode("utf-8"), padding.PKCS1v15(), hashes.SHA256())
    return base64.b64encode(signature).decode("ascii")


def _nonce() -> str:
    return secrets.token_hex(16).upper()


def build_authorization(
    *, method: str, url_path: str, body: str, mchid: str, serial_no: str,
    private_key: rsa.RSAPrivateKey, timestamp: int | None = None, nonce: str | None = None,
) -> str:
    """请求微信支付 API 的 Authorization 头。url_path 含查询串，不含域名；GET 的 body 为空串。"""
    timestamp = int(time.time()) if timestamp is None else timestamp
    nonce = nonce or _nonce()
    message = f"{method.upper()}\n{url_path}\n{timestamp}\n{nonce}\n{body}\n"
    signature = _sign(private_key, message)
    return (
        f'{AUTH_SCHEMA} mchid="{mchid}",nonce_str="{nonce}",signature="{signature}",'
        f'timestamp="{timestamp}",serial_no="{serial_no}"'
    )


def jsapi_pay_params(
    *, appid: str, prepay_id: str, private_key: rsa.RSAPrivateKey,
    timestamp: int | None = None, nonce: str | None = None,
) -> dict[str, str]:
    """小程序 wx.requestPayment 所需参数；签名只在服务端完成。"""
    timestamp_text = str(int(time.time()) if timestamp is None else timestamp)
    nonce = nonce or _nonce()
    package = f"prepay_id={prepay_id}"
    pay_sign = _sign(private_key, f"{appid}\n{timestamp_text}\n{nonce}\n{package}\n")
    return {"timeStamp": timestamp_text, "nonceStr": nonce, "package": package, "signType": "RSA", "paySign": pay_sign}


def is_sign_probe(signature: str) -> bool:
    return signature.startswith(SIGN_PROBE_PREFIX)


def verify_signature(
    *, headers: NotifyHeaders, body: bytes, public_keys: Mapping[str, rsa.RSAPublicKey],
    now: int | None = None, max_skew: int = DEFAULT_TIMESTAMP_SKEW,
) -> None:
    """验证回调或应答签名。body 必须是收到的原始字节，不能先解析再序列化。"""
    if is_sign_probe(headers.signature):
        raise WechatPayError("sign_probe", "签名探测流量：按验签失败应答")
    try:
        timestamp = int(headers.timestamp)
    except ValueError as exc:
        raise WechatPayError("bad_timestamp", "Wechatpay-Timestamp 不是整数") from exc
    current = int(time.time()) if now is None else now
    if abs(current - timestamp) > max_skew:
        raise WechatPayError("stale_timestamp", "回调时间戳超出允许偏差")
    key = public_keys.get(headers.serial)
    if key is None:
        # 序列号不在已配置的公钥 / 证书中：可能是轮换未同步，也可能是伪造
        raise WechatPayError("unknown_serial", "未知的 Wechatpay-Serial")
    try:
        signature = base64.b64decode(headers.signature, validate=True)
    except (ValueError, TypeError) as exc:
        raise WechatPayError("bad_signature", "签名不是有效的 Base64") from exc
    message = headers.timestamp.encode() + b"\n" + headers.nonce.encode() + b"\n" + body + b"\n"
    try:
        key.verify(signature, message, padding.PKCS1v15(), hashes.SHA256())
    except InvalidSignature as exc:
        raise WechatPayError("bad_signature", "验签失败") from exc


def decrypt_resource(resource: Mapping[str, Any], apiv3_key: bytes) -> dict[str, Any]:
    """解密回调 resource，返回业务 JSON。"""
    if len(apiv3_key) != 32:
        raise WechatPayError("bad_apiv3_key", "APIv3 密钥必须为 32 字节")
    if resource.get("algorithm") != ALGORITHM:
        raise WechatPayError("bad_algorithm", "不支持的加密算法")
    try:
        ciphertext = base64.b64decode(resource["ciphertext"], validate=True)
        nonce = str(resource["nonce"]).encode()
        associated = str(resource.get("associated_data") or "").encode()
        plaintext = AESGCM(apiv3_key).decrypt(nonce, ciphertext, associated)
        return json.loads(plaintext)
    except (KeyError, ValueError, TypeError, InvalidTag) as exc:
        raise WechatPayError("decrypt_failed", "回调解密失败") from exc


def encrypt_resource(payload: Mapping[str, Any], apiv3_key: bytes, *, nonce: str, associated_data: str = "transaction") -> dict[str, str]:
    """测试与联调用：按官方格式生成密文 resource。"""
    ciphertext = AESGCM(apiv3_key).encrypt(nonce.encode(), json.dumps(payload).encode(), associated_data.encode())
    return {
        "original_type": "transaction", "algorithm": ALGORITHM,
        "ciphertext": base64.b64encode(ciphertext).decode(), "nonce": nonce, "associated_data": associated_data,
    }


def parse_notify(
    *, headers: Mapping[str, str], body: bytes, public_keys: Mapping[str, rsa.RSAPublicKey],
    apiv3_key: bytes, now: int | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """验签 → 解析外层 → 解密。返回 (外层通知, 解密后的业务数据)。"""
    verify_signature(headers=NotifyHeaders.from_mapping(headers), body=body, public_keys=public_keys, now=now)
    try:
        envelope = json.loads(body)
    except ValueError as exc:
        raise WechatPayError("bad_body", "回调报文不是 JSON") from exc
    if envelope.get("resource_type") != "encrypt-resource" or not isinstance(envelope.get("resource"), dict):
        raise WechatPayError("bad_body", "回调缺少加密 resource")
    return envelope, decrypt_resource(envelope["resource"], apiv3_key)


def check_transaction(
    transaction: Mapping[str, Any], *, appid: str, mchid: str, out_trade_no: str, amount_cents: int, currency: str = "CNY",
) -> str:
    """核对解密后的交易与本地支付单，返回本地状态（success / pending / closed / failed）。

    任一事实不一致都抛出 WechatPayError，调用方不得改变订单状态，应记录并告警人工对账。
    """
    if transaction.get("appid") != appid or transaction.get("mchid") != mchid:
        raise WechatPayError("merchant_mismatch", "appid 或 mchid 与本系统配置不一致")
    if transaction.get("out_trade_no") != out_trade_no:
        raise WechatPayError("order_mismatch", "商户订单号不一致")
    state = str(transaction.get("trade_state", ""))
    if state not in TRADE_STATE_MAP:
        raise WechatPayError("unknown_state", f"未知交易状态 {state}")
    local = TRADE_STATE_MAP[state]
    if local == "success":
        amount = transaction.get("amount") or {}
        if amount.get("total") != amount_cents or (amount.get("currency") or "CNY") != currency:
            raise WechatPayError("amount_mismatch", "金额或币种与本地支付单不一致")
        if not transaction.get("transaction_id"):
            raise WechatPayError("missing_transaction_id", "缺少微信支付订单号")
    return local


def failure_response(message: str = "失败") -> tuple[int, dict[str, str]]:
    """回调失败应答：4XX/5XX + JSON。成功应答为 HTTP 204 无应答体。"""
    return 400, {"code": "FAIL", "message": message}
