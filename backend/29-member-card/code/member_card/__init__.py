"""会员卡（29-member-card）：次卡 / 期限卡 / 时段卡的卡种、购卡发卡、使用与冲正、冻结、转让、挂失补办、退卡、调整、到期。

储值卡由 15-recharge（wallet）负责；预付限额策略 PrepaidPolicy 与支付 / 鉴权适配层复用 wallet 的导出。
对外（其他模块只从这里导入）：issue_gift、consume、reverse_use、handle_event、quote_refund、check_usable、
outstanding_cents 以及模型与常量。用例函数不 commit。
"""
from wallet import declare_permissions

from .models import (
    CARD_STATUSES, KIND_COUNT, KIND_PERIOD, KIND_TIME_SLOT, CardFreeze, CardLedger, CardOrder, CardProduct, CardRefund,
    CardReminder, CardTransfer, CardUsage, MemberCard,
)
from .rules import CAUSE_CUSTOMER, CAUSE_MERCHANT, in_windows, quote_card_refund
from .service import (
    BIZ_TYPE, adjust, approve_refund, cancel_lost, check_usable, complete_offline_refund, consume, create_order,
    create_product, freeze, get_card, handle_event, issue_card, issue_gift, issue_manual, on_payment_succeeded,
    outstanding_cents, quote_refund, reject_refund, replace_card, report_lost, request_refund, reverse_use, transfer,
    unfreeze,
)

PERMISSIONS = (
    ("member_card:read", "会员卡、卡种、流水查看"),
    ("member_card:product", "卡种新建 / 编辑 / 上下架 / 删除"),
    ("member_card:issue", "后台发卡、赠卡"),
    ("member_card:consume", "核销使用、冲正"),
    ("member_card:adjust", "调整次数 / 天数（原因必填，审计）"),
    ("member_card:freeze", "冻结 / 解冻"),
    ("member_card:transfer", "转让"),
    ("member_card:lost", "挂失、解挂、补办"),
    ("member_card:disable", "停用 / 启用"),
    ("member_card:refund", "退卡报价、发起、审批、线下退款确认"),
    ("member_card:export", "导出"),
)
declare_permissions("member_card", PERMISSIONS)

__all__ = [
    "CARD_STATUSES", "KIND_COUNT", "KIND_PERIOD", "KIND_TIME_SLOT", "CardFreeze", "CardLedger", "CardOrder",
    "CardProduct", "CardRefund", "CardReminder", "CardTransfer", "CardUsage", "MemberCard",
    "CAUSE_CUSTOMER", "CAUSE_MERCHANT", "in_windows", "quote_card_refund",
    "BIZ_TYPE", "adjust", "approve_refund", "cancel_lost", "check_usable", "complete_offline_refund", "consume",
    "create_order", "create_product", "freeze", "get_card", "handle_event", "issue_card", "issue_gift", "issue_manual",
    "on_payment_succeeded", "outstanding_cents", "quote_refund", "reject_refund", "replace_card", "report_lost",
    "request_refund", "reverse_use", "transfer", "unfreeze", "PERMISSIONS",
]
