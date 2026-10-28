"""部署脚本：同步权限码与内置角色，并创建首个超级管理员（一次性临时口令只打印一次）。

    cd backend && python -m admin_rbac.bootstrap <username> [姓名]

首次登录必须修改口令并绑定 MFA（超级管理员角色要求 MFA）。已存在在职超级管理员时拒绝执行。
"""
from __future__ import annotations

import sys

from luzback import get_settings, init_engine
from luzback.db import get_session_factory

from .service import bootstrap_super_admin


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 2
    init_engine(get_settings().database_url)
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
