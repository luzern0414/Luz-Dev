"""并发（文件 SQLite + 线程）：失败计数不丢、只锁一次；恢复码只能用一次；同一 TOTP 只能成功一次；
两名超级管理员互相停用时至少保留一名。"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from admin_rbac import auth, config, service
from admin_rbac.access import load_staff
from admin_rbac.models import AdminLoginLog, AdminLoginThrottle, AdminUser
from luzback import AppError, Base, ErrorCode

from admin_rbac_testkit import PASSWORD, code_at, make_user, reset_hooks


@pytest.fixture()
def factory(tmp_path):
    reset_hooks()
    config.set_clock(None)  # 并发用例用真实时钟
    engine = create_engine(f"sqlite:///{tmp_path / 'rbac.db'}", future=True,
                           connect_args={"check_same_thread": False, "timeout": 30})
    Base.metadata.create_all(engine)
    make = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    with make() as s:
        service.ensure_seed(s)
    yield make
    engine.dispose()
    reset_hooks()


def run(make, fn, n):
    def call(i):
        with make() as s:
            try:
                return fn(s, i)
            except AppError as exc:
                s.rollback()
                return exc.code

    with ThreadPoolExecutor(max_workers=n) as pool:
        return list(pool.map(call, range(n)))


def test_concurrent_failures_counted_and_locked_once(factory):
    config.set_config(login_max_failures=5)
    with factory() as s:
        make_user(s, "boss")
    results = run(factory, lambda s, i: auth.login(s, "boss", "wrong-password-x"), 20)
    assert all(r in (ErrorCode.UNAUTHORIZED, ErrorCode.RATE_LIMITED) for r in results)
    assert ErrorCode.RATE_LIMITED in results
    with factory() as s:
        row = s.get(AdminLoginThrottle, "boss")
        assert row.lock_count == 1  # 只触发一次锁定
        assert s.execute(select(func.count()).select_from(AdminLoginLog)).scalar_one() == 20  # 每次尝试都有日志
        with pytest.raises(AppError) as exc:
            auth.login(s, "boss", PASSWORD)
        assert exc.value.code == ErrorCode.RATE_LIMITED


def test_recovery_code_used_once_under_race(factory):
    with factory() as s:
        user = make_user(s, "boss")
        uid = user.id
        codes = auth._store_recovery_codes(s, uid, config.now())
        s.commit()
    results = run(factory, lambda s, i: (auth.consume_recovery_code(s, uid, codes[0], config.now()), s.commit())[0], 8)
    assert results.count(True) == 1


def test_same_totp_accepted_once_under_race(factory):
    from admin_rbac.totp import new_secret

    secret = new_secret()
    with factory() as s:
        user = make_user(s, "boss")
        user.mfa_enabled = True
        user.mfa_secret_enc = auth.cipher().encrypt(secret)
        s.commit()
        uid = user.id
    code = code_at(secret, config.now().timestamp())

    def attempt(s, i):
        u = s.get(AdminUser, uid)
        ok, _, _ = auth._check_second_factor(s, u, code, None, config.now())
        s.commit()
        return ok

    results = run(factory, attempt, 8)
    assert results.count(True) == 1


def test_mutual_disable_keeps_one_super_admin(factory):
    with factory() as s:
        a = make_user(s, "boss.a")
        b = make_user(s, "boss.b")
        ids = (a.id, b.id)

    def disable_other(s, i):
        me, other = ids if i % 2 == 0 else ids[::-1]
        staff = load_staff(s, me, auth_at=config.now())
        service.disable_staff(s, staff, other)
        s.commit()
        return "ok"

    results = run(factory, disable_other, 2)
    with factory() as s:
        active = s.execute(select(func.count()).select_from(AdminUser).where(AdminUser.status == "active")).scalar_one()
    assert active >= 1
    assert results.count("ok") <= 1
