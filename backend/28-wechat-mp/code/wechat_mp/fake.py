"""测试替身：FakeWechatServer（HTTP 层假服务，按官方接口路径、参数与错误码响应）+ FakeMiniProgram
（接到假服务上的真实 HttpMiniProgramClient，带造数辅助方法）。测试不访问网络。

假服务遵守的官方行为：
- stable_token 普通模式在有效期内返回同一 token 与剩余秒数；force_refresh 立即换新，间隔 < 30 秒报 45011。
- wx.login code 与手机号 code 一次性、5 分钟有效；重复使用 40163，无效 40029。
- 订阅消息按 (openid, 模板) 计次，没有次数返回 43101。
- msg_sec_check 只接受 version=2；命中风险词 risky、命中复审词 review。
- media_check_async 返回 trace_id，结果由 media_result() 生成推送事件。
- getwxacodeunlimit scene 超过 32 字符 40169，非法字符 40129；成功返回 image/jpeg 二进制。
"""
from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

from luzback.resilience import BreakerRegistry, TransientError

from .client import SCENE_RE, HttpMiniProgramClient, HttpResponse
from .config import WechatMpConfig

FAKE_PNG = b"\x89PNG\r\n\x1a\nfake-qrcode"


@dataclass
class _Code:
    value: Any
    issued_at: float
    used: bool = False


@dataclass
class FakeWechatServer:
    appid: str = "wx-fake-appid"
    secret: str = "fake-secret"
    clock: Callable[[], float] = time.time
    token_ttl: int = 7200
    token_length: int = 512  # 官方要求存储至少 512 字符：假服务发足长的 token
    risky_words: set[str] = field(default_factory=lambda: {"违禁"})
    review_words: set[str] = field(default_factory=lambda: {"待审"})
    templates: set[str] = field(default_factory=lambda: {"TPL-ORDER"})

    def __post_init__(self) -> None:
        self._lock = threading.Lock()
        self.tokens: dict[str, float] = {}       # token → 过期时间
        self.current_token: str | None = None
        self.last_force_at = -1e18
        self.login_codes: dict[str, _Code] = {}
        self.phone_codes: dict[str, _Code] = {}
        self.session_keys: dict[str, str] = {}   # openid → 当前 session_key
        self.quota: dict[tuple[str, str], int] = {}
        self.sent: list[dict[str, Any]] = []
        self.media_checks: dict[str, dict[str, Any]] = {}
        self.calls: list[str] = []
        self.faults: dict[str, list[Any]] = {}   # path → [errcode | "timeout" | "http500" | callable]

    # ---- 造数

    def add_login_code(self, code: str, openid: str, unionid: str | None = None, session_key: str | None = None) -> None:
        with self._lock:
            self.login_codes[code] = _Code({"openid": openid, "unionid": unionid,
                                            "session_key": session_key or secrets.token_urlsafe(16)}, self.clock())

    def add_phone_code(self, code: str, phone: str, country_code: str = "86", openid: str | None = None) -> None:
        with self._lock:
            self.phone_codes[code] = _Code({"phone": phone, "cc": country_code, "openid": openid}, self.clock())

    def grant_subscription(self, openid: str, template_id: str, times: int = 1) -> None:
        with self._lock:
            self.templates.add(template_id)
            self.quota[(openid, template_id)] = self.quota.get((openid, template_id), 0) + times

    def fail_next(self, path: str, *faults: Any) -> None:
        with self._lock:
            self.faults.setdefault(path, []).extend(faults)

    def expire_all_tokens(self) -> None:
        with self._lock:
            for token in self.tokens:
                self.tokens[token] = self.clock() - 1

    def revoke_token(self, token: str) -> None:
        with self._lock:
            self.tokens.pop(token, None)

    def media_result(self, trace_id: str, suggest: str = "pass", label: int = 100, errcode: int = 0) -> dict[str, Any]:
        """生成 wxa_media_check 推送事件（JSON 格式）。"""
        info = self.media_checks[trace_id]
        return {"ToUserName": "gh_fake", "FromUserName": info["openid"], "CreateTime": int(self.clock()),
                "MsgType": "event", "Event": "wxa_media_check", "appid": self.appid, "trace_id": trace_id,
                "version": 2, "errcode": errcode, "errmsg": "ok" if errcode == 0 else "error",
                "result": {"suggest": suggest, "label": label},
                "detail": [{"strategy": "content_model", "errcode": errcode, "suggest": suggest, "label": label}]}

    # ---- 传输层入口

    def __call__(self, method: str, url: str, params: dict, body: Any, timeout: float) -> HttpResponse:
        path = urlparse(url).path
        with self._lock:
            self.calls.append(path)
            queue = self.faults.get(path)
            fault = queue.pop(0) if queue else None
        if fault is not None:
            if callable(fault):
                fault()
            elif fault == "timeout":
                raise TransientError("timeout")
            elif fault == "http500":
                return HttpResponse(500, {"content-type": "text/plain"}, b"error")
            elif isinstance(fault, int):
                return self._json({"errcode": fault, "errmsg": "injected"})
        handler = {
            "/cgi-bin/stable_token": self._stable_token, "/sns/jscode2session": self._code2session,
            "/wxa/checksession": self._checksession, "/wxa/resetusersessionkey": self._reset,
            "/wxa/business/getuserphonenumber": self._phone, "/cgi-bin/message/subscribe/send": self._send,
            "/wxa/msg_sec_check": self._msg_sec_check, "/wxa/media_check_async": self._media_check,
            "/wxa/getwxacodeunlimit": self._qrcode,
        }.get(path)
        if handler is None:
            return HttpResponse(404, {}, b"not found")
        with self._lock:
            return handler(method, params or {}, body or {})

    @staticmethod
    def _json(data: dict[str, Any]) -> HttpResponse:
        return HttpResponse(200, {"content-type": "application/json"}, json.dumps(data, ensure_ascii=False).encode())

    def _err(self, errcode: int, errmsg: str = "error") -> HttpResponse:
        return self._json({"errcode": errcode, "errmsg": errmsg})

    def _token_ok(self, params: dict) -> HttpResponse | None:
        token = params.get("access_token")
        if token not in self.tokens:
            return self._err(40001, "invalid credential")
        if self.tokens[token] <= self.clock():
            return self._err(42001, "access_token expired")
        return None

    def _take(self, store: dict[str, _Code], code: str) -> tuple[_Code | None, int]:
        item = store.get(code)
        if item is None or self.clock() - item.issued_at > 300:
            return None, 40029
        if item.used:
            return None, 40163
        item.used = True
        return item, 0

    # ---- 接口

    def _stable_token(self, method: str, params: dict, body: dict) -> HttpResponse:
        if method != "POST":
            return self._err(43002, "require POST method")
        if body.get("appid") != self.appid:
            return self._err(40013, "invalid appid")
        if body.get("secret") != self.secret:
            return self._err(40125, "invalid appsecret")
        now = self.clock()
        if body.get("force_refresh"):
            if now - self.last_force_at < 30:
                return self._err(45011, "api minute-quota reach limit")
            self.last_force_at = now
            if self.current_token:
                self.tokens.pop(self.current_token, None)
            self.current_token = None
        if self.current_token is None or self.tokens.get(self.current_token, 0) <= now:
            self.current_token = secrets.token_urlsafe(self.token_length)[: self.token_length]
            self.tokens[self.current_token] = now + self.token_ttl
        return self._json({"access_token": self.current_token, "expires_in": int(self.tokens[self.current_token] - now)})

    def _code2session(self, method: str, params: dict, body: dict) -> HttpResponse:
        if params.get("appid") != self.appid or params.get("secret") != self.secret:
            return self._err(40125, "invalid appsecret")
        if params.get("grant_type") != "authorization_code":
            return self._err(40002, "invalid grant_type")
        item, err = self._take(self.login_codes, str(params.get("js_code", "")))
        if item is None:
            return self._err(err, "invalid code" if err == 40029 else "code been used")
        info = item.value
        self.session_keys[info["openid"]] = info["session_key"]
        data = {"openid": info["openid"], "session_key": info["session_key"]}
        if info["unionid"]:
            data["unionid"] = info["unionid"]
        return self._json(data)

    def _session_sig_ok(self, params: dict) -> bool:
        key = self.session_keys.get(str(params.get("openid", "")))
        if key is None or params.get("sig_method") != "hmac_sha256":
            return False
        expected = hmac.new(key.encode(), b"", hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, str(params.get("signature", "")))

    def _checksession(self, method: str, params: dict, body: dict) -> HttpResponse:
        bad = self._token_ok(params)
        if bad:
            return bad
        return self._err(0, "ok") if self._session_sig_ok(params) else self._err(87009, "invalid signature")

    def _reset(self, method: str, params: dict, body: dict) -> HttpResponse:
        bad = self._token_ok(params)
        if bad:
            return bad
        if not self._session_sig_ok(params):
            return self._err(87009, "invalid signature")
        new_key = secrets.token_urlsafe(16)
        self.session_keys[params["openid"]] = new_key
        return self._json({"errcode": 0, "errmsg": "ok", "openid": params["openid"], "session_key": new_key})

    def _phone(self, method: str, params: dict, body: dict) -> HttpResponse:
        bad = self._token_ok(params)
        if bad:
            return bad
        item, err = self._take(self.phone_codes, str(body.get("code", "")))
        if item is None:
            return self._err(err, "invalid code")
        info = item.value
        if body.get("openid") and info["openid"] and body["openid"] != info["openid"]:
            return self._err(40029, "code does not belong to openid")
        full = info["phone"] if info["cc"] == "86" else f"+{info['cc']}{info['phone']}"
        return self._json({"errcode": 0, "errmsg": "ok", "phone_info": {
            "phoneNumber": full, "purePhoneNumber": info["phone"], "countryCode": info["cc"],
            "watermark": {"timestamp": int(self.clock()), "appid": self.appid}}})

    def _send(self, method: str, params: dict, body: dict) -> HttpResponse:
        bad = self._token_ok(params)
        if bad:
            return bad
        template = body.get("template_id")
        if template not in self.templates:
            return self._err(40037, "invalid template_id")
        key = (body.get("touser"), template)
        if self.quota.get(key, 0) <= 0:
            return self._err(43101, "user refuse to accept the msg")
        self.quota[key] -= 1
        self.sent.append(body)
        return self._err(0, "ok")

    def _msg_sec_check(self, method: str, params: dict, body: dict) -> HttpResponse:
        bad = self._token_ok(params)
        if bad:
            return bad
        if body.get("version") != 2:
            return self._err(47001, "data format error")
        content = str(body.get("content", ""))
        if any(word in content for word in self.risky_words):
            suggest, label = "risky", 20001
        elif any(word in content for word in self.review_words):
            suggest, label = "review", 21000
        else:
            suggest, label = "pass", 100
        return self._json({"errcode": 0, "errmsg": "ok", "trace_id": secrets.token_hex(12),
                           "result": {"suggest": suggest, "label": label},
                           "detail": [{"strategy": "content_model", "errcode": 0, "suggest": suggest, "label": label}]})

    def _media_check(self, method: str, params: dict, body: dict) -> HttpResponse:
        bad = self._token_ok(params)
        if bad:
            return bad
        if body.get("media_type") not in (1, 2):
            return self._err(40004, "invalid media type")
        trace_id = secrets.token_hex(12)
        self.media_checks[trace_id] = {"openid": body.get("openid"), "url": body.get("media_url")}
        return self._json({"errcode": 0, "errmsg": "ok", "trace_id": trace_id})

    def _qrcode(self, method: str, params: dict, body: dict) -> HttpResponse:
        bad = self._token_ok(params)
        if bad:
            return bad
        scene = str(body.get("scene", ""))
        if len(scene) > 32:
            return self._err(40169, "invalid length for scene")
        if not SCENE_RE.match(scene):
            return self._err(40129, "invalid scene")
        if body.get("env_version") not in ("release", "trial", "develop"):
            return self._err(40097, "invalid args")
        return HttpResponse(200, {"content-type": "image/jpeg"}, FAKE_PNG)


class FakeMiniProgram(HttpMiniProgramClient):
    """接到 FakeWechatServer 的客户端：走真实的请求、错误映射、token 缓存与熔断代码路径。"""

    def __init__(self, server: FakeWechatServer | None = None, **kwargs: Any):
        self.server = server or FakeWechatServer()
        config = kwargs.pop("config", None) or WechatMpConfig(appid=self.server.appid, secret=self.server.secret,
                                                              api_base="https://fake.weixin.local")
        kwargs.setdefault("breakers", BreakerRegistry())
        kwargs.setdefault("sleep", lambda _s: None)
        super().__init__(config=config, transport=self.server, **kwargs)

    # 造数辅助（委托给假服务）
    def add_login_code(self, code: str, openid: str, unionid: str | None = None, session_key: str | None = None) -> None:
        self.server.add_login_code(code, openid, unionid, session_key)

    def add_phone_code(self, code: str, phone: str, country_code: str = "86", openid: str | None = None) -> None:
        self.server.add_phone_code(code, phone, country_code, openid)

    def grant_subscription(self, openid: str, template_id: str, times: int = 1) -> None:
        self.server.grant_subscription(openid, template_id, times)

    def media_result(self, trace_id: str, suggest: str = "pass", label: int = 100, errcode: int = 0) -> dict[str, Any]:
        return self.server.media_result(trace_id, suggest, label, errcode)
