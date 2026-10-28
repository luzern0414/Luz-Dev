"""after_sales（10-after-sales）：售后——仅退款 / 退货退款，按订单行数量与金额申请（额度来自订单行实付分摊，并发安全）、
审核（职责分离 + 二次验证）、退货寄回与收货、原路退款（微信 09 / 余额 15）、退款结果事件、人工处理、超时任务、查询与导出。

其他模块只从这里导入：apply、refundable、cancel、ship_return、approve、reject、receive_return、submit_refund、
on_refund_result、retry_refund、manual_settle、admin_close、模型与状态常量、PERMISSIONS、扩展点 set_refund_gateway /
set_evidence_checker / set_config。事件订阅 events.SUBSCRIPTIONS；定时任务 jobs.JOBS；路由 after_sales.api。
"""
from .config import AfterSaleConfig, get_config, reset_config, set_config
from .models import AfterSale, AfterSaleLine, AfterSaleLog
from .ports import RefundGateway, set_evidence_checker, set_refund_gateway
from .rules import (
    AFTER_SALE_FLOW, AWAITING_RETURN, CANCELLED, CLOSED, COMPLETED, OPEN, PENDING_REVIEW, REASONS, REFUND_ABNORMAL,
    REFUND_FAILED, REFUND_ONLY, REFUNDING, REJECTED, RETURN_AND_REFUND, RETURN_SHIPPED, STATUSES, TYPES,
)
from .service import (
    PERMISSIONS, admin_close, admin_list, apply, approve, auto_approve, business_ref, cancel, case_view, close_unshipped,
    get_case, manual_settle, on_refund_result, receive_return, refundable, reject, retry_refund, ship_return,
    submit_due_refunds, submit_refund,
)

__all__ = [name for name in dir() if not name.startswith("_")]
