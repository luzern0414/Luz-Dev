"""会员卡订阅的发件箱事件（25-jobs worker 加载）：handler(db, payload, event_id)，幂等、不提交。

支付 / 退款 payload 见 09-wechat-pay MODULE.md §7（同事务钩子是主通道，这里是补偿通道）；抽奖见 18-lottery；售后完成见 10-after-sales MODULE.md §7（lines[].fulfil_ref = member_card:{id}）。
"""
from .service import (
    on_after_sale_completed, on_payment_event, on_prize_won, on_refund_abnormal_event, on_refund_failed_event, on_refund_succeeded_event,
)

SUBSCRIPTIONS = {
    "payment.succeeded": on_payment_event,
    "refund.succeeded": on_refund_succeeded_event,
    "refund.failed": on_refund_failed_event,
    "refund.abnormal": on_refund_abnormal_event,
    "lottery.prize_won": on_prize_won,
    "after_sale.completed": on_after_sale_completed,  # 订单售卡行售后退款完成 → 终止卡（10-after-sales §7）
}
