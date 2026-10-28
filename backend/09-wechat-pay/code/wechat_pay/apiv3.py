"""微信支付 APIv3 的签名、验签、加解密与事实核对（纯函数，不做网络调用）。

依据（2026-10-28 核对，见 MODULE.md §0）：
- 请求签名 4012365336：签名串 `方法\\nURL\\n时间戳\\n随机串\\n报文\\n`，SHA256withRSA(PKCS#1 v1.5) + Base64；
  Authorization: WECHATPAY2-SHA256-RSA2048 mchid,nonce_str,signature,timestamp,serial_no（顺序不限）。
- 验签 4013053249 / 4013053420：`时间戳\\n随机串\\n原始报文\\n`；时间戳偏差超过 5 分钟拒绝；
  Wechatpay-Serial 以 PUB_KEY_ID_ 开头用微信支付公钥，否则用平台证书；
  Wechatpay-Signature 以 WECHATPAY/SIGNTEST/ 开头的是签名探测，按正常流程必然验签失败。
- 解密 4012071382：AEAD_AES_256_GCM，key = APIv3 密钥 32 字节，nonce / associated_data 取 UTF-8；associated_data 可能为空。
- 敏感字段加密 4013053257：RSAES-OAEP（SHA-1 + MGF1-SHA1），Base64。
- 调起支付 4012791898：paySign 签名串 `appId\\ntimeStamp\\nnonceStr\\nprepay_id=…\\n`，signType=RSA。
"""
from __future__ import annotations

import base64
import hashlib
import json
import secrets
import string
import time
from dataclasses import dataclass, field
from typing import Any, Mapping

from cryptography import x509
from cryptography.exceptions import InvalidSignature, InvalidTag
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

AUTH_SCHEMA = "WECHATPAY2-SHA256-RSA2048"
SIGN_PROBE_PREFIX = "WECHATPAY/SIGNTEST/"
PUBLIC_KEY_ID_PREFIX = "PUB_KEY_ID_"
ALGORITHM = "AEAD_AES_256_GCM"
DEFAULT_TIMESTAMP_SKEW = 300  # 秒；超过视为重放或时钟错误
_NONCE_ALPHABET = string.ascii_letters + string.digits

# 交易状态 → 本地支付单状态。REVOKED / USERPAYING / PAYERROR 仅付款码支付返回，小程序支付不会出现。
TRADE_STATE_MAP = {
    "SUCCESS": "paid",
    "REFUND": "paid",      # 已支付并转入退款：支付事实成立，退款由退款单表达
    "NOTPAY": "paying",
    "CLOSED": "closed",
    "REVOKED": "closed",
    "USERPAYING": "paying",
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
            raise WechatPayError("missing_headers", "缺少 Wechatpay-* 签名头")
        return cls(*values)


# ---------------------------------------------------------------- 密钥


def load_private_key(pem: bytes) -> rsa.RSAPrivateKey:
    key = serialization.load_pem_private_key(pem, password=None)
    if not isinstance(key, rsa.RSAPrivateKey):
        raise WechatPayError("bad_private_key", "商户私钥必须是 RSA 私钥")
    return key


def load_public_key(pem: bytes) -> rsa.RSAPublicKey:
    """加载微信支付公钥（PEM 公钥）或平台证书（PEM 证书）。"""
    if b"BEGIN CERTIFICATE" in pem:
        key = x509.load_pem_x509_certificate(pem).public_key()
    else:
        key = serialization.load_pem_public_key(pem)
    if not isinstance(key, rsa.RSAPublicKey):
        raise WechatPayError("bad_public_key", "微信支付公钥 / 平台证书必须是 RSA")
    return key


@dataclass
class PublicKeyStore:
    """{Wechatpay-Serial → 公钥}：微信支付公钥（PUB_KEY_ID_…）与平台证书可同时存在（灰度期回调两种签名随机出现）。"""

    keys: dict[str, rsa.RSAPublicKey] = field(default_factory=dict)
    preferred: str | None = None  # 加密敏感字段用的序列号：公钥模式优先

    def add(self, serial: str, key: rsa.RSAPublicKey) -> None:
        self.keys[serial] = key
        if self.preferred is None or serial.startswith(PUBLIC_KEY_ID_PREFIX):
            self.preferred = serial

    def get(self, serial: str) -> rsa.RSAPublicKey | None:
        return self.keys.get(serial)

    def encryption_key(self) -> tuple[str, rsa.RSAPublicKey]:
        if not self.preferred:
            raise WechatPayError("no_public_key", "未配置微信支付公钥或平台证书")
        return self.preferred, self.keys[self.preferred]


# ---------------------------------------------------------------- 签名


def sign_message(private_key: rsa.RSAPrivateKey, message: str | bytes) -> str:
    data = message.encode("utf-8") if isinstance(message, str) else message
    return base64.b64encode(private_key.sign(data, padding.PKCS1v15(), hashes.SHA256())).decode("ascii")


def new_nonce(length: int = 32) -> str:
    """32 位 [0-9a-zA-Z]（与官方工具库一致）。"""
    return "".join(secrets.choice(_NONCE_ALPHABET) for _ in range(length))


def request_message(method: str, url_path: str, timestamp: int | str, nonce: str, body: str) -> str:
    return f"{method.upper()}\n{url_path}\n{timestamp}\n{nonce}\n{body}\n"


def build_authorization(
    *, method: str, url_path: str, body: str, mchid: str, serial_no: str,
    private_key: rsa.RSAPrivateKey, timestamp: int | None = None, nonce: str | None = None,
) -> str:
    """请求微信支付 API 的 Authorization 头。url_path 含查询串，不含域名；GET 的 body 为空串。"""
    timestamp = int(time.time()) if timestamp is None else timestamp
    nonce = nonce or new_nonce()
    signature = sign_message(private_key, request_message(method, url_path, timestamp, nonce, body))
    return (
        f'{AUTH_SCHEMA} mchid="{mchid}",nonce_str="{nonce}",signature="{signature}",'
        f'timestamp="{timestamp}",serial_no="{serial_no}"'
    )


def parse_authorization(header: str) -> dict[str, str]:
    """解析 Authorization 头（测试替身用它校验请求签名）。"""
    if not header.startswith(AUTH_SCHEMA + " "):
        raise WechatPayError("bad_authorization", "Authorization 认证类型错误")
    out: dict[str, str] = {}
    for part in header[len(AUTH_SCHEMA) + 1:].split(","):
        key, _, value = part.strip().partition("=")
        out[key] = value.strip('"')
    if not {"mchid", "nonce_str", "signature", "timestamp", "serial_no"} <= set(out):
        raise WechatPayError("bad_authorization", "Authorization 字段不全")
    return out


def verify_request(header: str, method: str, url_path: str, body: str, public_key: rsa.RSAPublicKey) -> dict[str, str]:
    parts = parse_authorization(header)
    message = request_message(method, url_path, parts["timestamp"], parts["nonce_str"], body)
    try:
        public_key.verify(base64.b64decode(parts["signature"]), message.encode(), padding.PKCS1v15(), hashes.SHA256())
    except (InvalidSignature, ValueError) as exc:
        raise WechatPayError("bad_signature", "请求签名无效") from exc
    return parts


def jsapi_pay_params(
    *, appid: str, prepay_id: str, private_key: rsa.RSAPrivateKey,
    timestamp: int | None = None, nonce: str | None = None,
) -> dict[str, str]:
    """小程序 wx.requestPayment 所需参数；签名只在服务端完成，appid 必须与下单时一致。"""
    timestamp_text = str(int(time.time()) if timestamp is None else timestamp)
    nonce = nonce or new_nonce()
    package = f"prepay_id={prepay_id}"
    pay_sign = sign_message(private_key, f"{appid}\n{timestamp_text}\n{nonce}\n{package}\n")
    return {"timeStamp": timestamp_text, "nonceStr": nonce, "package": package, "signType": "RSA", "paySign": pay_sign}


def response_headers(
    body: bytes, *, serial: str, private_key: rsa.RSAPrivateKey, timestamp: int | None = None,
    nonce: str | None = None, signature: str | None = None,
) -> dict[str, str]:
    """生成 Wechatpay-* 应答 / 回调签名头（测试替身与联调用）。signature 可覆盖为探测值。"""
    timestamp_text = str(int(time.time()) if timestamp is None else timestamp)
    nonce = nonce or new_nonce()
    sig = signature or sign_message(private_key, timestamp_text.encode() + b"\n" + nonce.encode() + b"\n" + body + b"\n")
    return {
        "Wechatpay-Serial": serial, "Wechatpay-Signature": sig, "Wechatpay-Timestamp": timestamp_text,
        "Wechatpay-Nonce": nonce, "Wechatpay-Signature-Type": AUTH_SCHEMA,
    }


# ---------------------------------------------------------------- 验签


def is_sign_probe(signature: str) -> bool:
    return signature.startswith(SIGN_PROBE_PREFIX)


def verify_signature(
    *, headers: NotifyHeaders, body: bytes, public_keys: Mapping[str, rsa.RSAPublicKey] | PublicKeyStore,
    now: int | None = None, max_skew: int = DEFAULT_TIMESTAMP_SKEW,
) -> None:
    """验证回调或应答签名。body 必须是收到的原始字节，不能先解析再序列化。"""
    if is_sign_probe(headers.signature):
        # 官方要求不特殊处理：探测值必然验签失败；这里只是给出稳定的原因码便于统计
        raise WechatPayError("sign_probe", "签名探测流量：按验签失败应答")
    try:
        timestamp = int(headers.timestamp)
    except ValueError as exc:
        raise WechatPayError("bad_timestamp", "Wechatpay-Timestamp 不是整数") from exc
    current = int(time.time()) if now is None else int(now)
    if abs(current - timestamp) > max_skew:
        raise WechatPayError("stale_timestamp", "时间戳超出允许偏差")
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


# ---------------------------------------------------------------- 解密 / 加密


def _apiv3_key(key: bytes | str) -> bytes:
    raw = key.encode() if isinstance(key, str) else key
    if len(raw) != 32:
        raise WechatPayError("bad_apiv3_key", "APIv3 密钥必须为 32 字节")
    return raw


def decrypt_resource(resource: Mapping[str, Any], apiv3_key: bytes | str) -> dict[str, Any]:
    """解密回调 resource（associated_data 可为空），返回业务 JSON。"""
    key = _apiv3_key(apiv3_key)
    if resource.get("algorithm") != ALGORITHM:
        raise WechatPayError("bad_algorithm", "不支持的加密算法")
    try:
        ciphertext = base64.b64decode(resource["ciphertext"], validate=True)
        nonce = str(resource["nonce"]).encode()
        associated = str(resource.get("associated_data") or "").encode()
        plaintext = AESGCM(key).decrypt(nonce, ciphertext, associated or None)
        data = json.loads(plaintext)
    except (KeyError, ValueError, TypeError, InvalidTag) as exc:
        raise WechatPayError("decrypt_failed", "解密失败") from exc
    if not isinstance(data, dict):
        raise WechatPayError("decrypt_failed", "解密结果不是 JSON 对象")
    return data


def encrypt_resource(
    payload: Mapping[str, Any], apiv3_key: bytes | str, *, nonce: str | None = None,
    associated_data: str = "transaction", original_type: str = "transaction",
) -> dict[str, str]:
    """测试与联调用：按官方格式生成密文 resource。"""
    nonce = nonce or new_nonce(12)
    ciphertext = AESGCM(_apiv3_key(apiv3_key)).encrypt(
        nonce.encode(), json.dumps(payload, ensure_ascii=False).encode(), associated_data.encode() or None)
    return {
        "original_type": original_type, "algorithm": ALGORITHM,
        "ciphertext": base64.b64encode(ciphertext).decode(), "nonce": nonce, "associated_data": associated_data,
    }


def encrypt_sensitive(public_key: rsa.RSAPublicKey, plaintext: str) -> str:
    """敏感字段加密（异常退款的银行卡号、姓名）：OAEP + SHA-1，原文最多 214 字节。"""
    data = plaintext.encode("utf-8")
    if len(data) > 214:
        raise WechatPayError("sensitive_too_long", "敏感字段原文超过 214 字节")
    oaep = padding.OAEP(mgf=padding.MGF1(algorithm=hashes.SHA1()), algorithm=hashes.SHA1(), label=None)
    return base64.b64encode(public_key.encrypt(data, oaep)).decode()


def decrypt_sensitive(private_key: rsa.RSAPrivateKey, ciphertext_b64: str) -> str:
    oaep = padding.OAEP(mgf=padding.MGF1(algorithm=hashes.SHA1()), algorithm=hashes.SHA1(), label=None)
    return private_key.decrypt(base64.b64decode(ciphertext_b64), oaep).decode("utf-8")


# ---------------------------------------------------------------- 回调


def parse_notify(
    *, headers: Mapping[str, str], body: bytes, public_keys: Mapping[str, rsa.RSAPublicKey] | PublicKeyStore,
    apiv3_key: bytes | str, now: int | None = None, max_skew: int = DEFAULT_TIMESTAMP_SKEW,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """验签 → 解析外层 → 解密。返回 (外层通知, 解密后的业务数据)。"""
    verify_signature(headers=NotifyHeaders.from_mapping(headers), body=body, public_keys=public_keys,
                     now=now, max_skew=max_skew)
    try:
        envelope = json.loads(body)
    except ValueError as exc:
        raise WechatPayError("bad_body", "回调报文不是 JSON") from exc
    if not isinstance(envelope, dict) or envelope.get("resource_type") != "encrypt-resource" \
            or not isinstance(envelope.get("resource"), dict):
        raise WechatPayError("bad_body", "回调缺少加密 resource")
    return envelope, decrypt_resource(envelope["resource"], apiv3_key)


def check_transaction(
    transaction: Mapping[str, Any], *, appid: str, mchid: str, out_trade_no: str, amount_cents: int, currency: str = "CNY",
) -> str:
    """核对解密后的交易与本地支付单，返回本地状态（paid / paying / closed / failed）。

    任一事实不一致都抛出 WechatPayError，调用方不得推进业务，应记录并告警人工对账。
    """
    if transaction.get("appid") != appid or transaction.get("mchid") != mchid:
        raise WechatPayError("merchant_mismatch", "appid 或 mchid 与本系统配置不一致")
    if transaction.get("out_trade_no") != out_trade_no:
        raise WechatPayError("order_mismatch", "商户订单号不一致")
    state = str(transaction.get("trade_state", ""))
    if state not in TRADE_STATE_MAP:
        raise WechatPayError("unknown_state", f"未知交易状态 {state}")
    local = TRADE_STATE_MAP[state]
    if local == "paid":
        amount = transaction.get("amount") or {}
        if amount.get("total") != amount_cents or (amount.get("currency") or "CNY") != currency:
            raise WechatPayError("amount_mismatch", "金额或币种与本地支付单不一致")
        if not transaction.get("transaction_id"):
            raise WechatPayError("missing_transaction_id", "缺少微信支付订单号")
    return local


def failure_body(message: str = "失败") -> dict[str, str]:
    """回调失败应答体（4XX/5XX）；成功应答为 HTTP 204 无应答体。"""
    return {"code": "FAIL", "message": message}


def failure_response(message: str = "失败") -> tuple[int, dict[str, str]]:
    """兼容 v1：失败应答（状态码, 应答体）。"""
    return 400, failure_body(message)


def sha1_hex(data: bytes) -> str:
    return hashlib.sha1(data).hexdigest()  # noqa: S324 - 官方账单哈希算法即 SHA1，仅用于完整性核对
