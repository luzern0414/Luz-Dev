"""微信支付渠道客户端：接口抽象 `WechatPayClient`、HTTP 实现 `HttpWechatPayClient`、错误分类。

- 每个请求按 APIv3 规则签名（Authorization），应答按 Wechatpay-* 头验签（账单文件下载除外，改校验 SHA1）。
- 公钥模式下请求带 `Wechatpay-Serial: PUB_KEY_ID_…`，微信用微信支付私钥签名应答（4013038816）。
- 应答验签失败（含签名探测）：丢弃应答，按可重试错误处理——微信不会对重试请求再做探测（4013053249）。
- 主域名连接失败时切换备域名 api2（4012081717 / 接口页）。
- 本模块不做重试与熔断，由 service 经 `luzback.resilient` 包裹；这里只把错误分成可重试 / 不可重试。
"""
from __future__ import annotations

import json
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, Protocol, runtime_checkable
from urllib.parse import quote, urlencode, urlsplit

from luzback import TransientError

from . import apiv3
from .config import WechatPayConfig

# 官方：用相同参数重试安全的错误码（4012791897 / 4012791903 / 4012081717 / 4014959631）
RETRYABLE_CODES = frozenset({
    "SYSTEM_ERROR", "FREQUENCY_LIMITED", "RATELIMIT_EXCEEDED", "SERVICE_UNAVAILABLE", "NOT_ENOUGH",
    "ACCEPTED", "TIMEOUT", "NETWORK_ERROR", "RESPONSE_SIGNATURE_INVALID", "BANK_ERROR",
})


class WechatPayApiError(Exception):
    """渠道返回的错误。code 为官方错误码（不依赖 message 文本做判断，4014959631）。"""

    def __init__(self, code: str, message: str = "", http_status: int = 0, request_id: str = "", detail: Any = None):
        super().__init__(f"{code}: {message}" if message else code)
        self.code = code
        self.message = message
        self.http_status = http_status
        self.request_id = request_id
        self.detail = detail

    @property
    def retryable(self) -> bool:
        return False


class TransientWechatPayError(WechatPayApiError, TransientError):
    """可用相同参数重试：5xx、202、429、超时、网络、应答验签失败、NOT_ENOUGH（充值后原单重试）。"""

    @property
    def retryable(self) -> bool:
        return True


def classify(code: str, http_status: int, message: str = "", request_id: str = "", detail: Any = None) -> WechatPayApiError:
    if code in RETRYABLE_CODES or http_status in (202, 429) or http_status >= 500:
        return TransientWechatPayError(code, message, http_status, request_id, detail)
    return WechatPayApiError(code, message, http_status, request_id, detail)


@runtime_checkable
class WechatPayClient(Protocol):
    """渠道能力。body 由 service 组装（同一单号重放时逐字节相同）。"""

    appid: str
    mchid: str

    def jsapi_prepay(self, body: Mapping[str, Any]) -> str: ...
    def query_by_out_trade_no(self, out_trade_no: str) -> dict[str, Any]: ...
    def close(self, out_trade_no: str) -> None: ...
    def create_refund(self, body: Mapping[str, Any]) -> dict[str, Any]: ...
    def query_refund(self, out_refund_no: str) -> dict[str, Any]: ...
    def apply_abnormal_refund(self, refund_id: str, body: Mapping[str, Any]) -> dict[str, Any]: ...
    def apply_trade_bill(self, bill_date: str, bill_type: str = "ALL", tar_type: str | None = "GZIP") -> dict[str, Any]: ...
    def download_bill(self, download_url: str) -> bytes: ...
    def encrypt_sensitive(self, plaintext: str) -> str: ...
    def jsapi_pay_params(self, prepay_id: str) -> dict[str, str]: ...
    def parse_notification(self, headers: Mapping[str, str], body: bytes, now: int | None = None) -> tuple[dict, dict]: ...


def dumps(body: Mapping[str, Any]) -> str:
    """签名与发送用同一个字符串（4012365336）。"""
    return json.dumps(body, ensure_ascii=False, separators=(",", ":"))


def _read(path: str) -> bytes:
    return Path(path).read_bytes()


class HttpWechatPayClient:
    """生产用 HTTP 客户端（httpx）。密钥只在内存；日志不写报文与密钥。"""

    def __init__(self, config: WechatPayConfig, http: Any = None, clock: Callable[[], float] = time.time):
        import httpx

        self.config = config
        self.appid = config.appid
        self.mchid = config.mchid
        self.clock = clock
        pem = config.private_key_pem.encode() if config.private_key_pem else _read(config.private_key_path)
        self.private_key = apiv3.load_private_key(pem)
        self.keys = apiv3.PublicKeyStore()
        for serial, path in config.platform_certs:
            self.keys.add(serial, apiv3.load_public_key(_read(path)))
        if config.public_key_id and config.public_key_path:
            self.keys.add(config.public_key_id, apiv3.load_public_key(_read(config.public_key_path)))
        self.http = http or httpx.Client(verify=True, timeout=config.timeout_bill_s)
        self._httpx = httpx

    # ------------------------------------------------------------ 传输

    def _headers(self, method: str, url_path: str, body: str, extra: Mapping[str, str] | None = None) -> dict[str, str]:
        headers = {
            "Authorization": apiv3.build_authorization(
                method=method, url_path=url_path, body=body, mchid=self.mchid, serial_no=self.config.merchant_serial_no,
                private_key=self.private_key, timestamp=int(self.clock())),
            "Accept": "application/json", "User-Agent": self.config.user_agent, "Accept-Language": "zh-CN",
        }
        if body:
            headers["Content-Type"] = "application/json"
        if self.config.public_key_id:
            headers["Wechatpay-Serial"] = self.config.public_key_id
        headers.update(extra or {})
        return headers

    def _send(self, method: str, url_path: str, body: Mapping[str, Any] | None = None, *,
              extra_headers: Mapping[str, str] | None = None, verify: bool = True, raw: bool = False) -> Any:
        text = dumps(body) if body is not None else ""
        headers = self._headers(method, url_path, text, extra_headers)
        response = None
        last_exc: Exception | None = None
        for base in (self.config.base_url, self.config.backup_base_url):
            try:
                response = self.http.request(method, base + url_path, content=text.encode("utf-8") if text else None,
                                             headers=headers)
                break
            except self._httpx.TimeoutException as exc:
                raise TransientWechatPayError("TIMEOUT", "request timed out") from exc  # 超时不切换：可能已受理，交给上层用原参数重试
            except self._httpx.TransportError as exc:  # 连接失败：切备域名（异地接入点）
                last_exc = exc
                continue
        if response is None:
            raise TransientWechatPayError("NETWORK_ERROR", type(last_exc).__name__)
        request_id = response.headers.get("Request-ID", "")
        content = response.content
        if raw:
            if response.status_code != 200:
                raise self._error(response.status_code, content, request_id)
            return content
        if verify:
            try:
                apiv3.verify_signature(headers=apiv3.NotifyHeaders.from_mapping(response.headers), body=content,
                                       public_keys=self.keys, now=int(self.clock()), max_skew=self.config.timestamp_skew_s)
            except apiv3.WechatPayError as exc:
                raise TransientWechatPayError("RESPONSE_SIGNATURE_INVALID", exc.reason, response.status_code, request_id) from exc
        if response.status_code == 202:
            raise TransientWechatPayError("ACCEPTED", "accepted, retry with same params", 202, request_id)
        if response.status_code >= 300:
            raise self._error(response.status_code, content, request_id)
        if not content:
            return {}
        return json.loads(content)

    @staticmethod
    def _error(status: int, content: bytes, request_id: str) -> WechatPayApiError:
        try:
            data = json.loads(content or b"{}")
        except ValueError:
            data = {}
        return classify(str(data.get("code") or f"HTTP_{status}"), status, str(data.get("message") or ""),
                        request_id, data.get("detail"))

    # ------------------------------------------------------------ 接口

    def jsapi_prepay(self, body: Mapping[str, Any]) -> str:
        return self._send("POST", "/v3/pay/transactions/jsapi", body)["prepay_id"]

    def query_by_out_trade_no(self, out_trade_no: str) -> dict[str, Any]:
        path = f"/v3/pay/transactions/out-trade-no/{quote(out_trade_no, safe='')}?{urlencode({'mchid': self.mchid})}"
        return self._send("GET", path)

    def close(self, out_trade_no: str) -> None:
        self._send("POST", f"/v3/pay/transactions/out-trade-no/{quote(out_trade_no, safe='')}/close", {"mchid": self.mchid})

    def create_refund(self, body: Mapping[str, Any]) -> dict[str, Any]:
        return self._send("POST", "/v3/refund/domestic/refunds", body)

    def query_refund(self, out_refund_no: str) -> dict[str, Any]:
        return self._send("GET", f"/v3/refund/domestic/refunds/{quote(out_refund_no, safe='')}")

    def apply_abnormal_refund(self, refund_id: str, body: Mapping[str, Any]) -> dict[str, Any]:
        serial, _ = self.keys.encryption_key()
        return self._send("POST", f"/v3/refund/domestic/refunds/{quote(refund_id, safe='')}/apply-abnormal-refund", body,
                          extra_headers={"Wechatpay-Serial": serial})

    def apply_trade_bill(self, bill_date: str, bill_type: str = "ALL", tar_type: str | None = "GZIP") -> dict[str, Any]:
        query = {"bill_date": bill_date, "bill_type": bill_type}
        if tar_type:
            query["tar_type"] = tar_type
        return self._send("GET", f"/v3/bill/tradebill?{urlencode(query)}")

    def download_bill(self, download_url: str) -> bytes:
        parts = urlsplit(download_url)
        path = parts.path + (f"?{parts.query}" if parts.query else "")
        return self._send("GET", path, verify=False, raw=True)  # 下载应答不带签名头，改校验哈希

    def encrypt_sensitive(self, plaintext: str) -> str:
        _, key = self.keys.encryption_key()
        return apiv3.encrypt_sensitive(key, plaintext)

    def jsapi_pay_params(self, prepay_id: str) -> dict[str, str]:
        return apiv3.jsapi_pay_params(appid=self.appid, prepay_id=prepay_id, private_key=self.private_key,
                                      timestamp=int(self.clock()))

    def parse_notification(self, headers: Mapping[str, str], body: bytes, now: int | None = None) -> tuple[dict, dict]:
        return apiv3.parse_notify(headers=headers, body=body, public_keys=self.keys, apiv3_key=self.config.apiv3_key,
                                  now=int(self.clock()) if now is None else now, max_skew=self.config.timestamp_skew_s)


# ---------------------------------------------------------------- 小程序发货信息管理


class ShippingApiError(Exception):
    def __init__(self, errcode: int, errmsg: str = ""):
        super().__init__(f"{errcode}: {errmsg}")
        self.errcode = errcode
        self.errmsg = errmsg


# 系统繁忙可重试（api_uploadshippinginfo）
SHIPPING_RETRYABLE = frozenset({-1, 10060012, 10060019})
SHIPPING_ALREADY_DONE = 10060002


@runtime_checkable
class ShippingClient(Protocol):
    def upload_shipping_info(self, payload: Mapping[str, Any]) -> dict[str, Any]: ...


class HttpShippingClient:
    """`POST https://api.weixin.qq.com/wxa/sec/order/upload_shipping_info?access_token=…`。

    access_token 由注入的提供者给出（小程序服务端凭证，属于 identity / 小程序服务端模块）。
    """

    URL = "https://api.weixin.qq.com/wxa/sec/order/upload_shipping_info"

    def __init__(self, access_token: Callable[[], str], http: Any = None, timeout_s: float = 5):
        import httpx

        self.access_token = access_token
        self.http = http or httpx.Client(verify=True, timeout=timeout_s)
        self._httpx = httpx

    def upload_shipping_info(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        try:
            response = self.http.post(self.URL, params={"access_token": self.access_token()},
                                      content=dumps(payload).encode(), headers={"Content-Type": "application/json"})
        except self._httpx.HTTPError as exc:
            raise TransientError(str(exc)) from exc
        data = response.json()
        code = int(data.get("errcode", 0))
        if code != 0:
            raise ShippingApiError(code, str(data.get("errmsg", "")))
        return data
