"""order（08-order）：通用交易订单——试算（报价令牌）、下单（幂等、快照、分摊、锁券、占库存、限购、条款）、
支付（微信 / 余额）、支付成功（核销券、确认库存、履约钩子）、取消与超时关单（先查单再关单）、履约与完成、
退款额度（供 10-after-sales）、查询与后台管理。

其他模块只从这里导入：
- 用例：preview、submit、pay、cancel、confirm_receipt、ship、mark_fulfilled、admin_close、close_expired、auto_complete
- 退款额度：refund_quote、reserve_refund、release_refund、settle_refund、refund_balance、get_owned
- 扩展点：register_line_kind / LineKind / ResolvedLine、set_stock_port、set_service_catalog、set_member_card_port、
  set_promotions_provider、set_coupon_port、set_buyer_contact_provider、set_address_provider、set_terms_checker、
  set_payment_gateway、set_wallet_port、set_config
- v1 兼容：create_order、mark_paid、cancel_order、Order、OrderItem、PENDING、PAID、COMPLETED …
事件订阅 events.SUBSCRIPTIONS；定时任务 jobs.JOBS；路由 order.api。
"""
from .config import OrderConfig, get_config, reset_config, set_config
from .kinds import (
    GoodsKind, LineKind, MemberCardKind, MemberCardPort, ResolvedLine, ServiceKind, StockPort, get_line_kind,
    line_kinds, register_line_kind, set_member_card_port, set_service_catalog, set_stock_port,
)
from .models import Order, OrderItem, OrderNote, OrderQuote, OrderStatusLog, PurchaseCounter
from .ports import (
    CouponPort, PaymentGateway, WalletPort, set_address_provider, set_buyer_contact_provider, set_coupon_port,
    set_payment_gateway, set_promotions_provider, set_terms_checker, set_wallet_port,
)
from .rules import (
    ACTIVE_AFTER_PAY, CANCELLED, CLOSED, COMPLETED, FULFILLED, FULFILLING, ORDER_FLOW, PAID, PARTIALLY_REFUNDED,
    PAY_BALANCE, PAY_FREE, PAY_WECHAT, PENDING, REFUND_DONE, REFUND_FLOW, REFUND_NONE, REFUNDED, REFUNDING, SHIPPED,
    STATUSES, TABS,
)
from .service import (
    PERMISSIONS, add_note, admin_close, admin_detail, admin_get, admin_list, after_commit_shipping, app_detail, app_list,
    auto_complete, cancel, cancel_order, close_expired, confirm_receipt, create_order, get_owned, install_payment_hooks, mark_fulfilled,
    mark_needs_manual, mark_paid, on_payment_paid, order_summary, order_view, pay, preview, refund_balance,
    refund_quote, release_refund, reserve_refund, resolve_payable, settle_refund, ship, shipping_sync, submit, tab_counts,
)

install_payment_hooks()

__all__ = [name for name in dir() if not name.startswith("_")]
