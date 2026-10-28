"""wechat_pay：微信支付 APIv3（小程序 / JSAPI，普通商户直连）。

对外（CONTRACTS.md · L2 wechat_pay，其他模块只从这里导入）：
- 支付：create_payment、pay_business、close_payment、query_and_reconcile、paid_total、get_business_payment
- 回调：handle_pay_notify、handle_refund_notify（handle_notify 为 v1 别名）
- 退款：create_refund、get_refund_status
- 发货信息：request_shipping_upload
- 对账：reconcile_bill
- 注册：register_payable、register_on_paid、register_on_refunded、set_openid_resolver、set_client、set_shipping_client
- 模型与常量：PaymentRecord、RefundRecord、PayNotifyLog、ReconBatch、ReconDiff、ShippingUpload、状态常量、PERMISSIONS
- 渠道：WechatPayClient、HttpWechatPayClient、FakeWechatPay（测试替身，见 wechat_pay.fake）
路由在 wechat_pay.api；定时任务在 wechat_pay.jobs（register_jobs）。
"""
from .client import HttpWechatPayClient, ShippingClient, WechatPayApiError, WechatPayClient
from .config import WechatPayConfig, assert_production_ready, get_config, set_config
from .models import PaymentRecord, PayNotifyLog, ReconBatch, ReconDiff, RefundRecord, ShippingUpload
from .rules import (
    GOODS_KINDS, GOODS_OFFLINE_SERVICE, GOODS_PHYSICAL, GOODS_VIRTUAL_CONTENT, PAY_CLOSED, PAY_CREATED, PAY_FAILED,
    PAY_PAID, PAY_PAYING, REFUND_ABNORMAL, REFUND_CLOSED, REFUND_FAILED, REFUND_PENDING, REFUND_PROCESSING,
    REFUND_SUCCEEDED,
)
from .service import (
    PERMISSIONS, NotifyResult, Payable, close_payment, create_payment, create_refund, get_business_payment,
    get_client, get_refund_status, handle_notify, handle_pay_notify, handle_refund_notify, paid_total, pay_business,
    query_and_reconcile, reconcile_bill, register_on_paid, register_on_refunded, register_payable,
    request_shipping_upload, set_client, set_openid_resolver, set_shipping_client,
)

__all__ = [
    "HttpWechatPayClient", "ShippingClient", "WechatPayApiError", "WechatPayClient",
    "WechatPayConfig", "assert_production_ready", "get_config", "set_config",
    "PaymentRecord", "PayNotifyLog", "ReconBatch", "ReconDiff", "RefundRecord", "ShippingUpload",
    "GOODS_KINDS", "GOODS_OFFLINE_SERVICE", "GOODS_PHYSICAL", "GOODS_VIRTUAL_CONTENT",
    "PAY_CLOSED", "PAY_CREATED", "PAY_FAILED", "PAY_PAID", "PAY_PAYING",
    "REFUND_ABNORMAL", "REFUND_CLOSED", "REFUND_FAILED", "REFUND_PENDING", "REFUND_PROCESSING", "REFUND_SUCCEEDED",
    "PERMISSIONS", "NotifyResult", "Payable", "close_payment", "create_payment", "create_refund", "get_business_payment",
    "get_client", "get_refund_status", "handle_notify", "handle_pay_notify", "handle_refund_notify", "paid_total",
    "pay_business", "query_and_reconcile", "reconcile_bill", "register_on_paid", "register_on_refunded",
    "register_payable", "request_shipping_upload", "set_client", "set_openid_resolver", "set_shipping_client",
]
