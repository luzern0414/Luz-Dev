"""membership 订阅的事件（25-jobs worker 加载；处理器幂等、不提交）。

| 主题 | 用到的字段 | 处理 |
|---|---|---|
| order.completed（08） | order_id, user_id, total_cents | 返积分 ref = order:{id}:earn（扣除完成前已退的金额，等级倍率） |
| order.refunded（08） | order_id, user_id, total_cents, refunded_cents, ref | 按累计退款比例追回（向上取整），余额不足记欠分 |
| lottery.prize_won（18） | draw_id, member_id, prize_kind, quantity, delivery_key, prize_name | prize_kind=points：赠 quantity 积分（ref = delivery_key），回执 ack_delivery / lottery.delivery_result |
"""
from .service import on_order_completed, on_order_refunded, on_prize_won

SUBSCRIPTIONS = {
    "order.completed": on_order_completed,
    "order.refunded": on_order_refunded,
    "lottery.prize_won": on_prize_won,
}
