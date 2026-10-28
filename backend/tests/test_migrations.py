"""迁移：空库 upgrade head → 与模型无漂移（alembic check）→ downgrade base → 再 upgrade；之后权限种子可执行。

子进程运行（alembic env.py 会导入全部模块），用临时 SQLite 文件库。
"""
from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]


def _run(args: list[str], db: Path) -> subprocess.CompletedProcess:
    env = {**os.environ, "LUZ_ENV": "dev", "LUZ_DATABASE_URL": f"sqlite:///{db.as_posix()}", "LUZ_FEATURES": "",
           "PYTHONUTF8": "1", "PYTHONPATH": os.pathsep.join(
               [str(BACKEND / "00-framework" / "code"), *map(str, sorted(BACKEND.glob("*/code")))])}
    return subprocess.run([sys.executable, "-X", "utf8", *args], cwd=BACKEND, env=env, capture_output=True, text=True,
                          encoding="utf-8", timeout=300)


def _ok(proc: subprocess.CompletedProcess) -> str:
    assert proc.returncode == 0, (proc.stdout[-2000:], proc.stderr[-4000:])
    return proc.stdout


def _tables(db: Path) -> set[str]:
    with sqlite3.connect(db) as conn:
        return {r[0] for r in conn.execute("select name from sqlite_master where type='table'")}


def test_upgrade_check_downgrade_upgrade_and_seed(tmp_path):
    db = tmp_path / "migrate.db"
    _ok(_run(["-m", "alembic", "upgrade", "head"], db))
    tables = _tables(db)
    for expected in ("users", "orders", "payment_records", "after_sales", "wallet_accounts", "user_coupons",
                     "lottery_draws", "admin_users", "member_card_cards", "points_ledger", "outbox_events", "audit_logs"):
        assert any(t == expected for t in tables), (expected, sorted(tables))
    check = _run(["-m", "alembic", "check"], db)
    assert check.returncode == 0, ("model drift — generate a migration", check.stdout[-2000:], check.stderr[-3000:])
    _ok(_run(["-m", "alembic", "downgrade", "base"], db))
    assert _tables(db) <= {"alembic_version"}
    _ok(_run(["-m", "alembic", "upgrade", "head"], db))
    out = _ok(_run(["-m", "admin_rbac.bootstrap", "--seed"], db))
    assert "seeded" in out
    with sqlite3.connect(db) as conn:
        codes = {r[0] for r in conn.execute("select code from admin_permissions")}
        grants = {r[0] for r in conn.execute("select code from admin_role_permissions")}
    for code in ("member:reveal", "wallet:adjust", "payment:refund", "after_sale:approve", "coupon:issue", "lottery:fulfil",
                 "notification:resend", "member_card:refund", "order:read"):
        assert code in codes, code
    assert {"wallet:*", "payment:*", "after_sale:*", "order:*"} <= grants
