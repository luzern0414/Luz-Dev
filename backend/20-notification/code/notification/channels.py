"""渠道抽象：订阅消息发送器、短信发送器、收件人解析器；内存假实现；wechat_mp 适配器。

- 业务规则只依赖这里的 Protocol；真实平台通过适配器接入，测试用 Fake*。
- wechat_mp（28 模块，共享的小程序服务端客户端）可导入时，默认订阅消息发送器是 WechatMpSubscribeSender；
  导入失败或缺少函数时：生产环境返回 channel_not_configured（永久失败，不重试），开发环境用 FakeSubscribeSender。
- 短信没有内置云厂商适配器：项目用 set_sms_sender() 注入（腾讯云 SendSms / 阿里云 SendSms 的薄封装）。
"""
from __future__ import annotations

import logging
import os
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from sqlalchemy.orm import Session

log = logging.getLogger("luz.notification")


@dataclass
class SendResult:
    """一次外部发送的结果。errcode 统一为字符串（微信为数字串，短信为厂商码）。"""

    ok: bool
    errcode: str = "0"
    errmsg: str = ""
    msg_id: str = ""
    retryable: bool | None = None  # None = 按错误码表判断


class SubscribeMessageSender(Protocol):
    def send(self, *, touser: str, template_id: str, data: dict[str, dict[str, str]], page: str | None,
             miniprogram_state: str, lang: str) -> SendResult: ...


class SmsSender(Protocol):
    def send(self, *, phone: str, template_code: str, params: dict[str, str]) -> SendResult: ...


@dataclass
class Recipient:
    openid: str | None = None
    phone: str | None = None   # 明文只在发送瞬间使用，不落库
    label: str = ""            # 后台检索用的非敏感标识（如会员号）


class RecipientResolver(Protocol):
    def member(self, db: Session, member_id: int) -> Recipient | None: ...
    def staff(self, db: Session, staff_id: int) -> Recipient | None: ...
    def member_id_by_openid(self, db: Session, openid: str) -> int | None: ...


# ---------------------------------------------------------------- 假实现（测试 / 开发）


class FakeSubscribeSender:
    """内存假服务：按脚本返回结果（errcode 整数 / SendResult / 异常 / 可调用），并记录每次请求。"""

    def __init__(self, script: list[Any] | None = None, default: Any = 0):
        self.script = list(script or [])
        self.default = default
        self.calls: list[dict[str, Any]] = []
        self._lock = threading.Lock()
        self._seq = 0

    def send(self, *, touser, template_id, data, page, miniprogram_state, lang) -> SendResult:
        with self._lock:
            self.calls.append({"touser": touser, "template_id": template_id, "data": data, "page": page,
                               "miniprogram_state": miniprogram_state, "lang": lang})
            step = self.script.pop(0) if self.script else self.default
            self._seq += 1
            seq = self._seq
        if callable(step) and not isinstance(step, SendResult):
            step = step(self.calls[-1])
        if isinstance(step, BaseException):
            raise step
        if isinstance(step, SendResult):
            return step
        code = int(step)
        return SendResult(ok=code == 0, errcode=str(code), errmsg="ok" if code == 0 else f"errcode {code}",
                          msg_id=f"msg-{seq}" if code == 0 else "")


class FakeSmsSender:
    """内存假短信：脚本元素为厂商码字符串（"" 表示成功）/ SendResult / 异常。"""

    def __init__(self, script: list[Any] | None = None, default: Any = ""):
        self.script = list(script or [])
        self.default = default
        self.calls: list[dict[str, Any]] = []
        self._lock = threading.Lock()

    def send(self, *, phone, template_code, params) -> SendResult:
        with self._lock:
            self.calls.append({"phone": phone, "template_code": template_code, "params": params})
            step = self.script.pop(0) if self.script else self.default
            n = len(self.calls)
        if isinstance(step, BaseException):
            raise step
        if isinstance(step, SendResult):
            return step
        code = str(step)
        return SendResult(ok=code == "", errcode=code or "0", errmsg=code, msg_id=f"sms-{n}" if code == "" else "")


@dataclass
class InMemoryRecipients:
    """测试用收件人表：members / staff 为 id → Recipient。"""

    members: dict[int, Recipient] = field(default_factory=dict)
    staff_map: dict[int, Recipient] = field(default_factory=dict)

    def member(self, db, member_id):
        return self.members.get(member_id)

    def staff(self, db, staff_id):
        return self.staff_map.get(staff_id)

    def member_id_by_openid(self, db, openid):
        for member_id, rec in self.members.items():
            if rec.openid == openid:
                return member_id
        return None


class NotConfiguredSender:
    """生产环境未接入渠道时使用：永久失败，不重试，后台可见。"""

    def send(self, **_kwargs) -> SendResult:
        return SendResult(ok=False, errcode="channel_not_configured", errmsg="channel not configured", retryable=False)


# ---------------------------------------------------------------- wechat_mp 适配器

def _wechat_mp():
    try:
        import wechat_mp  # noqa: PLC0415  (28-wechat-mp，可能未部署)
    except Exception:  # noqa: BLE001
        return None
    return wechat_mp


def wechat_mp_available() -> bool:
    mod = _wechat_mp()
    return mod is not None and callable(getattr(mod, "send_subscribe_message", None))


class WechatMpSubscribeSender:
    """薄适配器：调用 wechat_mp.send_subscribe_message(...)。

    期望签名：send_subscribe_message(touser, template_id, data, page=None, miniprogram_state="formal", lang="zh_CN")
    → dict（含 errcode / errmsg / msgid）；非 0 errcode 以带 .errcode 属性的异常（wechat_mp.errors.WechatError）抛出亦可。
    access_token 获取与 40001/42001 被动刷新由 wechat_mp 负责。传输错误（超时、连接）原样抛出，由投递流程按可重试处理。
    """

    def __init__(self, send_fn: Callable[..., Any] | None = None):
        if send_fn is None:
            mod = _wechat_mp()
            send_fn = getattr(mod, "send_subscribe_message", None) if mod else None
        if send_fn is None:
            raise RuntimeError("wechat_mp.send_subscribe_message is not available")
        self.send_fn = send_fn

    def send(self, *, touser, template_id, data, page, miniprogram_state, lang) -> SendResult:
        kwargs: dict[str, Any] = {"touser": touser, "template_id": template_id, "data": data,
                                  "miniprogram_state": miniprogram_state, "lang": lang}
        if page:
            kwargs["page"] = page
        try:
            resp = self.send_fn(**kwargs)
        except Exception as exc:  # noqa: BLE001
            errcode = getattr(exc, "errcode", None)
            if errcode is None:
                raise
            return SendResult(ok=False, errcode=str(errcode), errmsg=str(getattr(exc, "errmsg", ""))[:200])
        resp = resp or {}
        if not isinstance(resp, dict):
            resp = {"errcode": getattr(resp, "errcode", 0), "msgid": getattr(resp, "msgid", "")}
        code = int(resp.get("errcode") or 0)
        return SendResult(ok=code == 0, errcode=str(code), errmsg=str(resp.get("errmsg") or "")[:200],
                          msg_id=str(resp.get("msgid") or resp.get("msg_id") or ""))


# ---------------------------------------------------------------- 默认收件人解析（identity / admin_rbac）


class DefaultRecipients:
    """从 identity.User（openid、phone_enc）与 admin_rbac.AdminUser（phone_enc）读取收件人；模块缺失时返回 None。

    跨模块只读 openid / 手机号这一处；项目可用 set_recipient_resolver() 替换为对方模块导出的函数。
    """

    @staticmethod
    def _decrypt(token: str | None) -> str | None:
        if not token:
            return None
        try:
            from luzback import get_settings  # noqa: PLC0415
            from luzback.crypto import FieldCipher  # noqa: PLC0415
            return FieldCipher(get_settings().field_encryption_key).decrypt(token)
        except Exception:  # noqa: BLE001
            return None

    def member(self, db, member_id):
        try:
            from identity import User  # noqa: PLC0415
        except Exception:  # noqa: BLE001
            return None
        user = db.get(User, member_id)
        if user is None or getattr(user, "deleted_at", None) is not None:
            return None
        return Recipient(openid=user.openid, phone=self._decrypt(getattr(user, "phone_enc", None)), label=str(member_id))

    def staff(self, db, staff_id):
        try:
            from admin_rbac.models import AdminUser  # noqa: PLC0415
        except Exception:  # noqa: BLE001
            return None
        user = db.get(AdminUser, staff_id)
        if user is None or getattr(user, "deleted_at", None) is not None:
            return None
        return Recipient(phone=self._decrypt(getattr(user, "phone_enc", None)), label=f"staff:{staff_id}")

    def member_id_by_openid(self, db, openid):
        try:
            from identity import User  # noqa: PLC0415
            from sqlalchemy import select  # noqa: PLC0415
        except Exception:  # noqa: BLE001
            return None
        return db.execute(select(User.id).where(User.openid == openid)).scalar_one_or_none()


# ---------------------------------------------------------------- 注入点

_lock = threading.Lock()
_subscribe_sender: SubscribeMessageSender | None = None
_sms_sender: SmsSender | None = None
_recipients: RecipientResolver | None = None
_store_tz: Callable[[int], str | None] | None = None


def _is_prod() -> bool:
    return os.getenv("LUZ_ENV", "dev") == "prod"


def set_subscribe_sender(sender: SubscribeMessageSender | None) -> None:
    global _subscribe_sender
    with _lock:
        _subscribe_sender = sender


def get_subscribe_sender() -> SubscribeMessageSender:
    global _subscribe_sender
    with _lock:
        if _subscribe_sender is None:
            if wechat_mp_available():
                _subscribe_sender = WechatMpSubscribeSender()
            elif _is_prod():
                _subscribe_sender = NotConfiguredSender()
            else:
                log.warning("notification: wechat_mp not available, using FakeSubscribeSender (dev)")
                _subscribe_sender = FakeSubscribeSender()
        return _subscribe_sender


def set_sms_sender(sender: SmsSender | None) -> None:
    global _sms_sender
    with _lock:
        _sms_sender = sender


def get_sms_sender() -> SmsSender:
    global _sms_sender
    with _lock:
        if _sms_sender is None:
            _sms_sender = NotConfiguredSender() if _is_prod() else FakeSmsSender()
        return _sms_sender


def set_recipient_resolver(resolver: RecipientResolver | None) -> None:
    global _recipients
    with _lock:
        _recipients = resolver


def get_recipient_resolver() -> RecipientResolver:
    global _recipients
    with _lock:
        if _recipients is None:
            _recipients = DefaultRecipients()
        return _recipients


def set_store_timezone_resolver(fn: Callable[[int], str | None] | None) -> None:
    """门店时区钩子：store_id → IANA 时区名；未设置或返回 None 时用 NOTIFY_TIMEZONE。"""
    global _store_tz
    _store_tz = fn


def store_timezone(store_id: int | None, default: str) -> str:
    if store_id is not None and _store_tz is not None:
        try:
            return _store_tz(store_id) or default
        except Exception:  # noqa: BLE001
            return default
    return default
