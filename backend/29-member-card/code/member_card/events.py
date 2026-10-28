"""会员卡订阅的发件箱事件（25-jobs worker 加载）：handler(db, payload, event_id)，幂等、不提交。"""
from .service import on_payment_event, on_prize_won, on_refund_failed_event, on_refunded_event

SUBSCRIPTIONS = {
    "payment.succeeded": on_payment_event,
    "payment.refunded": on_refunded_event,
    "payment.refund_failed": on_refund_failed_event,
    "lottery.prize_won": on_prize_won,
}
