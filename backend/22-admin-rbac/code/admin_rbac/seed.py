"""种子数据：权限码全集（与 frontend/admin/SPEC.md §4.3 完全一致）与五个默认角色。

新增权限码：先在这里登记，再在业务模块路由上 `Depends(require_permission(code))`。
角色权限用 `前缀:*` 表示该前缀下的全部权限码，由 expand_codes 展开。
"""
from __future__ import annotations

# (code, module, description)
PERMISSIONS: tuple[tuple[str, str, str], ...] = (
    ("dashboard:read", "report", "驾驶舱"),
    ("order:read", "order", "订单查看"),
    ("order:accept", "order", "接单"),
    ("order:prepare", "order", "开始制作"),
    ("order:ready", "order", "出餐 / 备货完成"),
    ("order:ship", "order", "发货 / 开始配送"),
    ("order:complete", "order", "完成订单"),
    ("order:cancel", "order", "取消订单"),
    ("order:refund", "order", "订单退款"),
    ("order:note", "order", "订单备注"),
    ("order:print", "order", "打印小票"),
    ("aftersales:read", "after_sales", "售后查看"),
    ("aftersales:approve", "after_sales", "售后审批"),
    ("delivery:read", "delivery", "配送查看"),
    ("delivery:dispatch", "delivery", "配送指派 / 改派"),
    ("table:read", "queueing", "桌台查看"),
    ("table:write", "queueing", "桌台管理"),
    ("queue:read", "queueing", "排队查看"),
    ("queue:call", "queueing", "叫号"),
    ("queue:seat", "queueing", "入座"),
    ("product:read", "catalog", "商品查看"),
    ("product:write", "catalog", "商品编辑"),
    ("product:publish", "catalog", "商品上下架"),
    ("category:write", "catalog", "分类编辑"),
    ("setmeal:read", "catalog", "套餐查看"),
    ("setmeal:write", "catalog", "套餐编辑"),
    ("stock:read", "inventory", "库存查看"),
    ("stock:adjust", "inventory", "库存增减"),
    ("member:read", "membership", "会员查看"),
    ("member:write", "membership", "会员编辑 / 冻结"),
    ("member:sensitive", "membership", "会员敏感字段查看（写审计）"),
    ("points:read", "membership", "积分流水"),
    ("points:adjust", "membership", "积分调整"),
    ("recharge:read", "recharge", "充值记录"),
    ("recharge:config", "recharge", "充值档位配置"),
    ("recharge:adjust", "recharge", "余额调整"),
    ("coupon:read", "coupon", "优惠券查看"),
    ("coupon:write", "coupon", "优惠券配置"),
    ("coupon:issue", "coupon", "定向发券"),
    ("campaign:read", "campaign", "活动查看"),
    ("campaign:write", "campaign", "活动配置"),
    ("review:read", "review", "评价查看"),
    ("review:moderate", "review", "评价审核"),
    ("review:reply", "review", "评价回复"),
    ("store:read", "store", "门店查看"),
    ("store:write", "store", "门店编辑"),
    ("notify:read", "notification", "通知查看"),
    ("notify:template", "notification", "通知模板"),
    ("notify:resend", "notification", "通知重发"),
    ("kb:read", "agent_support", "知识库查看"),
    ("kb:write", "agent_support", "知识库编辑"),
    ("agent:read", "agent_support", "智能体对话审计"),
    ("agent:config", "agent_support", "智能体配置"),
    ("report:read", "report", "报表查看"),
    ("report:export", "report", "报表导出"),
    ("staff:manage", "admin_rbac", "员工管理"),
    ("role:manage", "admin_rbac", "角色管理"),
    ("audit:read", "admin_rbac", "审计查询"),
    ("settings:read", "app_settings", "系统设置查看"),
    ("settings:write", "app_settings", "系统设置修改"),
)

ALL_CODES: frozenset[str] = frozenset(code for code, _, _ in PERMISSIONS)

_CLERK = (
    "order:read", "order:accept", "order:prepare", "order:ready", "order:complete", "order:note", "order:print",
    "queue:*", "table:*", "product:read", "review:read",
)
_MANAGER = _CLERK + (
    "order:cancel", "order:refund", "delivery:*", "product:*", "setmeal:*", "stock:*", "member:read", "member:write",
    "points:adjust", "coupon:issue", "store:*", "report:read", "dashboard:read",
)
_OPERATIONS = (
    "product:*", "setmeal:*", "coupon:*", "campaign:*", "review:*", "kb:*", "notify:template", "dashboard:read", "report:read",
)
_FINANCE = ("report:*", "recharge:*", "aftersales:*", "audit:read", "member:read")
_SYSADMIN = ("staff:manage", "role:manage", "settings:*", "audit:read", "agent:config", "notify:*")

# (name, description, patterns)
DEFAULT_ROLES: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("店员", "前厅、收银：订单处理、桌台与排队、商品与评价只读", _CLERK),
    ("店长", "门店负责人：店员全部 + 商品 / 套餐 / 库存、会员与积分、发券、门店、报表", _MANAGER),
    ("运营", "总部运营：商品 / 套餐 / 营销 / 活动 / 知识库 / 通知模板 / 评价", _OPERATIONS),
    ("财务", "总部财务：报表与导出、储值、售后审批、审计只读", _FINANCE),
    ("系统管理员", "IT：员工与角色、系统设置、审计、智能体配置、通知", _SYSADMIN),
)


def expand_codes(patterns: tuple[str, ...] | list[str]) -> list[str]:
    """把 `prefix:*` 展开为全集中以该前缀开头的权限码；普通码原样保留（必须在全集内）。"""
    result: list[str] = []
    for pattern in patterns:
        if pattern.endswith(":*"):
            prefix = pattern[:-1]
            matched = sorted(code for code in ALL_CODES if code.startswith(prefix))
            if not matched:
                raise ValueError(f"no permission matches {pattern}")
            result.extend(matched)
        else:
            if pattern not in ALL_CODES:
                raise ValueError(f"unknown permission {pattern}")
            result.append(pattern)
    return sorted(set(result))
