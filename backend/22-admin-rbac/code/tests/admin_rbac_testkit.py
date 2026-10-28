"""admin_rbac 测试工具：可推进时钟、内存库 + 种子、快速建员工、登录取令牌、算 TOTP。"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select

from admin_rbac import auth, config, service
from admin_rbac.access import load_staff
from admin_rbac.models import AdminRole, AdminStoreScope, AdminUser, AdminUserRole
from admin_rbac.passwords import hash_secret
from admin_rbac.totp import decode_secret, totp_at
from luzback.testing import FakeClock, memory_session

START = datetime(2026, 10, 28, 2, 0, 0, tzinfo=timezone.utc).timestamp()
PASSWORD = "Correct-Horse-Battery-9"
UA = "testclient"


class Clock:
    """FakeClock 的 datetime 适配：config.set_clock(clock)。"""

    def __init__(self, start: float = START):
        self.fake = FakeClock(start)

    def __call__(self) -> datetime:
        return datetime.fromtimestamp(self.fake(), timezone.utc)

    def advance(self, seconds: float) -> None:
        self.fake.advance(seconds)

    @property
    def ts(self) -> float:
        return self.fake()


def reset_hooks() -> Clock:
    config.reset_config()
    config.set_breach_checker(None)
    config.set_captcha_verifier(None)
    clock = Clock()
    config.set_clock(clock)
    return clock


def new_db():
    db = memory_session("admin_rbac.models", "luzback.audit", "luzback.outbox", "luzback.idempotency")
    service.ensure_seed(db)
    return db


def role_id(db, builtin_code: str) -> int:
    return db.execute(select(AdminRole.id).where(AdminRole.builtin_code == builtin_code)).scalar_one()


def make_user(db, username: str, roles: tuple = ("super_admin",), all_stores: bool = True, store_ids: tuple = (),
              password: str = PASSWORD, must_change: bool = False, name: str = "") -> AdminUser:
    """直接建员工（绕过防越权，供准备数据）。roles 元素为内置 code 或角色 id。"""
    user = AdminUser(username=username, password_hash=hash_secret(password), name=name, all_stores=all_stores,
                     must_change_password=must_change, created_by="test")
    db.add(user)
    db.flush()
    for r in roles:
        rid = r if isinstance(r, int) else role_id(db, r)
        db.add(AdminUserRole(user_id=user.id, role_id=rid))
    for sid in store_ids:
        db.add(AdminStoreScope(user_id=user.id, store_id=sid))
    db.commit()
    return user


def login(db, username: str, password: str = PASSWORD, ua: str = UA) -> dict:
    return auth.login(db, username, password, ip="10.0.0.1", user_agent=ua)


def token_of(db, username: str, password: str = PASSWORD) -> str:
    return login(db, username, password)["token"]


def staff_of(db, token: str, ua: str = UA):
    return auth.authenticate(db, token, ip="10.0.0.1", user_agent=ua)


def actor(db, user_id: int):
    """无会话的员工快照（auth_at=now，视为新鲜）。"""
    return load_staff(db, user_id, auth_at=config.now())


def code_at(secret: str, ts: float) -> str:
    return totp_at(decode_secret(secret), ts)


def enable_mfa(db, token: str, clock: Clock) -> tuple[str, list[str], str]:
    """为当前会话员工绑定 MFA；返回 (secret, recovery_codes, new_token)。"""
    staff = staff_of(db, token)
    enrolled = auth.start_mfa_enrollment(db, staff)
    result = auth.confirm_mfa_enrollment(db, staff, code_at(enrolled["secret"], clock.ts), ip="10.0.0.1", user_agent=UA)
    return enrolled["secret"], result["recovery_codes"], result["token"]
