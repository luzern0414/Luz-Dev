"""小程序服务端接口客户端：协议（MiniProgramClient）+ HTTP 实现（HttpMiniProgramClient）。

规则（MODULE.md §4）：
- 所有调用经框架 `resilient`（超时 + 熔断，按接口名各一个熔断器）；只有幂等调用重试 1 次
  （stable_token 普通模式、checksession、msg_sec_check、getwxacodeunlimit）；code2Session、手机号、
  订阅消息、异步媒体检测、重置 session_key 不重试（code 一次性 / 有副作用）。
- 业务接口返回 40001 / 42001 / 40014：失效中控缓存后用新 token 重试一次（请求未被执行，可安全重试）。
- errcode 不存在或为 0 即成功；-1 系统繁忙视为瞬时错误；其他 errcode 映射为类型化异常（errors.py）。
- 日志与异常信息不包含 secret、access_token、session_key、手机号。
"""
from __future__ import annotations

import hashlib
import hmac
import json
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

from luzback import AppError, ErrorCode
from luzback.resilience import BreakerRegistry, TransientError, resilient

from .config import WechatMpConfig, get_config
from .errors import SYSTEM_BUSY, AccessTokenInvalid, SessionKeyInvalid, WechatError, error_for
from .token import AccessTokenCache, TokenStore

# 小程序码 scene：最多 32 个可见字符，只允许数字、大小写字母与 !#$&'()*+,/:;=?@-._~
SCENE_RE = re.compile(r"^[0-9A-Za-z!#$&'()*+,/:;=?@\-._~]{1,32}$")
MSG_SEC_SCENES = {1, 2, 3, 4}  # 1 资料 2 评论 3 论坛 4 社交日志
MEDIA_TYPES = {1, 2}  # 1 音频 2 图片
CONTENT_MAX = 2500

breakers = BreakerRegistry()


# ---------------------------------------------------------------- 结果类型

@dataclass(frozen=True)
class Code2SessionResult:
    openid: str
    session_key: str
    unionid: str | None = None


@dataclass(frozen=True)
class PhoneInfo:
    phone_number: str        # 带区号（国外号码）
    pure_phone_number: str   # 不带区号
    country_code: str
    watermark_appid: str | None = None
    watermark_timestamp: int | None = None


@dataclass(frozen=True)
class SecCheckResult:
    suggest: str             # pass | review | risky
    label: int
    trace_id: str | None = None
    detail: tuple[dict, ...] = ()

    @property
    def passed(self) -> bool:
        return self.suggest == "pass"

    @property
    def risky(self) -> bool:
        return self.suggest == "risky"


# ---------------------------------------------------------------- 协议

class MiniProgramClient(Protocol):
    def access_token(self) -> str: ...
    def code2session(self, js_code: str) -> Code2SessionResult: ...
    def check_session_key(self, openid: str, session_key: str) -> bool: ...
    def reset_session_key(self, openid: str, session_key: str) -> str: ...
    def get_phone_number(self, code: str, openid: str | None = None) -> PhoneInfo: ...
    def send_subscribe_message(self, touser: str, template_id: str, data: Mapping[str, Any], page: str | None = None,
                               miniprogram_state: str | None = None, lang: str | None = None) -> dict[str, Any]: ...
    def msg_sec_check(self, openid: str, content: str, scene: int, title: str | None = None,
                      nickname: str | None = None, signature: str | None = None) -> SecCheckResult: ...
    def media_check_async(self, openid: str, media_url: str, media_type: int, scene: int) -> str: ...
    def get_unlimited_qrcode(self, scene: str, page: str | None = None, check_path: bool = True,
                             env_version: str | None = None, width: int = 430, auto_color: bool = False,
                             line_color: Mapping[str, int] | None = None, is_hyaline: bool = False) -> bytes: ...


# ---------------------------------------------------------------- 传输层

@dataclass
class HttpResponse:
    status: int
    headers: dict[str, str]
    content: bytes

    def json(self) -> Any:
        return json.loads(self.content.decode("utf-8"))

    @property
    def content_type(self) -> str:
        return self.headers.get("content-type", "")


Transport = Callable[[str, str, dict, Any, float], HttpResponse]  # method, url, params, json_body, timeout


def httpx_transport(method: str, url: str, params: dict, body: Any, timeout: float) -> HttpResponse:  # pragma: no cover - 真实网络
    import httpx

    content = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
    headers = {"Content-Type": "application/json; charset=utf-8"} if content is not None else {}
    try:
        resp = httpx.request(method, url, params=params, content=content, headers=headers, timeout=timeout)
    except (httpx.TimeoutException, httpx.TransportError) as exc:
        raise TransientError(type(exc).__name__) from exc
    return HttpResponse(resp.status_code, {k.lower(): v for k, v in resp.headers.items()}, resp.content)


def session_signature(session_key: str) -> str:
    """checksession / resetusersessionkey 的 signature = hmac_sha256(session_key, "")。"""
    return hmac.new(session_key.encode("utf-8"), b"", hashlib.sha256).hexdigest()


def validate_scene(scene: str) -> str:
    if not isinstance(scene, str) or not SCENE_RE.match(scene):
        raise AppError(ErrorCode.BAD_REQUEST, "scene must be 1-32 visible chars of [0-9A-Za-z!#$&'()*+,/:;=?@-._~]")
    return scene


# 订阅消息关键词类型的本地预检（官方表，api_sendmessage）：不合规的数据不发出去，免得浪费次数
_KEY_RE = re.compile(r"^([a-z_]+?)(\d+)$")
_CN = re.compile(r"^[一-鿿]+$")
_RULES: dict[str, Callable[[str], bool]] = {
    "thing": lambda v: 0 < len(v) <= 20,
    "number": lambda v: len(v) <= 32 and re.fullmatch(r"[0-9]+(\.[0-9]+)?", v) is not None,
    "letter": lambda v: len(v) <= 32 and re.fullmatch(r"[A-Za-z]+", v) is not None,
    "symbol": lambda v: 0 < len(v) <= 5 and re.search(r"[0-9A-Za-z一-鿿\s]", v) is None,
    "character_string": lambda v: len(v) <= 32 and re.fullmatch(r"[0-9A-Za-z!-/:-@\[-`{-~]+", v) is not None,
    "phrase": lambda v: 0 < len(v) <= 5 and _CN.match(v) is not None,
    "phone_number": lambda v: len(v) <= 17 and re.fullmatch(r"[0-9+\-() ]+", v) is not None,
    "car_number": lambda v: 0 < len(v) <= 8,
    "amount": lambda v: re.fullmatch(r"[^\d\s]?\d{1,10}(\.\d{1,2})?元?", v) is not None,
    "name": lambda v: (_CN.match(v) is not None and len(v) <= 10) or (re.fullmatch(r"[A-Za-z\s.\-]+", v) is not None and len(v) <= 20),
}


def validate_subscribe_data(data: Mapping[str, Any]) -> dict[str, dict[str, str]]:
    """检查 {"thing1": {"value": "..."}}：键为类型名 + 序号；按官方关键词规则校验长度与字符；不允许控制字符。"""
    if not data:
        raise AppError(ErrorCode.BAD_REQUEST, "subscribe message data required")
    out: dict[str, dict[str, str]] = {}
    for key, item in data.items():
        value = item.get("value") if isinstance(item, Mapping) else item
        if not isinstance(value, str) or any(ord(ch) < 32 for ch in value):
            raise AppError(ErrorCode.BAD_REQUEST, f"{key}: value must be a single-line string", {"field": key})
        match = _KEY_RE.match(key)
        if match is None:
            raise AppError(ErrorCode.BAD_REQUEST, f"{key}: key must be <type><n>", {"field": key})
        rule = _RULES.get(match.group(1))
        if rule is not None and not rule(value):
            raise AppError(ErrorCode.BAD_REQUEST, f"{key}: value does not match {match.group(1)} rule", {"field": key})
        out[key] = {"value": value}
    return out


# ---------------------------------------------------------------- HTTP 实现

@dataclass
class HttpMiniProgramClient:
    config: WechatMpConfig = field(default_factory=get_config)
    transport: Transport = httpx_transport
    token_store: TokenStore | None = None
    breakers: BreakerRegistry = field(default_factory=lambda: breakers)
    sleep: Callable[[float], None] = time.sleep
    clock: Callable[[], float] = time.time
    lock: Any = None  # 可选分布式锁（luzback.scheduler.LockProvider）

    def __post_init__(self) -> None:
        self.tokens = AccessTokenCache(
            self._fetch_stable_token, key=f"wechat_mp:access_token:{self.config.appid}", store=self.token_store,
            margin_seconds=self.config.token_refresh_margin_s, force_min_interval=self.config.force_refresh_min_interval_s,
            clock=self.clock, lock=self.lock,
        )

    # ---- 底层调用

    def _once(self, api: str, method: str, path: str, params: dict | None, body: Any, access_token: str | None,
              idempotent: bool, binary: bool, timeout: float) -> Any:
        def do() -> Any:
            query = dict(params or {})
            if access_token is not None:
                query["access_token"] = access_token
            try:
                resp = self.transport(method, self.config.api_base + path, query, body, timeout)
            except (TransientError, AppError):
                raise
            except (TimeoutError, ConnectionError, OSError) as exc:
                raise TransientError(type(exc).__name__) from exc
            if resp.status >= 500 or resp.status == 429:
                raise TransientError(f"http {resp.status}")
            if binary and resp.content_type.startswith("image/"):
                return resp.content
            try:
                data = resp.json()
            except ValueError as exc:
                raise WechatError(-2, "non-json response") from exc
            errcode = int(data.get("errcode") or 0) if isinstance(data, dict) else 0
            if errcode == 0:
                return data
            if errcode == SYSTEM_BUSY:
                raise TransientError("wechat system busy")
            raise error_for(errcode, str(data.get("errmsg", "")))

        return resilient(f"wechat_mp.{api}", do, timeout_s=timeout, attempts=2 if idempotent else 1,
                         breaker=self.breakers.get(f"wechat_mp.{api}"), sleep=self.sleep)

    def _call(self, api: str, method: str, path: str, params: dict | None = None, body: Any = None,
              token: bool = True, idempotent: bool = False, binary: bool = False, timeout: float | None = None) -> Any:
        timeout = timeout or self.config.timeout_s
        if not token:
            return self._once(api, method, path, params, body, None, idempotent, binary, timeout)
        current = self.tokens.get()
        try:
            return self._once(api, method, path, params, body, current, idempotent, binary, timeout)
        except AccessTokenInvalid:
            fresh = self.tokens.invalidate(current)
            return self._once(api, method, path, params, body, fresh, idempotent, binary, timeout)

    def _fetch_stable_token(self, force: bool) -> tuple[str, int]:
        body = {"grant_type": "client_credential", "appid": self.config.appid, "secret": self.config.secret,
                "force_refresh": bool(force)}
        data = self._call("stable_token", "POST", "/cgi-bin/stable_token", body=body, token=False, idempotent=not force)
        return str(data["access_token"]), int(data.get("expires_in", 7200))

    # ---- 接口

    def access_token(self) -> str:
        return self.tokens.get()

    def code2session(self, js_code: str) -> Code2SessionResult:
        if not js_code:
            raise AppError(ErrorCode.BAD_REQUEST, "code required")
        data = self._call("code2session", "GET", "/sns/jscode2session", token=False, params={
            "appid": self.config.appid, "secret": self.config.secret, "js_code": js_code, "grant_type": "authorization_code"})
        if not data.get("openid"):
            raise WechatError(-3, "no openid")
        return Code2SessionResult(openid=data["openid"], session_key=data.get("session_key", ""), unionid=data.get("unionid"))

    def check_session_key(self, openid: str, session_key: str) -> bool:
        try:
            self._call("checksession", "GET", "/wxa/checksession", idempotent=True, params={
                "openid": openid, "signature": session_signature(session_key), "sig_method": "hmac_sha256"})
        except SessionKeyInvalid:
            return False
        return True

    def reset_session_key(self, openid: str, session_key: str) -> str:
        data = self._call("resetusersessionkey", "GET", "/wxa/resetusersessionkey", params={
            "openid": openid, "signature": session_signature(session_key), "sig_method": "hmac_sha256"})
        return str(data["session_key"])

    def get_phone_number(self, code: str, openid: str | None = None) -> PhoneInfo:
        if not code:
            raise AppError(ErrorCode.BAD_REQUEST, "phone code required")
        body: dict[str, Any] = {"code": code}
        if openid:
            body["openid"] = openid  # 校验 code 与 openid 的绑定关系
        data = self._call("getuserphonenumber", "POST", "/wxa/business/getuserphonenumber", body=body)
        info = data.get("phone_info") or {}
        mark = info.get("watermark") or {}
        return PhoneInfo(phone_number=str(info.get("phoneNumber", "")), pure_phone_number=str(info.get("purePhoneNumber", "")),
                         country_code=str(info.get("countryCode", "")), watermark_appid=mark.get("appid"),
                         watermark_timestamp=mark.get("timestamp"))

    def send_subscribe_message(self, touser: str, template_id: str, data: Mapping[str, Any], page: str | None = None,
                               miniprogram_state: str | None = None, lang: str | None = None) -> dict[str, Any]:
        body: dict[str, Any] = {
            "touser": touser, "template_id": template_id, "data": validate_subscribe_data(data),
            "miniprogram_state": miniprogram_state or self.config.miniprogram_state, "lang": lang or self.config.lang,
        }
        if page:
            body["page"] = page.lstrip("/")
        result = self._call("subscribe_send", "POST", "/cgi-bin/message/subscribe/send", body=body)
        return dict(result) if isinstance(result, Mapping) else {"errcode": 0, "errmsg": "ok"}

    def msg_sec_check(self, openid: str, content: str, scene: int, title: str | None = None,
                      nickname: str | None = None, signature: str | None = None) -> SecCheckResult:
        if scene not in MSG_SEC_SCENES:
            raise AppError(ErrorCode.BAD_REQUEST, "scene must be 1-4")
        if not content or len(content) > CONTENT_MAX:
            raise AppError(ErrorCode.BAD_REQUEST, f"content must be 1-{CONTENT_MAX} chars")
        body: dict[str, Any] = {"content": content, "version": 2, "scene": scene, "openid": openid}
        if title:
            body["title"] = title
        if nickname:
            body["nickname"] = nickname
        if signature and scene == 1:
            body["signature"] = signature
        data = self._call("msg_sec_check", "POST", "/wxa/msg_sec_check", body=body, idempotent=True)
        result = data.get("result") or {}
        return SecCheckResult(suggest=str(result.get("suggest", "review")), label=int(result.get("label", 0) or 0),
                              trace_id=data.get("trace_id"), detail=tuple(data.get("detail") or ()))

    def media_check_async(self, openid: str, media_url: str, media_type: int, scene: int) -> str:
        if media_type not in MEDIA_TYPES or scene not in MSG_SEC_SCENES:
            raise AppError(ErrorCode.BAD_REQUEST, "media_type must be 1|2 and scene 1-4")
        if not media_url.startswith(("https://", "http://")):
            raise AppError(ErrorCode.BAD_REQUEST, "media_url must be a downloadable url")
        data = self._call("media_check_async", "POST", "/wxa/media_check_async", body={
            "media_url": media_url, "media_type": media_type, "version": 2, "scene": scene, "openid": openid})
        return str(data["trace_id"])

    def get_unlimited_qrcode(self, scene: str, page: str | None = None, check_path: bool = True,
                             env_version: str | None = None, width: int = 430, auto_color: bool = False,
                             line_color: Mapping[str, int] | None = None, is_hyaline: bool = False) -> bytes:
        validate_scene(scene)
        if not 280 <= width <= 1280:
            raise AppError(ErrorCode.BAD_REQUEST, "width must be 280-1280")
        if page and (page.startswith("/") or "?" in page):
            raise AppError(ErrorCode.BAD_REQUEST, "page must not start with / or carry query; put params in scene")
        body: dict[str, Any] = {"scene": scene, "check_path": check_path, "env_version": env_version or self.config.env_version,
                                "width": width, "auto_color": auto_color, "is_hyaline": is_hyaline}
        if page:
            body["page"] = page
        if line_color:
            body["line_color"] = dict(line_color)
        result = self._call("getwxacodeunlimit", "POST", "/wxa/getwxacodeunlimit", body=body, idempotent=True, binary=True,
                            timeout=self.config.qrcode_timeout_s)
        if not isinstance(result, bytes):
            raise WechatError(-2, "qrcode response is not an image")
        return result


# ---------------------------------------------------------------- 进程级客户端

_client: MiniProgramClient | None = None


def set_client(client: MiniProgramClient | None) -> None:
    """注入客户端（测试用 FakeMiniProgram；项目可注入带共享 token 存储的实例）。"""
    global _client
    _client = client


def get_client() -> MiniProgramClient:
    global _client
    if _client is None:
        config = get_config()
        if config.is_dev_placeholder:
            raise AppError(ErrorCode.UPSTREAM_FAILED, "wechat mini program is not configured")
        _client = HttpMiniProgramClient(config)
    return _client


# ---------------------------------------------------------------- 模块级便捷函数（其他模块只调这些，不持有客户端）


def is_configured() -> bool:
    """已注入客户端，或配置了真实 AppID / AppSecret（开发占位值不算）。"""
    return _client is not None or not get_config().is_dev_placeholder


def access_token() -> str:
    """当前有效的接口调用凭证（stable_token，带缓存与提前刷新）；其他模块调用非本模块封装的微信接口时用
    （例如 09-wechat-pay 的发货信息录入 upload_shipping_info）。"""
    return get_client().access_token()


def invalidate_access_token(bad_token: str) -> str:
    """调用方收到 40001 / 40014 / 42001 时上报失效的 token，返回刷新后的 token（与本模块内部被动刷新同一缓存）。"""
    client = get_client()
    tokens = getattr(client, "tokens", None)
    if tokens is None or not hasattr(tokens, "invalidate"):
        return client.access_token()
    return tokens.invalidate(bad_token)


def send_subscribe_message(touser: str, template_id: str, data: Mapping[str, Any], page: str | None = None,
                           miniprogram_state: str | None = None, lang: str | None = None) -> dict[str, Any]:
    """发送订阅消息（20-notification 的 WechatMpSubscribeSender 调用）。返回 {"errcode": 0, "errmsg": "ok", "msgid"?}；
    平台非 0 errcode 抛 WechatError 子类（带 .errcode / .errmsg，如 43101 SubscriptionRefused）；传输错误原样抛出。
    miniprogram_state / lang 为空时用配置（WECHAT_MP_STATE / WECHAT_MP_LANG）。"""
    return get_client().send_subscribe_message(touser, template_id, data, page=page,
                                               miniprogram_state=miniprogram_state, lang=lang)


def production_problems(settings: Any = None) -> list[str]:
    """生产配置检查（luzback.register_production_check("wechat_mp", ...) 启动时执行）。"""
    from .fake import FakeMiniProgram  # noqa: PLC0415

    config = get_config()
    problems: list[str] = []
    if config.is_dev_placeholder:
        problems.append("WECHAT_APPID / WECHAT_SECRET must be the real mini program credentials")
    if isinstance(_client, FakeMiniProgram):
        problems.append("FakeMiniProgram is installed as the process client")
    if not config.api_base.startswith("https://"):
        problems.append("WECHAT_MP_API_BASE must be https")
    if config.push_token and config.push_require_aes and not config.push_aes_key:
        problems.append("WECHAT_MP_PUSH_AES_KEY is required when push is enabled (安全模式)")
    if config.miniprogram_state != "formal":
        problems.append("WECHAT_MP_STATE must be formal in prod")
    return problems
