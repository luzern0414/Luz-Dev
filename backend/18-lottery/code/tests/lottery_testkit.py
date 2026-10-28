"""lottery 测试共用：固定时间、默认奖池、建活动并发布。"""
from __future__ import annotations

import itertools
from datetime import datetime, timedelta, timezone

from lottery import hooks, service
from lottery.config import reset_config, set_config
from lottery.service import StaffActor

NOW = datetime(2026, 10, 28, 4, 0, tzinfo=timezone.utc)  # 上海 12:00
ADMIN = StaffActor(id=1, store_scope=None)
_keys = itertools.count(1)


def key(prefix: str = "k") -> str:
    return f"{prefix}-{next(_keys):08d}"


def prizes() -> list[dict]:
    return [
        {"seq": 1, "name": "满50减10券", "kind": "coupon", "ref_id": 7, "quantity": 1, "value_cents": 1000,
         "weight_bp": 1000, "stock_total": 100, "description": "全场满50可用，7天有效"},
        {"seq": 2, "name": "100积分", "kind": "points", "quantity": 100, "value_cents": 100, "weight_bp": 2000,
         "stock_total": None, "description": "积分1:1抵扣，365天有效"},
        {"seq": 3, "name": "空气炸锅", "kind": "physical", "quantity": 1, "value_cents": 80_000, "weight_bp": 100,
         "stock_total": 2},
        {"seq": 4, "name": "会员卡7天", "kind": "member_card_days", "quantity": 7, "value_cents": 3000,
         "weight_bp": 900, "stock_total": 50},
        {"seq": 5, "name": "谢谢参与", "kind": "thanks", "weight_bp": 6000},
    ]


# FixedRandom 命中各奖项的随机值（累积区间）：1:[0,1000) 2:[1000,3000) 3:[3000,3100) 4:[3100,4000) 5:[4000,10000)
HIT = {1: 0, 2: 1000, 3: 3000, 4: 3100, 5: 4000}


def activity_data(**over) -> dict:
    data = {
        "name": "开业大转盘", "kind": "wheel", "start_at": NOW - timedelta(hours=1), "end_at": NOW + timedelta(days=7),
        "rules_text": "活动规则：每人每天免费抽 1 次……", "free_daily_chances": 1, "budget_cents": 1_000_000,
    }
    data.update(over)
    return data


def make_activity(db, publish: bool = True, prize_list: list[dict] | None = None, actor: StaffActor = ADMIN,
                  **over) -> dict:
    act = service.create_activity(db, actor, activity_data(**over), key("create"), prize_list or prizes())
    if publish:
        act = service.publish(db, actor, act["id"], key("pub"), now=NOW)
    return act


def prize_id(act: dict, seq: int) -> int:
    return next(p["id"] for p in act["prizes"] if p["seq"] == seq)


def setup_hooks(rng=None, rate_limits: bool = False) -> None:
    hooks.reset_hooks()
    reset_config()
    if not rate_limits:
        set_config(member_draws_per_minute=0, ip_draws_per_minute=0)
    if rng is not None:
        hooks.set_random_source(rng)
