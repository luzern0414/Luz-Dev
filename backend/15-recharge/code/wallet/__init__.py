"""储值钱包（15-recharge）：本金 / 赠送金分账、充值批次、不可变流水、幂等、条件扣减、按批次退款、对账。

对外（其他模块只从这里导入）：
- 入账 / 消费 / 冲回：recharge、grant_bonus、consume、reverse、refund_card、split、ledger_matches_balance
- 充值单与支付：create_recharge_order、handle_event（payment.* 发件箱消费入口）、on_payment_succeeded
- 退款：quote_refund、request_refund、approve_refund、reject_refund、complete_offline_refund
- 预付限额扩展点：PrepaidPolicy、PRESETS、policy_for、set_policy_resolver、register_outstanding_provider
- 适配层：set_payment_port、FakePaymentPort、parse_paid、parse_refund_result、require_permission（委托 admin_rbac）、
  register_payment_hooks、set_member_resolver、set_store_timezone_resolver、find_member、store_timezone
金额单位为分；用例函数不 commit（接口层 / 事件消费 / 任务负责提交）。
"""
from .models import (
    BatchAllocation, RechargeBatch, RechargePlan, WalletAccount, WalletLedger, WalletMismatch, WalletRefund,
)
from .ports import (
    REFUND_TOPICS, FakePaymentPort, MemberRef, PaidEvent, RefundResultEvent, StaffContext, WechatPayPort,
    assert_store_scope, declare_permissions, find_member, parse_paid, parse_refund_result, payment_port,
    phone_lookup_hash, register_payment_hooks, require_permission, staff_context, set_member_resolver, set_payment_port, set_store_timezone_resolver, set_user_token_decoder,
    store_timezone, current_user,
)
from .prepaid import (
    CATEGORY_COUNT, CATEGORY_PERIOD, CATEGORY_STORED_VALUE, CONSERVATIVE, PRESETS, PrepaidPolicy, custom_policy,
    member_outstanding_cents, policy_for, register_outstanding_provider, set_policy_resolver,
)
from .rules import CAUSE_CUSTOMER, CAUSE_MERCHANT, PRINCIPAL_FIRST, PROPORTIONAL, lpr_interest, split
from .service import (
    BIZ_TYPE, adjust, approve_refund, complete_offline_refund, consume, create_recharge_order, grant_bonus, handle_event,
    ledger_matches_balance, on_payment_succeeded, quote_refund, recharge, refund_card, reject_refund, request_refund,
    reverse,
)

PERMISSIONS = (
    ("wallet:read", "储值账户、充值单、流水查看"),
    ("wallet:config", "充值档位配置与上下架"),
    ("wallet:adjust", "余额调整（原因必填，审计）"),
    ("wallet:recharge", "门店线下充值"),
    ("wallet:consume", "门店扣款 / 冲回"),
    ("wallet:refund", "充值退款发起、审批、线下退款确认"),
    ("wallet:export", "导出"),
    ("wallet:reconcile", "对账查看与执行"),
)
declare_permissions("wallet", PERMISSIONS)

__all__ = [
    "BatchAllocation", "RechargeBatch", "RechargePlan", "WalletAccount", "WalletLedger", "WalletMismatch", "WalletRefund",
    "FakePaymentPort", "MemberRef", "PaidEvent", "RefundResultEvent", "StaffContext", "WechatPayPort",
    "REFUND_TOPICS", "assert_store_scope", "declare_permissions", "find_member", "parse_paid", "parse_refund_result",
    "payment_port", "phone_lookup_hash", "register_payment_hooks", "require_permission", "staff_context",
    "set_member_resolver", "set_payment_port",
    "set_store_timezone_resolver", "set_user_token_decoder", "store_timezone", "current_user",
    "CATEGORY_COUNT", "CATEGORY_PERIOD", "CATEGORY_STORED_VALUE", "CONSERVATIVE", "PRESETS", "PrepaidPolicy",
    "custom_policy", "member_outstanding_cents", "policy_for", "register_outstanding_provider", "set_policy_resolver",
    "CAUSE_CUSTOMER", "CAUSE_MERCHANT", "PRINCIPAL_FIRST", "PROPORTIONAL", "lpr_interest", "split",
    "BIZ_TYPE", "adjust", "approve_refund", "complete_offline_refund", "consume", "create_recharge_order", "grant_bonus",
    "handle_event", "ledger_matches_balance", "on_payment_succeeded", "quote_refund", "recharge", "refund_card",
    "reject_refund", "request_refund", "reverse", "PERMISSIONS",
]
