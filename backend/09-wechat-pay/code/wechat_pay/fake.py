"""微信支付测试替身（内存假服务）：实现 `WechatPayClient`，并能当作 HTTP 假服务器挂到 httpx.MockTransport。

- 使用真实密码学：商户密钥对签 paySign / 请求，平台（微信支付公钥）密钥对签回调与应答，APIv3 密钥加密 resource。
- 模拟官方规则：同单号同参数可重复下单、参数不同报 OUT_TRADE_NO_USED；关单可重入；同退款单号幂等；
  累计退款不超过订单金额；状态流转 NOTPAY → SUCCESS / CLOSED，SUCCESS → REFUND。
- 故障注入：`fail_next(op, code)`（SYSTEM_ERROR / FREQUENCY_LIMITED / NOT_ENOUGH …）、`timeout_next(op)`、
  `apply=True` 表示“渠道已处理但应答丢失”；账单哈希篡改；签名探测 / 篡改 / 过期回调。
- 开发环境（WECHAT_PAY_CLIENT=fake）也用它，生产禁止（config.production_problems）。
"""
from __future__ import annotations

import gzip
import json
import secrets
import threading
import time
from collections.abc import Callable, Mapping
from datetime import date, datetime, timezone
from functools import lru_cache
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit
from zoneinfo import ZoneInfo

from cryptography.hazmat.primitives.asymmetric import rsa

from . import apiv3
from .client import ShippingApiError, TransientWechatPayError, classify, dumps

FAKE_PUBLIC_KEY_ID = "PUB_KEY_ID_0000000000000000000000000000000001"
FAKE_MERCHANT_SERIAL = "FAKE0000MERCHANT0000SERIAL0000000000000001"
FAKE_APIV3_KEY = "0123456789abcdefghijklmnopqrstuv"  # 32 字节测试密钥，仅用于假服务
BILL_COLUMNS = ["交易时间", "公众账号ID", "商户号", "特约商户号", "设备号", "微信订单号", "商户订单号", "用户标识", "交易类型",
                "交易状态", "付款银行", "货币种类", "应结订单金额", "代金券金额", "微信退款单号", "商户退款单号", "退款金额",
                "充值券退款金额", "退款类型", "退款状态", "商品名称", "商户数据包", "手续费", "费率", "订单金额", "申请退款金额", "费率备注"]


@lru_cache(maxsize=8)
def fake_keypair(name: str) -> rsa.RSAPrivateKey:
    """按名称缓存的 2048 位测试密钥（只在进程内存中）。"""
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _yuan(cents: int) -> str:
    return f"{cents // 100}.{cents % 100:02d}"


def _rfc3339(ts: float) -> str:
    return datetime.fromtimestamp(ts, ZoneInfo("Asia/Shanghai")).replace(microsecond=0).isoformat()


class FakeWechatPay:
    def __init__(self, *, appid: str = "wx-dev-appid", mchid: str = "1900000000", apiv3_key: str = FAKE_APIV3_KEY,
                 clock: Callable[[], float] = time.time, platform_serial: str = FAKE_PUBLIC_KEY_ID,
                 merchant_key: rsa.RSAPrivateKey | None = None, platform_key: rsa.RSAPrivateKey | None = None):
        self.appid = appid
        self.mchid = mchid
        self.apiv3_key = apiv3_key
        self.clock = clock
        self.merchant_key = merchant_key or fake_keypair("merchant")
        self.platform_key = platform_key or fake_keypair("platform")
        self.platform_serial = platform_serial
        self.keys = apiv3.PublicKeyStore()
        self.keys.add(platform_serial, self.platform_key.public_key())
        self.orders: dict[str, dict[str, Any]] = {}
        self.refunds: dict[str, dict[str, Any]] = {}
        self.abnormal_requests: list[dict[str, Any]] = []
        self.calls: list[tuple[str, Any]] = []
        self._failures: dict[str, list[tuple[Exception, bool]]] = {}
        self._lock = threading.RLock()
        self.bill_overrides: dict[str, bytes] = {}
        self.bill_hash_overrides: dict[str, str] = {}
        self.bill_status: dict[str, str] = {}   # date → NO_STATEMENT_EXIST | STATEMENT_CREATING
        self._bill_tokens: dict[str, tuple[str, str | None]] = {}

    # ------------------------------------------------------------ 故障注入

    def fail_next(self, op: str, code: str = "SYSTEM_ERROR", http_status: int | None = None, times: int = 1,
                  apply: bool = False, message: str = "") -> None:
        """下一次（或 times 次）调用 op 失败；apply=True 表示渠道已执行但应答丢失。"""
        status = http_status or {"SYSTEM_ERROR": 500, "FREQUENCY_LIMITED": 429, "NOT_ENOUGH": 403, "PARAM_ERROR": 400,
                                 "INVALID_REQUEST": 400, "SIGN_ERROR": 401, "USER_ACCOUNT_ABNORMAL": 403,
                                 "RESOURCE_NOT_EXISTS": 404, "ORDER_NOT_EXIST": 404, "TRADE_ERROR": 403,
                                 "STATEMENT_CREATING": 400, "NO_STATEMENT_EXIST": 400}.get(code, 400)
        for _ in range(times):
            self._failures.setdefault(op, []).append((classify(code, status, message or code), apply))

    def timeout_next(self, op: str, times: int = 1, apply: bool = False) -> None:
        for _ in range(times):
            self._failures.setdefault(op, []).append((TransientWechatPayError("TIMEOUT", "fake timeout"), apply))

    def _enter(self, op: str, arg: Any = None) -> tuple[Exception, bool] | None:
        self.calls.append((op, arg))
        queue = self._failures.get(op)
        if queue:
            failure = queue.pop(0)
            if not failure[1]:
                raise failure[0]
            return failure
        return None

    def count(self, op: str) -> int:
        return sum(1 for name, _ in self.calls if name == op)

    # ------------------------------------------------------------ 支付

    def jsapi_prepay(self, body: Mapping[str, Any]) -> str:
        with self._lock:
            pending = self._enter("jsapi_prepay", body.get("out_trade_no"))
            out_trade_no = str(body.get("out_trade_no", ""))
            if body.get("appid") != self.appid or body.get("mchid") != self.mchid:
                raise classify("APPID_MCHID_NOT_MATCH", 400, "appid 与 mchid 不匹配")
            if int((body.get("amount") or {}).get("total") or 0) < 1:
                raise classify("PARAM_ERROR", 400, "amount.total 值低于最小值 1")
            if not apiv3_out_trade_no_ok(out_trade_no):
                raise classify("PARAM_ERROR", 400, "out_trade_no 格式错误")
            text = dumps(body)
            existing = self.orders.get(out_trade_no)
            if existing is not None:
                if existing["body_text"] != text:
                    raise classify("OUT_TRADE_NO_USED", 403, "商户订单号重复")
                if existing["trade_state"] != "NOTPAY":
                    raise classify("TRADE_ERROR", 403, "订单状态不允许下单")
            else:
                existing = self.orders[out_trade_no] = {
                    "body": dict(body), "body_text": text, "trade_state": "NOTPAY", "transaction_id": None,
                    "success_time": None, "prepay_ids": [], "created": self.clock()}
            prepay_id = "wx" + secrets.token_hex(15)
            existing["prepay_ids"].append(prepay_id)
            if pending:
                raise pending[0]
            return prepay_id

    def _transaction(self, order: dict[str, Any]) -> dict[str, Any]:
        body = order["body"]
        data: dict[str, Any] = {
            "appid": body["appid"], "mchid": body["mchid"], "out_trade_no": body["out_trade_no"], "trade_type": "JSAPI",
            "trade_state": order["trade_state"], "trade_state_desc": order["trade_state"],
            "amount": {"total": order.get("paid_total", body["amount"]["total"]), "currency": "CNY"},
        }
        if order["transaction_id"]:
            data.update({"transaction_id": order["transaction_id"], "bank_type": "OTHERS",
                         "success_time": _rfc3339(order["success_time"]), "payer": dict(body.get("payer") or {})})
            data["amount"].update({"payer_total": data["amount"]["total"], "payer_currency": "CNY"})
        if body.get("attach"):
            data["attach"] = body["attach"]
        return data

    def pay(self, out_trade_no: str, *, amount: int | None = None, at: float | None = None) -> dict[str, Any]:
        """模拟用户支付成功；amount 可篡改为不一致金额以测试核对。"""
        with self._lock:
            order = self.orders[out_trade_no]
            if order["trade_state"] != "NOTPAY":
                raise ValueError(f"order is {order['trade_state']}")
            order["trade_state"] = "SUCCESS"
            order["transaction_id"] = "42" + secrets.token_hex(13)
            order["success_time"] = self.clock() if at is None else at
            if amount is not None:
                order["paid_total"] = amount
            return self._transaction(order)

    def query_by_out_trade_no(self, out_trade_no: str) -> dict[str, Any]:
        with self._lock:
            self._enter("query", out_trade_no)
            order = self.orders.get(out_trade_no)
            if order is None:
                raise classify("ORDER_NOT_EXIST", 404, "订单不存在")
            return self._transaction(order)

    def close(self, out_trade_no: str) -> None:
        with self._lock:
            pending = self._enter("close", out_trade_no)
            order = self.orders.get(out_trade_no)
            if order is None:
                raise classify("ORDER_NOT_EXIST", 404, "订单不存在")
            if order["trade_state"] in ("SUCCESS", "REFUND"):
                raise classify("TRADE_ERROR", 403, "订单已支付")
            order["trade_state"] = "CLOSED"  # 关单可重入
            if pending:
                raise pending[0]

    # ------------------------------------------------------------ 退款

    def _refund_view(self, refund: dict[str, Any]) -> dict[str, Any]:
        data = {
            "refund_id": refund["refund_id"], "out_refund_no": refund["out_refund_no"],
            "transaction_id": refund["transaction_id"], "out_trade_no": refund["out_trade_no"],
            "channel": "ORIGINAL", "user_received_account": "支付用户零钱", "status": refund["status"],
            "create_time": _rfc3339(refund["created"]),
            "amount": {"total": refund["total"], "refund": refund["amount"], "payer_total": refund["total"],
                       "payer_refund": refund["amount"], "currency": "CNY"},
        }
        if refund.get("success_time"):
            data["success_time"] = _rfc3339(refund["success_time"])
        return data

    def create_refund(self, body: Mapping[str, Any]) -> dict[str, Any]:
        with self._lock:
            pending = self._enter("create_refund", body.get("out_refund_no"))
            out_refund_no = str(body["out_refund_no"])
            amount = int(body["amount"]["refund"])
            existing = self.refunds.get(out_refund_no)
            if existing is not None:  # 同一退款单号多次请求只退一笔
                if existing["amount"] != amount:
                    raise classify("INVALID_REQUEST", 400, "REFUND_FEE_MISMATCH")
                if pending:
                    raise pending[0]
                return self._refund_view(existing)
            order = self.orders.get(str(body.get("out_trade_no", "")))
            if order is None or order["trade_state"] not in ("SUCCESS", "REFUND"):
                raise classify("RESOURCE_NOT_EXISTS", 404, "订单不存在或未支付")
            total = int(body["amount"]["total"])
            if total != order["body"]["amount"]["total"]:
                raise classify("PARAM_ERROR", 400, "amount.total 与原订单金额不一致")
            used = sum(r["amount"] for r in self.refunds.values()
                       if r["out_trade_no"] == order["body"]["out_trade_no"] and r["status"] != "CLOSED")
            if used + amount > total:
                raise classify("INVALID_REQUEST", 400, "退款金额超过可退金额")
            refund = {"out_refund_no": out_refund_no, "refund_id": "50" + secrets.token_hex(13),
                      "transaction_id": order["transaction_id"], "out_trade_no": order["body"]["out_trade_no"],
                      "amount": amount, "total": total, "status": "PROCESSING", "created": self.clock(), "success_time": None}
            self.refunds[out_refund_no] = refund
            order["trade_state"] = "REFUND"
            if pending:
                raise pending[0]
            return self._refund_view(refund)

    def settle_refund(self, out_refund_no: str, status: str = "SUCCESS", at: float | None = None) -> dict[str, Any]:
        """渠道侧把退款推进到 SUCCESS / ABNORMAL / CLOSED。"""
        with self._lock:
            refund = self.refunds[out_refund_no]
            refund["status"] = status
            if status == "SUCCESS":
                refund["success_time"] = self.clock() if at is None else at
            return self._refund_view(refund)

    def query_refund(self, out_refund_no: str) -> dict[str, Any]:
        with self._lock:
            self._enter("query_refund", out_refund_no)
            refund = self.refunds.get(out_refund_no)
            if refund is None:
                raise classify("RESOURCE_NOT_EXISTS", 404, "退款单不存在")
            return self._refund_view(refund)

    def apply_abnormal_refund(self, refund_id: str, body: Mapping[str, Any]) -> dict[str, Any]:
        with self._lock:
            self._enter("apply_abnormal_refund", refund_id)
            refund = next((r for r in self.refunds.values() if r["refund_id"] == refund_id), None)
            if refund is None:
                raise classify("RESOURCE_NOT_EXISTS", 404, "退款单不存在")
            if refund["status"] != "ABNORMAL":
                raise classify("INVALID_REQUEST", 400, "退款单不是异常状态")
            request = dict(body)
            for name in ("bank_account", "real_name"):  # 能用平台私钥解开，证明按官方 OAEP 加密
                if request.get(name):
                    request[name] = apiv3.decrypt_sensitive(self.platform_key, request[name])
            self.abnormal_requests.append(request)
            refund["status"] = "PROCESSING"
            return self._refund_view(refund)

    # ------------------------------------------------------------ 账单

    def bill_text(self, bill_date: str) -> str:
        tz = ZoneInfo("Asia/Shanghai")
        day = date.fromisoformat(bill_date)

        def on_day(ts: float | None) -> bool:
            return ts is not None and datetime.fromtimestamp(ts, tz).date() == day

        lines = [",".join(BILL_COLUMNS)]
        count = total = refund_total = 0

        def row(values: dict[str, str]) -> str:
            return ",".join("`" + values.get(column, "") for column in BILL_COLUMNS)

        for order in self.orders.values():
            if order["transaction_id"] and on_day(order["success_time"]):
                amount = order.get("paid_total", order["body"]["amount"]["total"])
                lines.append(row({"交易时间": _rfc3339(order["success_time"])[:19].replace("T", " "), "公众账号ID": self.appid,
                                  "商户号": self.mchid, "特约商户号": "0", "微信订单号": order["transaction_id"],
                                  "商户订单号": order["body"]["out_trade_no"], "交易类型": "JSAPI", "交易状态": "SUCCESS",
                                  "货币种类": "CNY", "应结订单金额": _yuan(amount), "代金券金额": "0.00", "微信退款单号": "0",
                                  "商户退款单号": "0", "退款金额": "0.00", "商品名称": order["body"].get("description", ""),
                                  "订单金额": _yuan(amount), "申请退款金额": "0.00"}))
                count += 1
                total += amount
        for refund in self.refunds.values():
            when = refund["success_time"] or refund["created"]
            if on_day(when):
                lines.append(row({"交易时间": _rfc3339(when)[:19].replace("T", " "), "公众账号ID": self.appid, "商户号": self.mchid,
                                  "特约商户号": "0", "微信订单号": refund["transaction_id"], "商户订单号": refund["out_trade_no"],
                                  "交易类型": "JSAPI", "交易状态": "REFUND", "货币种类": "CNY", "微信退款单号": refund["refund_id"],
                                  "商户退款单号": refund["out_refund_no"], "退款金额": _yuan(refund["amount"]),
                                  "退款类型": "ORIGINAL", "退款状态": "SUCCESS" if refund["status"] == "SUCCESS" else "PROCESSING",
                                  "申请退款金额": _yuan(refund["amount"])}))
                refund_total += refund["amount"]
        lines.append("总交易单数,应结订单总金额,退款总金额,充值券退款总金额,手续费总金额,订单总金额,申请退款总金额")
        lines.append(",".join("`" + v for v in (str(count), _yuan(total), _yuan(refund_total), "0.00", "0.00",
                                                 _yuan(total), _yuan(refund_total))))
        return "\r\n".join(lines) + "\r\n"

    def apply_trade_bill(self, bill_date: str, bill_type: str = "ALL", tar_type: str | None = "GZIP") -> dict[str, Any]:
        with self._lock:
            self._enter("apply_trade_bill", bill_date)
            status = self.bill_status.get(bill_date)
            if status:
                raise classify(status, 400, status)
            content = self.bill_overrides.get(bill_date) or self.bill_text(bill_date).encode("utf-8")
            if len(content.splitlines()) <= 3 and bill_date not in self.bill_overrides:  # 只有表头与汇总：当天无交易
                raise classify("NO_STATEMENT_EXIST", 400, "无交易")
            token = secrets.token_hex(8)
            self._bill_tokens[token] = (bill_date, tar_type)
            return {"hash_type": "SHA1", "hash_value": self.bill_hash_overrides.get(bill_date) or apiv3.sha1_hex(content),
                    "download_url": f"https://api.mch.weixin.qq.com/v3/billdownload/file?token={token}"}

    def download_bill(self, download_url: str) -> bytes:
        with self._lock:
            self._enter("download_bill", download_url)
            token = parse_qs(urlsplit(download_url).query).get("token", [""])[0]
            if token not in self._bill_tokens:
                raise classify("INVALID_REQUEST", 400, "下载地址无效")
            bill_date, tar_type = self._bill_tokens[token]
            content = self.bill_overrides.get(bill_date) or self.bill_text(bill_date).encode("utf-8")
            return gzip.compress(content) if tar_type == "GZIP" else content

    # ------------------------------------------------------------ 签名 / 回调

    def encrypt_sensitive(self, plaintext: str) -> str:
        return apiv3.encrypt_sensitive(self.platform_key.public_key(), plaintext)

    def jsapi_pay_params(self, prepay_id: str) -> dict[str, str]:
        return apiv3.jsapi_pay_params(appid=self.appid, prepay_id=prepay_id, private_key=self.merchant_key,
                                      timestamp=int(self.clock()))

    def parse_notification(self, headers: Mapping[str, str], body: bytes, now: int | None = None) -> tuple[dict, dict]:
        return apiv3.parse_notify(headers=headers, body=body, public_keys=self.keys, apiv3_key=self.apiv3_key,
                                  now=int(self.clock()) if now is None else now)

    def signed_notification(self, event_type: str, plain: Mapping[str, Any], *, original_type: str = "transaction",
                            associated_data: str = "transaction", timestamp: int | None = None,
                            signature: str | None = None, serial: str | None = None,
                            tamper: bool = False) -> tuple[dict[str, str], bytes]:
        """按官方格式生成签名回调：(请求头, 原始报文)。tamper=True 时签名后改动报文。"""
        envelope = {
            "id": "EV-" + secrets.token_hex(8), "create_time": _rfc3339(self.clock()), "resource_type": "encrypt-resource",
            "event_type": event_type, "summary": "支付成功" if event_type.startswith("TRANSACTION") else "退款状态变更",
            "resource": apiv3.encrypt_resource(plain, self.apiv3_key, associated_data=associated_data,
                                               original_type=original_type),
        }
        body = json.dumps(envelope, ensure_ascii=False).encode()
        headers = apiv3.response_headers(body, serial=serial or self.platform_serial, private_key=self.platform_key,
                                         timestamp=int(self.clock()) if timestamp is None else timestamp, signature=signature)
        if tamper:
            body = body.replace(b'"summary"', b'"summary" ', 1)
        return headers, body

    def payment_notification(self, out_trade_no: str, **kwargs: Any) -> tuple[dict[str, str], bytes]:
        return self.signed_notification("TRANSACTION.SUCCESS", self._transaction(self.orders[out_trade_no]), **kwargs)

    def refund_notification(self, out_refund_no: str, **kwargs: Any) -> tuple[dict[str, str], bytes]:
        view = self._refund_view(self.refunds[out_refund_no])
        status = view.pop("status")
        plain = {"mchid": self.mchid, "refund_status": status, **{k: v for k, v in view.items() if k != "channel"}}
        kwargs.setdefault("original_type", "refund")
        kwargs.setdefault("associated_data", "refund")
        return self.signed_notification(f"REFUND.{status}", plain, **kwargs)

    # ------------------------------------------------------------ HTTP 假服务器

    def http_handler(self, merchant_serial: str = FAKE_MERCHANT_SERIAL, *, probe_once: set[str] | None = None):
        """返回 httpx.MockTransport 的处理函数：校验请求签名 → 分派 → 用平台密钥签名应答。

        probe_once：对这些路径的第一次请求下发签名探测值（WECHATPAY/SIGNTEST/…）。
        """
        import httpx

        probes = set(probe_once or ())
        merchant_public = self.merchant_key.public_key()

        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.raw_path.decode()
            body = request.content.decode("utf-8")
            try:
                auth = apiv3.verify_request(request.headers.get("Authorization", ""), request.method, path, body, merchant_public)
                if auth["serial_no"] != merchant_serial or auth["mchid"] != self.mchid:
                    raise apiv3.WechatPayError("bad_signature", "serial or mchid")
                if not request.headers.get("User-Agent"):
                    raise apiv3.WechatPayError("bad_request", "missing UA")
            except apiv3.WechatPayError:
                return self._signed_response(401, {"code": "SIGN_ERROR", "message": "签名错误"})
            try:
                status, payload, raw = self._dispatch(request.method, path, json.loads(body) if body else None)
            except Exception as exc:  # noqa: BLE001 - 渠道错误转成应答
                code = getattr(exc, "code", "SYSTEM_ERROR")
                http_status = getattr(exc, "http_status", 500) or 500
                if code == "TIMEOUT":
                    raise httpx.ReadTimeout("fake timeout", request=request) from exc
                return self._signed_response(http_status, {"code": code, "message": str(exc)})
            if raw is not None:
                return httpx.Response(200, content=raw)  # 下载应答不带签名头
            route = path.split("?")[0]
            signature = None
            if route in probes:
                probes.discard(route)
                signature = "WECHATPAY/SIGNTEST/" + secrets.token_hex(8)
            return self._signed_response(status, payload, signature=signature)

        return handler

    def _signed_response(self, status: int, payload: Any, signature: str | None = None):
        import httpx

        content = b"" if payload is None else json.dumps(payload, ensure_ascii=False).encode()
        headers = apiv3.response_headers(content, serial=self.platform_serial, private_key=self.platform_key,
                                         timestamp=int(self.clock()), signature=signature)
        headers["Request-ID"] = secrets.token_hex(8)
        return httpx.Response(status, content=content, headers=headers)

    def _dispatch(self, method: str, path: str, body: Any) -> tuple[int, Any, bytes | None]:
        route, _, query = path.partition("?")
        params = {k: v[0] for k, v in parse_qs(query).items()}
        if method == "POST" and route == "/v3/pay/transactions/jsapi":
            return 200, {"prepay_id": self.jsapi_prepay(body)}, None
        if method == "GET" and route.startswith("/v3/pay/transactions/out-trade-no/"):
            return 200, self.query_by_out_trade_no(unquote(route.rsplit("/", 1)[1])), None
        if method == "POST" and route.endswith("/close"):
            self.close(unquote(route.split("/")[-2]))
            return 204, None, None
        if method == "POST" and route == "/v3/refund/domestic/refunds":
            return 200, self.create_refund(body), None
        if method == "POST" and route.endswith("/apply-abnormal-refund"):
            return 200, self.apply_abnormal_refund(unquote(route.split("/")[-2]), body), None
        if method == "GET" and route.startswith("/v3/refund/domestic/refunds/"):
            return 200, self.query_refund(unquote(route.rsplit("/", 1)[1])), None
        if method == "GET" and route == "/v3/bill/tradebill":
            return 200, self.apply_trade_bill(params["bill_date"], params.get("bill_type", "ALL"), params.get("tar_type")), None
        if method == "GET" and route == "/v3/billdownload/file":
            return 200, None, self.download_bill("https://api.mch.weixin.qq.com" + path)
        return 404, {"code": "NOT_FOUND", "message": "no route"}, None


def apiv3_out_trade_no_ok(value: str) -> bool:
    from .rules import OUT_TRADE_NO_RE

    return bool(OUT_TRADE_NO_RE.match(value))


class FakeShippingClient:
    """发货信息录入替身：记录请求，支持错误码注入；同一支付单重复上传返回 10060002 之外的成功（首次）。"""

    def __init__(self):
        self.uploads: list[dict[str, Any]] = []
        self._failures: list[Exception] = []
        self._lock = threading.Lock()

    def fail_next(self, errcode: int, times: int = 1, transient: bool = False) -> None:
        from luzback import TransientError

        for _ in range(times):
            self._failures.append(TransientError("network") if transient else ShippingApiError(errcode, "fake"))

    def upload_shipping_info(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        with self._lock:
            if self._failures:
                raise self._failures.pop(0)
            key = json.dumps(payload["order_key"], sort_keys=True)
            if any(json.dumps(u["order_key"], sort_keys=True) == key for u in self.uploads):
                raise ShippingApiError(10060002, "支付单已完成发货")
            self.uploads.append(dict(payload))
            return {"errcode": 0, "errmsg": "ok"}


def utc_ts(dt: datetime) -> float:
    return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).timestamp()
