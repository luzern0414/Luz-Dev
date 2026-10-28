"""部署脚本：同步权限码与内置角色，并创建首个超级管理员（一次性临时口令只打印一次）。

    cd backend && python -m admin_rbac.bootstrap --seed            # 每次发布：迁移后同步权限码与内置角色（幂等）
    cd backend && python -m admin_rbac.bootstrap <username> [姓名]  # 首次部署：创建首个超级管理员

--seed 会先导入全部已启用模块（它们在导入时登记各自的权限码），再写入 admin_permissions。

首次登录必须修改口令并绑定 MFA（超级管理员角色要求 MFA）。已存在在职超级管理员时拒绝执行。
"""
from __future__ import annotations

import sys

from luzback import get_settings, init_engine
from luzback.db import get_session_factory

from .service import bootstrap_super_admin, ensure_seed


def _import_modules() -> None:
    """导入全部已启用模块包，让它们的 declare_permissions 生效（与 app.py 的装配范围一致）。"""
    import importlib
    from pathlib import Path

    backend = Path(__file__).resolve().parents[3]
    settings = get_settings()
    for code_dir in sorted(backend.glob("*/code")):
        for pkg in sorted(p for p in code_dir.iterdir() if p.is_dir() and (p / "__init__.py").exists()):
            if pkg.name in {"tests", "luzback"} or not settings.feature_enabled(pkg.name):
                continue
            importlib.import_module(pkg.name)
            try:
                importlib.import_module(f"{pkg.name}.api")
            except ModuleNotFoundError as exc:
                if exc.name not in (f"{pkg.name}.api", pkg.name):
                    raise


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 2
    init_engine(get_settings().database_url)
    if argv[0] == "--seed":
        _import_modules()
        db = get_session_factory()()
        try:
            result = ensure_seed(db)
        finally:
            db.close()
        print(f"seeded: permissions +{result['permissions']}, roles +{result['roles']}")
        return 0
    _import_modules()
    db = get_session_factory()()
    try:
        result = bootstrap_super_admin(db, argv[0], argv[1] if len(argv) > 1 else "")
    finally:
        db.close()
    print(f"super admin created: id={result['id']} username={result['username']}")
    print(f"temporary password (shown once, expires in 72h): {result['temporary_password']}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main(sys.argv[1:]))
