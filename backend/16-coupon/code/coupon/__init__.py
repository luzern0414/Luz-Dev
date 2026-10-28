"""优惠券模块 coupon（16-coupon）：模板、领取 / 发放 / 兑换码、下单锁券 / 核销 / 解锁 / 退券、过期与提醒、统计。

其他模块只从这里导入。约定：用例函数不 commit，由调用方（接口层 run_idempotent、订单事务、任务）提交。

订单模块接入：
    from coupon import CartLine, available_coupons, price_with_coupons, lock, redeem, unlock, return_on_refund
    报价   price_with_coupons(db, user_id, [coupon_id], lines, campaign_promotions)  → CouponPricing（行分摊）
    下单   lock(db, user_id, order_id, [coupon_id], lines, campaign_promotions)       （同一事务）
    支付   redeem(db, order_id)；关单 unlock(db, order_id)；退款 return_on_refund(db, order_id, full_refund)
发券接入：issue(db, template_id, user_id, ref_key, channel=..., source_ref=...)；或发件箱事件 coupon.issue_requested。
事件订阅：events.SUBSCRIPTIONS；定时任务：jobs.JOBS（25-jobs 装配）。
"""
from .config import (
    RETURN_ANY_REFUND, RETURN_FULL_REFUND, RETURN_NEVER, CouponConfig, MemberProfile, get_config, reset_config,
    set_audience_checker, set_cart_lines_provider, set_config, set_member_profile_provider,
)
from .deps import PERMISSIONS, jwt_member_resolver, set_member_resolver, set_staff_authenticator
from .events import SUBSCRIPTIONS, handle_event, make_consumer
from .jobs import JOBS, expire_coupons, process_issue_batches, register_jobs, remind_expiring
from .models import (
    CHANNEL_ADMIN, CHANNEL_CLAIM_CENTER, CHANNEL_CODE, CHANNEL_COMPENSATION, CHANNEL_EVENT, CHANNEL_LOTTERY,
    STATUS_EXPIRED, STATUS_LOCKED, STATUS_REVOKED, STATUS_UNUSED, STATUS_USED, TEMPLATE_ACTIVE, TEMPLATE_DRAFT,
    TEMPLATE_ENDED, TEMPLATE_PAUSED, TYPE_AMOUNT_OFF, TYPE_EXCHANGE, TYPE_PERCENT_OFF, CouponCode, CouponCodeAttempt,
    CouponCodeBatch, CouponIssueBatch, CouponIssueBatchItem, CouponIssueLog, CouponTemplate, UserCoupon,
)
from .rules import CartLine, Evaluation, evaluate, from_quote_line
from .service import (
    COUPON_FLOW, TEMPLATE_FLOW, CouponPricing, InvalidCode, available_coupons, best_coupon, claim,
    coupon_view, create_code_batch, create_issue_batch, create_template, delete_template, end_template,
    issue, lock, pause_template, try_issue, price_with_coupons, publish_template, record_code_failure, redeem,
    redeem_code, resume_template, return_on_refund, revoke, revoke_by_source, template_stats, template_view, unlock,
    update_template,
)

__all__ = [name for name in dir() if not name.startswith("_")]
