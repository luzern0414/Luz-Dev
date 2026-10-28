"""默认场景与模板（种子数据）。ensure_default_scenes 幂等：只补缺失的场景 / 渠道，不覆盖后台改过的配置。

订阅消息模板 id 与短信模板 code 每个小程序 / 短信账号都不同，种子里留空且 enabled=False，
后台在「通知模板」里填入后启用；站内信模板默认启用，所以没有配置任何外部渠道时通知也不会丢。
"""
from __future__ import annotations

from typing import Any

DEFAULT_SCENES: list[dict[str, Any]] = [
    {
        "scene": "order.paid", "name": "支付成功", "category": "transaction", "audience": "member", "urgent": True,
        "sms_mode": "off",
        "templates": {
            "inapp": {"title_tpl": "支付成功", "body_tpl": "订单 {order_no} 已支付 {amount_yuan} 元", "page": "pages/order/detail?id={order_id}"},
            "subscribe": {"keywords": [
                {"key": "character_string1", "source": "order_no"}, {"key": "amount2", "source": "amount_cents"},
                {"key": "thing3", "source": "{goods_name}", "default": "商品"}, {"key": "time4", "source": "paid_at"},
            ], "page": "pages/order/detail?id={order_id}"},
        },
    },
    {
        "scene": "refund.succeeded", "name": "退款成功", "category": "transaction", "audience": "member", "urgent": True,
        "sms_mode": "fallback",
        "templates": {
            "inapp": {"title_tpl": "退款成功", "body_tpl": "退款 {amount_yuan} 元已原路退回", "page": "pages/order/detail?id={order_id}"},
            "subscribe": {"keywords": [
                {"key": "character_string1", "source": "order_no"}, {"key": "amount2", "source": "amount_cents"},
                {"key": "time3", "source": "refunded_at"},
            ]},
            "sms": {"params": [{"name": "amount", "source": "amount_yuan"}]},
        },
    },
    {
        "scene": "card.expiring", "name": "会员卡即将到期", "category": "reminder", "audience": "member", "urgent": False,
        "sms_mode": "fallback",
        "templates": {
            "inapp": {"title_tpl": "会员卡即将到期", "body_tpl": "您的会员卡将于 {expire_date} 到期", "page": "pages/member/card"},
            "subscribe": {"keywords": [
                {"key": "thing1", "source": "{card_name}", "default": "会员卡"}, {"key": "date2", "source": "expire_date"},
            ]},
            "sms": {"params": [{"name": "date", "source": "expire_date"}]},
        },
    },
    {
        "scene": "booking.reminder", "name": "预约提醒", "category": "reminder", "audience": "member", "urgent": False,
        "sms_mode": "fallback",
        "templates": {
            "inapp": {"title_tpl": "预约提醒", "body_tpl": "{service_name} 将于 {start_at_text} 开始", "page": "pages/booking/detail?id={booking_id}"},
            "subscribe": {"keywords": [
                {"key": "thing1", "source": "{service_name}"}, {"key": "time2", "source": "start_at"},
                {"key": "thing3", "source": "{store_name}", "default": "门店"},
            ]},
            "sms": {"params": [{"name": "service", "source": "service_name"}, {"name": "time", "source": "start_at_text"}]},
        },
    },
    {
        "scene": "security.alert", "name": "账号安全提醒", "category": "security", "audience": "member", "urgent": True,
        "sms_mode": "fallback",
        "templates": {
            "inapp": {"title_tpl": "账号安全提醒", "body_tpl": "{action}，如非本人操作请及时联系客服"},
            "sms": {"params": [{"name": "action", "source": "action"}]},
        },
    },
    {
        "scene": "lottery.prize_won", "name": "中奖领取提醒（实物奖品）", "category": "reminder", "audience": "member",
        "urgent": False, "sms_mode": "fallback",
        "templates": {
            "inapp": {"title_tpl": "恭喜中奖", "body_tpl": "您抽中了{prize_name}，请尽快填写收货信息领取",
                      "page": "pages/lottery/prize?draw_id={draw_id}"},
            "subscribe": {"keywords": [
                {"key": "thing1", "source": "{prize_name}"},
                {"key": "thing2", "source": "{claim_hint}", "default": "请在小程序内填写收货信息领取"},
            ], "page": "pages/lottery/prize?draw_id={draw_id}"},
            "sms": {"params": [{"name": "prize", "source": "prize_name"}]},
        },
    },
    {
        "scene": "staff.device_offline", "name": "设备离线告警", "category": "staff_alert", "audience": "staff", "urgent": True,
        "sms_mode": "always",
        "templates": {
            "inapp": {"title_tpl": "设备离线", "body_tpl": "{device_name} 已离线（{store_name}）"},
            "sms": {"params": [{"name": "device", "source": "device_name"}]},
        },
    },
    {
        "scene": "staff.security_alert", "name": "后台安全告警", "category": "staff_alert", "audience": "staff", "urgent": True,
        "sms_mode": "always",
        "templates": {
            "inapp": {"title_tpl": "安全告警", "body_tpl": "{detail}"},
            "sms": {"params": [{"name": "detail", "source": "detail"}]},
        },
    },
]
