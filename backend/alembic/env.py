"""Alembic 环境：把框架与每个模块的 code/ 加入 sys.path，导入所有模块的模型后以 luzback.Base.metadata 生成 / 执行迁移。

用法（在 backend/ 目录）：
    alembic upgrade head                                  # 部署：只走迁移，生产禁止 create_all（SPEC §1）
    alembic revision --autogenerate -m "add xxx"          # 改模型后生成迁移，人工审阅后提交
    alembic check                                         # CI：模型与迁移无漂移
连接串来自 LUZ_DATABASE_URL（与应用一致）。

模型导入：每个模块包 `<pkg>` 与 `<pkg>.models`（存在时）都导入；任何一个导入失败直接报错，
避免“模块没导入 → autogenerate 以为表该删掉”。LUZ_FEATURES 不影响迁移：所有表都建，未启用的模块只是不暴露接口。
"""
from __future__ import annotations

import importlib
import importlib.util
import os
import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import engine_from_config, pool

BACKEND = Path(__file__).resolve().parents[1]
for code_dir in sorted(BACKEND.glob("*/code"), reverse=True):
    if str(code_dir) not in sys.path:
        sys.path.insert(0, str(code_dir))

from luzback.db import Base  # noqa: E402
import luzback.audit, luzback.idempotency, luzback.outbox  # noqa: E401,E402,F401  框架表


def model_packages() -> list[str]:
    names: list[str] = []
    for code_dir in sorted(BACKEND.glob("*/code")):
        for pkg in sorted(code_dir.iterdir()):
            if pkg.is_dir() and (pkg / "__init__.py").exists() and pkg.name not in {"tests", "luzback"}:
                names.append(pkg.name)
    return names


def import_models() -> None:
    for name in model_packages():
        importlib.import_module(name)
        if importlib.util.find_spec(f"{name}.models") is not None:
            importlib.import_module(f"{name}.models")


import_models()

config = context.config
if config.config_file_name is not None and config.attributes.get("configure_logger", True):
    fileConfig(config.config_file_name, disable_existing_loggers=False)
url = config.attributes.get("url") or os.getenv("LUZ_DATABASE_URL", "sqlite:///./luz-dev.db")
config.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
target_metadata = Base.metadata


def _options(dialect_name: str) -> dict:
    # SQLite 不支持大多数 ALTER：用 batch 模式（重建表）；compare_type 让列类型变化也进入 autogenerate / check
    return {"target_metadata": target_metadata, "compare_type": True, "render_as_batch": dialect_name == "sqlite"}


def run_migrations_offline() -> None:
    url_ = config.get_main_option("sqlalchemy.url")
    context.configure(url=url_, literal_binds=True, dialect_opts={"paramstyle": "named"},
                      **_options(url_.split(":", 1)[0].split("+", 1)[0]))
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connection = config.attributes.get("connection")
    if connection is not None:  # 测试 / 程序内调用传入现成连接
        context.configure(connection=connection, **_options(connection.dialect.name))
        with context.begin_transaction():
            context.run_migrations()
        return
    connectable = engine_from_config(config.get_section(config.config_ini_section, {}), prefix="sqlalchemy.", poolclass=pool.NullPool)
    with connectable.connect() as conn:
        context.configure(connection=conn, **_options(conn.dialect.name))
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
