"""admin_rbac：后台账号、认证（口令 + TOTP）、服务端会话、RBAC 权限码与门店范围、审计查询。

其他模块只从这里（或 admin_rbac.api 的依赖）导入：

    from admin_rbac import (current_staff, require_permission, require_fresh_auth, load_staff, declare_permissions,
                            Staff, store_scope, assert_in_scope, apply_store_scope, assert_not_self, log_sensitive_read)

后台路由：`staff: Staff = Depends(require_permission("wallet:adjust", fresh=True))`；门店范围 `store_scope(staff)`
（None = 全部门店）。测试用 `admin_rbac.testing.staff_headers(db, *codes, store_ids=...)` 走真实会话。
"""
from . import seed as _seed  # noqa: F401  # 登记标准权限码目录
from .access import (
    Staff, apply_store_scope, assert_fresh, assert_in_scope, assert_not_self, assert_permission, load_staff,
    log_sensitive_read, store_scope,
)
from .config import (
    AdminAuthConfig, get_config, set_breach_checker, set_captcha_verifier, set_clock, set_config,
)
from .models import (
    AdminLoginLog, AdminLoginThrottle, AdminPermission, AdminRecoveryCode, AdminRole, AdminRolePermission, AdminSession,
    AdminStoreScope, AdminUser, AdminUserRole,
)
from .permissions import declare_permission, declare_permissions, grants, is_module_wildcard, registered, sync_permissions
from .seed import SUPER_ADMIN
from .auth import authenticate, decode_access
from .service import bootstrap_super_admin, ensure_seed, grants_of, load_active_staff
from .api import authenticated_staff, current_staff, require_fresh, require_fresh_auth, require_permission  # noqa: E402

__all__ = [
    "Staff", "apply_store_scope", "assert_fresh", "assert_in_scope", "assert_not_self", "assert_permission", "load_staff",
    "log_sensitive_read", "store_scope",
    "AdminAuthConfig", "get_config", "set_breach_checker", "set_captcha_verifier", "set_clock", "set_config",
    "AdminLoginLog", "AdminLoginThrottle", "AdminPermission", "AdminRecoveryCode", "AdminRole", "AdminRolePermission",
    "AdminSession", "AdminStoreScope", "AdminUser", "AdminUserRole",
    "declare_permission", "declare_permissions", "grants", "is_module_wildcard", "registered", "sync_permissions",
    "SUPER_ADMIN", "bootstrap_super_admin", "ensure_seed", "grants_of", "load_active_staff", "authenticate", "decode_access",
    "authenticated_staff", "current_staff", "require_fresh", "require_fresh_auth", "require_permission",
]
