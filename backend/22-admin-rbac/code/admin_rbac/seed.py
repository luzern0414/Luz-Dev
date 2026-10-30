"""种子：标准权限码目录（与 frontend/admin/SPEC.md §4.3 一致）与内置角色。

导入本文件即把目录登记到注册表；业务模块新增码用 declare_permissions，不必改这里。
角色权限用 `前缀:*` 表示该前缀下全部权限码（原样存储，鉴权时前缀匹配，模块以后新增的码自动覆盖）。
"""
from __future__ import annotations

from .permissions import declare_permissions

# 只登记“还没有代码模块自己声明”的标准码（前端 SPEC §4.3）。已有代码的模块在自己的包里 declare_permissions，
# 以模块为准：member:*（01-identity）、order:*（08-order）、payment:*（09-wechat-pay）、after_sale:*（10-after-sales）、
# wallet:*（15-recharge）、coupon:*（16-coupon）、lottery:*（18-lottery）、notification:*（20-notification）、
# member_card:*（29-member-card）。
CATALOG: dict[str, tuple[tuple[str, str], ...]] = {
    "report": (("dashboard:read", "驾驶舱"), ("report:read", "报表查看"), ("report:export", "报表导出")),
    "delivery": (("delivery:read", "配送查看"), ("delivery:dispatch", "配送指派 / 改派")),
    "queueing": (("table:read", "桌台查看"), ("table:write", "桌台管理"), ("queue:read", "排队查看"),
                 ("queue:call", "叫号"), ("queue:seat", "入座")),
    "catalog": (("product:read", "商品查看"), ("product:write", "商品编辑"), ("product:publish", "商品上下架"),
                ("category:write", "分类编辑"), ("setmeal:read", "套餐查看"), ("setmeal:write", "套餐编辑")),
    "inventory": (("stock:read", "库存查看"), ("stock:adjust", "库存增减")),
    "membership": (("points:read", "积分流水"), ("points:adjust", "积分调整")),
    "campaign": (("campaign:read", "活动查看"), ("campaign:write", "活动配置")),
    "review": (("review:read", "评价查看"), ("review:moderate", "评价审核"), ("review:reply", "评价回复")),
    "store": (("store:read", "门店查看"), ("store:write", "门店编辑")),
    "reservation": (("booking:read", "预约查看"), ("booking:manage", "预约签到与取消")),
    "legal": (("legal:read", "协议查看"), ("legal:publish", "协议发布")),
    "agent_support": (("kb:read", "知识库查看"), ("kb:write", "知识库编辑"), ("agent:read", "智能体对话审计"),
                      ("agent:config", "智能体快捷回复与知识库设置")),
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

# 角色模式：精确码或 `module:*`（原样存储，前缀匹配）。码取自各模块的 PERMISSIONS 声明（MODULES.md 权限码一栏）。
_FRONT_DESK = (
    "order:read", "order:remark", "order:fulfil",
    "after_sale:read", "after_sale:create", "after_sale:receive",
    "queue:*", "table:*", "product:read", "review:read",
    "member:read", "wallet:read", "wallet:recharge", "wallet:consume",
    "member_card:read", "member_card:issue", "member_card:consume",
    "coupon:read", "lottery:read", "payment:read",
)
_STORE_MANAGER = _FRONT_DESK + (
    "order:*", "after_sale:*", "delivery:*", "product:*", "setmeal:*", "category:write", "stock:*",
    "member:write", "member:reveal", "points:*", "coupon:issue", "store:*", "report:read", "dashboard:read", "staff:read",
    "member_card:freeze", "member_card:transfer", "member_card:lost", "member_card:refund",
    "wallet:refund", "lottery:fulfil", "notification:read",
)
_OPERATOR = (
    "product:*", "setmeal:*", "category:write", "coupon:*", "campaign:*", "lottery:*", "review:*", "kb:*",
    "notification:read", "notification:template", "member_card:read", "member_card:product", "wallet:read",
    "wallet:config", "dashboard:read", "report:read", "member:read",
)
_FINANCE = (
    "report:*", "dashboard:read", "audit:read", "member:read", "order:read", "order:export",
    "payment:*", "after_sale:*", "wallet:*", "member_card:read", "member_card:refund", "member_card:export",
    "coupon:read", "coupon:export", "lottery:read", "lottery:export", "notification:read",
)

# (builtin_code, 名称, 说明, 权限模式, mfa_required)
BUILTIN_ROLES: tuple[tuple[str, str, str, tuple[str, ...], bool], ...] = (
    (SUPER_ADMIN, "超级管理员", "全部权限；不可编辑、不可删除；至少保留一名", ("*",), True),
    ("store_manager", "店长", "门店负责人：前台全部 + 订单 / 售后、商品、库存、会员、发券、会员卡与储值退款、门店、报表、员工查看",
     _STORE_MANAGER, False),
    ("front_desk", "前台值班", "前厅 / 收银：订单处理、售后登记、桌台与排队、储值与会员卡核销、商品与评价只读", _FRONT_DESK, False),
    ("finance", "财务", "报表与导出、支付与退款、储值、售后审批、会员卡退款、审计只读", _FINANCE, True),
    ("operator", "运营", "商品 / 套餐 / 优惠券 / 活动 / 抽奖 / 知识库 / 通知模板 / 评价 / 卡种与充值档位", _OPERATOR, False),
)
