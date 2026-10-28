"""储值订阅的发件箱事件（25-jobs worker 加载）：handler(db, payload, event_id)，幂等、不提交。

payload 契约见 09-wechat-pay MODULE.md §7；支付模块的同事务钩子是主通道，这里是补偿通道（重复时返回 duplicate）。
"""
from .service import on_payment_event, on_refund_abnormal_event, on_refund_failed_event, on_refund_succeeded_event

SUBSCRIPTIONS = {
    "payment.succeeded": on_payment_event,
    "refund.succeeded": on_refund_succeeded_event,
    "refund.failed": on_refund_failed_event,
    "refund.abnormal": on_refund_abnormal_event,
}
