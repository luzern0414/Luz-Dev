"""种子：标准权限码目录（与 frontend/admin/SPEC.md §4.3 一致）与内置角色。

导入本文件即把目录登记到注册表；业务模块新增码用 declare_permissions，不必改这里。
角色权限用 `前缀:*` 表示该前缀下全部权限码（种子时展开）。
"""
from __future__ import annotations

from .permissions import declare_permissions

CATALOG: dict[str, tuple[tuple[str, str], ...]] = {
    "report": (("dashboard:read", "驾驶舱"), ("report:read", "报表查看"), ("report:export", "报表导出")),
    "order": (
        ("order:read", "订单查看"), ("order:accept", "接单"), ("order:prepare", "开始制作"), ("order:ready", "出餐 / 备货完成"),
        ("order:ship", "发货 / 开始配送"), ("order:complete", "完成订单"), ("order:cancel", "取消订单"),
        ("order:refund", "订单退款"), ("order:note", "订单备注"), ("order:print", "打印小票"),
    ),
    "after_sales": (("aftersales:read", "售后查看"), ("aftersales:approve", "售后审批")),
    "delivery": (("delivery:read", "配送查看"), ("delivery:dispatch", "配送指派 / 改派")),
    "queueing": (("table:read", "桌台查看"), ("table:write", "桌台管理"), ("queue:read", "排队查看"),
                 ("queue:call", "叫号"), ("queue:seat", "入座")),
    "catalog": (("product:read", "商品查看"), ("product:write", "商品编辑"), ("product:publish", "商品上下架"),
                ("category:write", "分类编辑"), ("setmeal:read", "套餐查看"), ("setmeal:write", "套餐编辑")),
    "inventory": (("stock:read", "库存查看"), ("stock:adjust", "库存增减")),
    "membership": (("member:read", "会员查看"), ("member:write", "会员编辑 / 冻结"),
                   ("member:sensitive", "会员敏感字段查看（写审计）"), ("points:read", "积分流水"), ("points:adjust", "积分调整")),
    "recharge": (("recharge:read", "充值记录"), ("recharge:config", "充值档位配置"), ("recharge:adjust", "余额调整")),
    "coupon": (("coupon:read", "优惠券查看"), ("coupon:write", "优惠券配置"), ("coupon:issue", "定向发券")),
    "campaign": (("campaign:read", "活动查看"), ("campaign:write", "活动配置")),
    "review": (("review:read", "评价查看"), ("review:moderate", "评价审核"), ("review:reply", "评价回复")),
    "store": (("store:read", "门店查看"), ("store:write", "门店编辑")),
    "notification": (("notify:read", "通知查看"), ("notify:template", "通知模板"), ("notify:resend", "通知重发")),
    "agent_support": (("kb:read", "知识库查看"), ("kb:write", "知识库编辑"), ("agent:read", "智能体对话审计"),
                      ("agent:config", "智能体配置")),
    "admin_rbac": (
        ("staff:read", "员工查看"), ("staff:write", "员工新建 / 编辑 / 停用 / 重置"),
        ("staff:sensitive", "员工手机号明文查看（写审计）"), ("role:read", "角色与权限码查看"),
        ("role:write", "角色新建 / 编辑 / 删除"), ("audit:read", "审计与登录日志查询、导出"),
    ),
    "app_settings": (("settings:read", "系统设置查看"), ("settings:write", "系统设置修改")),
}

for _module, _items in CATALOG.items():
    declare_permissions(_module, _items)

SUPER_ADMIN = "super_admin"

_FRONT_DESK = (
    "order:read", "order:accept", "order:prepare", "order:ready", "order:complete", "order:note", "order:print",
    "queue:*", "table:*", "product:read", "review:read",
)
_STORE_MANAGER = _FRONT_DESK + (
    "order:cancel", "order:refund", "delivery:*", "product:*", "setmeal:*", "stock:*", "member:read", "member:write",
    "points:adjust", "coupon:issue", "store:*", "report:read", "dashboard:read", "staff:read",
)
_OPERATOR = (
    "product:*", "setmeal:*", "coupon:*", "campaign:*", "review:*", "kb:*", "notify:template", "dashboard:read", "report:read",
)
_FINANCE = ("report:*", "recharge:*", "aftersales:*", "audit:read", "member:read", "order:read", "order:refund")

# (builtin_code, 名称, 说明, 权限模式, mfa_required, all_stores 默认)
BUILTIN_ROLES: tuple[tuple[str, str, str, tuple[str, ...], bool], ...] = (
    (SUPER_ADMIN, "超级管理员", "全部权限；不可编辑、不可删除；至少保留一名", ("*",), True),
    ("store_manager", "店长", "门店负责人：前台全部 + 退款 / 取消、商品、库存、会员、发券、门店、报表、员工查看", _STORE_MANAGER, False),
    ("front_desk", "前台值班", "前厅 / 收银：订单处理、桌台与排队、商品与评价只读", _FRONT_DESK, False),
    ("finance", "财务", "报表与导出、储值、售后审批、退款、审计只读", _FINANCE, True),
    ("operator", "运营", "商品 / 套餐 / 营销 / 活动 / 知识库 / 通知模板 / 评价", _OPERATOR, False),
)
