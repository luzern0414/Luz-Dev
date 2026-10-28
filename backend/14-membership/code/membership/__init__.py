"""membership：会员等级与积分（入账 / 扣减 / 冲回 / 追回欠分 / 赠送 / FIFO 过期 / 对账）、会员画像、订单与抽奖积分事件。

其他模块只从这里导入：
- 模型：MemberLevel、Member、PointsAccount、PointsLedger、MembershipOrder、PointsMismatch
- 积分：earn、spend、gift_points、revert_spend、clawback、credit、debit、balance、account_dict
- 画像：member_profile（16-coupon 人群的 ProfileProvider）
- 任务 / 事件：expire_points、reconcile；events.SUBSCRIPTIONS（order.completed、order.refunded、lottery.prize_won）

不变量：balance == Σledger.delta == Σremaining ≥ 0；每笔变动 ref_key 唯一（幂等）。
"""
from .models import (
    KIND_ADJUST, KIND_CLAWBACK, KIND_DEBT, KIND_EARN, KIND_EXPIRE, KIND_GIFT, KIND_LOTTERY, KIND_REDEEM, KIND_REVERT,
    LEDGER_KINDS, Member, MemberLevel, MembershipOrder, PointsAccount, PointsLedger, PointsMismatch,
)
from .service import (
    account_dict, balance, clawback, credit, debit, earn, expire_points, gift_points, ledger_dict, member_profile,
    on_order_completed, on_order_refunded, on_prize_won, points_valid_days, reconcile, revert_spend, spend,
)

EVENTS = ("points.changed", "points.expired", "points.reconcile_mismatch", "lottery.delivery_result")

__all__ = [
    "KIND_ADJUST", "KIND_CLAWBACK", "KIND_DEBT", "KIND_EARN", "KIND_EXPIRE", "KIND_GIFT", "KIND_LOTTERY", "KIND_REDEEM",
    "KIND_REVERT", "LEDGER_KINDS", "Member", "MemberLevel", "MembershipOrder", "PointsAccount", "PointsLedger",
    "PointsMismatch", "account_dict", "balance", "clawback", "credit", "debit", "earn", "expire_points", "gift_points",
    "ledger_dict", "member_profile", "on_order_completed", "on_order_refunded", "on_prize_won", "points_valid_days",
    "reconcile", "revert_spend", "spend", "EVENTS",
]
