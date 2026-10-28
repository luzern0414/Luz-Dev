"""Alembic 环境：把框架与每个模块的 code/ 加入 sys.path，导入所有模型后以 luzback.Base.metadata 生成迁移。

用法（在 backend/ 目录）：
    alembic revision --autogenerate -m "init"
    alembic upgrade head
连接串来自 LUZ_DATABASE_URL；生产禁止 create_all，只走迁移（SPEC §1）。
"""
from __future__ import annotations

import importlib
import os
import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import engine_from_config, pool

BACKEND = Path(__file__).resolve().parents[1]
for code_dir in sorted(BACKEND.glob("*/code")):
    sys.path.insert(0, str(code_dir))

from luzback.db import Base  # noqa: E402

# 导入每个模块的包以注册 ORM 模型（包名 = code/ 下的目录名）
for code_dir in sorted(BACKEND.glob("*/code")):
    for pkg in code_dir.iterdir():
        if pkg.is_dir() and (pkg / "__init__.py").exists() and pkg.name not in {"tests"}:
            try:
                importlib.import_module(pkg.name)
            except Exception as exc:  # noqa: BLE001
                print(f"[alembic] skip {pkg.name}: {exc}", file=sys.stderr)

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)
config.set_main_option("sqlalchemy.url", os.getenv("LUZ_DATABASE_URL", "sqlite:///./luz-dev.db"))
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(url=config.get_main_option("sqlalchemy.url"), target_metadata=target_metadata,
                      literal_binds=True, dialect_opts={"paramstyle": "named"}, compare_type=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(config.get_section(config.config_ini_section, {}), prefix="sqlalchemy.", poolclass=pool.NullPool)
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
