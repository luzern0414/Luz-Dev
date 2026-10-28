"""identity 的扩展点与外部依赖抽象（协议 + 默认实现 + 测试替身）。

- SmsSender：短信通道（腾讯云 / 阿里云等由项目实现）；FakeSmsSender 记录发出的验证码。
- AgreementGateway：协议同意记录。默认接 27-legal 的公开函数（pending / accept），不可用时视为无必需协议。
- 注销阻断检查：其他模块 register_deletion_checker(name, fn)，fn(db, user_id) → [Blocker]。
- LegacyCode2Session / LegacyPhoneResolver：兼容旧注入方式 set_code2session(fn) / set_phone_resolver(fn)。
"""
from __future__ import annotations

import threading
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from sqlalchemy.orm import Session

from luzback import AppError, ErrorCode, UpstreamError
from luzback.resilience import BreakerRegistry, TransientError, resilient

try:  # 27-legal 存在时使用其公开函数（导入同时注册其表，便于建表）
    import legal as _legal
except ImportError:  # pragma: no cover - 项目未启用 legal
    _legal = None

breakers = BreakerRegistry()


# ---------------------------------------------------------------- 短信

class SmsSender(Protocol):
    def send_code(self, phone: str, code: str, purpose: str, ttl_seconds: int) -> None: ...


class FakeSmsSender:
    def __init__(self):
        self.sent: list[tuple[str, str, str]] = []
        self.fail_next = 0
        self._lock = threading.Lock()

    def send_code(self, phone: str, code: str, purpose: str, ttl_seconds: int) -> None:
        with self._lock:
            if self.fail_next > 0:
                self.fail_next -= 1
                raise ConnectionError("sms gateway down")
            self.sent.append((phone, code, purpose))

    def last_code(self, phone: str) -> str | None:
        for item in reversed(self.sent):
            if item[0] == phone:
                return item[1]
        return None


class LegacySmsSender:
    def __init__(self, fn: Callable[[str, str], None]):
        self.fn = fn

    def send_code(self, phone: str, code: str, purpose: str, ttl_seconds: int) -> None:
        self.fn(phone, code)


def send_via(sender: SmsSender, phone: str, code: str, purpose: str, ttl_seconds: int, timeout_s: float = 3.0) -> None:
    """经熔断 + 超时发送；短信非幂等，不重试（SPEC §9：由任务重试，这里直接报 5020）。"""
    def call() -> None:
        try:
            sender.send_code(phone, code, purpose, ttl_seconds)
        except AppError:
            raise
        except Exception as exc:  # noqa: BLE001 通道错误 → 计入熔断
            raise TransientError(type(exc).__name__) from exc

    resilient("identity.sms", call, timeout_s=timeout_s, attempts=1, breaker=breakers.get("identity.sms"))


# ---------------------------------------------------------------- 协议同意

@dataclass(frozen=True)
class AgreementAccept:
    document_id: int
    version: str
    sha256: str


class AgreementGateway(Protocol):
    def accept(self, db: Session, user_id: int, items: Iterable[AgreementAccept], channel: str, now: datetime) -> list[int]: ...
    def pending(self, db: Session, user_id: int, now: datetime) -> list[dict[str, Any]]: ...


class NoAgreements:
    def accept(self, db, user_id, items, channel, now) -> list[int]:
        return []

    def pending(self, db, user_id, now) -> list[dict[str, Any]]:
        return []


class LegalAgreements:
    """接 27-legal：accept(db, user_id, document_id, version, sha256, channel, now)；pending(db, user_id, now)。"""

    def accept(self, db: Session, user_id: int, items: Iterable[AgreementAccept], channel: str, now: datetime) -> list[int]:
        ids = []
        for item in items:
            consent = _legal.accept(db, user_id, item.document_id, item.version, item.sha256, channel, now)
            ids.append(int(getattr(consent, "document_id", item.document_id)))
        return ids

    def pending(self, db: Session, user_id: int, now: datetime) -> list[dict[str, Any]]:
        return [{"document_id": d.id, "kind": d.kind, "version": d.version, "title": getattr(d, "title", "")}
                for d in _legal.pending(db, user_id, now)]


class FakeAgreements:
    """测试替身：required = {document_id: version}。"""

    def __init__(self, required: dict[int, str] | None = None):
        self.required = dict(required or {})
        self.accepted: dict[int, set[int]] = {}

    def accept(self, db, user_id, items, channel, now) -> list[int]:
        ids = []
        for item in items:
            if self.required.get(item.document_id) != item.version:
                raise AppError(ErrorCode.CONFLICT, "document version changed, reload and confirm again")
            self.accepted.setdefault(user_id, set()).add(item.document_id)
            ids.append(item.document_id)
        return ids

    def pending(self, db, user_id, now) -> list[dict[str, Any]]:
        done = self.accepted.get(user_id, set())
        return [{"document_id": d, "version": v} for d, v in self.required.items() if d not in done]


def default_agreements() -> AgreementGateway:
    if _legal is not None and hasattr(_legal, "accept") and hasattr(_legal, "pending"):
        return LegalAgreements()
    return NoAgreements()  # pragma: no cover


# ---------------------------------------------------------------- 注销阻断检查

@dataclass(frozen=True)
class Blocker:
    code: str       # unfinished_orders | balance | active_card …
    message: str    # 给用户看的说明，例如“还有 2 笔未完成订单”


DeletionChecker = Callable[[Session, int], list[Blocker]]
_checkers: dict[str, DeletionChecker] = {}


def register_deletion_checker(name: str, fn: DeletionChecker) -> None:
    """其他模块注册注销前置检查（未完成订单、储值余额、有效会员卡等）；同名覆盖。"""
    _checkers[name] = fn


def unregister_deletion_checker(name: str) -> None:
    _checkers.pop(name, None)


def run_deletion_checks(db: Session, user_id: int) -> list[Blocker]:
    blockers: list[Blocker] = []
    for name in sorted(_checkers):
        blockers.extend(_checkers[name](db, user_id) or [])
    return blockers


# ---------------------------------------------------------------- 兼容旧注入

class LegacyCode2Session:
    """把旧的 code2session(fn) 包成 MiniProgramClient 的 code2session（其余能力不可用）。"""

    def __init__(self, fn: Callable[[str], dict], phone_resolver: Callable[[str], str] | None = None):
        self.fn = fn
        self.phone_resolver = phone_resolver

    def code2session(self, js_code: str):
        from wechat_mp import Code2SessionResult

        def call() -> dict:
            try:
                return self.fn(js_code)
            except AppError:
                raise
            except Exception as exc:  # noqa: BLE001
                raise TransientError(type(exc).__name__) from exc

        info = resilient("identity.code2session", call, timeout_s=3.0, attempts=1, breaker=breakers.get("identity.code2session"))
        if not isinstance(info, dict) or not info.get("openid"):
            raise AppError(ErrorCode.UNAUTHORIZED, "login code invalid or expired, please retry")
        return Code2SessionResult(openid=info["openid"], session_key=info.get("session_key", "") or "",
                                  unionid=info.get("unionid"))

    def get_phone_number(self, code: str, openid: str | None = None):
        from wechat_mp import PhoneInfo

        if self.phone_resolver is None:
            raise UpstreamError("wechat phone client not configured")
        phone = self.phone_resolver(code)
        return PhoneInfo(phone_number=phone, pure_phone_number=phone, country_code="86")

    def msg_sec_check(self, *args, **kwargs):  # 旧注入不支持内容安全
        raise UpstreamError("content security client not configured")

    def media_check_async(self, *args, **kwargs):
        raise UpstreamError("content security client not configured")
