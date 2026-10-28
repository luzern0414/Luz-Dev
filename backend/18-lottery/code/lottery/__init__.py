"""lottery：抽奖活动（大转盘 / 九宫格 / 刮刮卡，同一引擎）、奖池、次数账户、服务端抽奖、发奖事件、实物领奖、公示、统计。

对外（其他模块只从这里导入）：
- 模型：LotteryActivity、LotteryPrize、LotteryDraw、LotteryChanceGrant 等
- 用例：draw(db, activity_id, user_id, idempotency_key, *, store_id=None, ip=None)（CONTRACTS）
        grant_chances(db, activity_id, member_id, chances, source, source_ref)（按业务事件发次数，幂等）
        ack_delivery(db, draw_id, ok, ref=None, error=None)（发奖消费方回执，不 commit）
        handle_event(db, topic, payload, event_id=None)（发件箱消费者入口）
- 事件契约：prize_won_payload（`lottery.prize_won` v1）
- 钩子：hooks.set_audience_checker / set_risk_checker / set_member_display / set_random_source / set_limiters
- 任务：jobs.JOBS、jobs.register_jobs(registry)
- 权限码：PERMISSIONS（lottery:read/write/publish/grant/fulfil/export）
"""
from .config import LotteryConfig, get_config, reset_config, set_config
from .deps import PERMISSIONS
from .models import (
    LotteryActivity, LotteryAmendment, LotteryChanceAccount, LotteryChanceGrant, LotteryChancePeriod, LotteryDraw,
    LotteryMemberPrizeWin, LotteryPrize,
)
from .service import (
    StaffActor, ack_delivery, activity_detail, draw, grant_chances, handle_event, prize_won_payload, revoke_grant,
    verify_draw,
)

__all__ = [
    "LotteryConfig", "get_config", "reset_config", "set_config", "PERMISSIONS",
    "LotteryActivity", "LotteryAmendment", "LotteryChanceAccount", "LotteryChanceGrant", "LotteryChancePeriod",
    "LotteryDraw", "LotteryMemberPrizeWin", "LotteryPrize",
    "StaffActor", "ack_delivery", "activity_detail", "draw", "grant_chances", "handle_event", "prize_won_payload",
    "revoke_grant", "verify_draw",
]
