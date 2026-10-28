"""coupon 事件订阅（25-jobs 事件 worker 按 SUBSCRIPTIONS 装配）。

约定：handler(db, payload, event_id) 幂等、不 commit；worker 每条事件一个会话，成功提交，异常回滚后退避重试。
- 幂等：`mark_processed(db, "coupon.<用途>", event_id)` 去掉重投；业务键 ref_key（发放日志唯一）去掉“同一业务、新事件 id”的重发。
- 业务原因发不出（售罄、限领、人群、模板未上架）不抛异常（重试也不会成功）：写 `coupon.issue_failed`，
  抽奖奖品另外回执 `lottery.ack_delivery(..., ok=False)`，由抽奖模块按退避重发。
- 并发冲突（唯一约束）抛出，worker 回滚整条事件后重试。

| 主题 | 载荷（用到的字段） | 处理 |
|---|---|---|
| member.registered（01-identity） | user_id | 发放 trigger_topic 为注册事件、含 event 渠道的进行中模板；ref = registered:{模板}:{会员} |
| lottery.prize_won | draw_id, member_id, prize_kind, ref_id(模板), quantity, delivery_key | prize_kind=coupon 时发 quantity 张，ref = lottery:{delivery_key}:{序号}；不受每人限领约束；回执抽奖模块 |
| coupon.issue_requested | template_id, user_id, ref, channel?(event / compensation / lottery), source_ref? | 其他模块请求发券（补偿、生日礼） |
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from luzback import enqueue_event
from luzback.outbox import mark_processed

from .models import CHANNEL_COMPENSATION, CHANNEL_EVENT, CHANNEL_LOTTERY, TEMPLATE_ACTIVE, CouponTemplate
from .service import try_issue

TOPIC_MEMBER_REGISTERED = "member.registered"
TOPIC_USER_REGISTERED = "user.registered"  # 旧主题名：只为兼容已存的模板 trigger_topic，不再订阅
TOPIC_LOTTERY_PRIZE_WON = "lottery.prize_won"
TOPIC_ISSUE_REQUESTED = "coupon.issue_requested"


def _member(payload: dict[str, Any]) -> int:
    # 生产方各用自己的字段名：identity / coupon.issue_requested 用 user_id，lottery.prize_won 用 member_id（只读存在的那个）
    return int(payload["user_id"] if "user_id" in payload else payload["member_id"])


def _failed(db: Session, template_id: int, user_id: int, ref: str, topic: str, reason: str) -> None:
    enqueue_event(db, "coupon.issue_failed", {"template_id": template_id, "user_id": user_id, "ref": ref,
                                              "topic": topic, "reason": reason}, "coupon_template", template_id)


def on_registered(db: Session, payload: dict[str, Any], event_id: str) -> dict[str, Any]:
    if event_id and not mark_processed(db, "coupon.registered_gift", event_id):
        return {"skipped": "duplicate"}
    user_id = _member(payload)
    templates = db.execute(select(CouponTemplate.id).where(
        CouponTemplate.trigger_topic.in_((TOPIC_MEMBER_REGISTERED, TOPIC_USER_REGISTERED)),
        CouponTemplate.status == TEMPLATE_ACTIVE, CouponTemplate.deleted_at.is_(None),
    ).order_by(CouponTemplate.id)).scalars().all()
    issued, failed = [], []
    for template_id in templates:
        ref = f"registered:{template_id}:{user_id}"
        coupon, reason = try_issue(db, template_id, user_id, CHANNEL_EVENT, ref)
        if coupon is None:
            failed.append(template_id)
            _failed(db, template_id, user_id, ref, TOPIC_MEMBER_REGISTERED, reason)
        else:
            issued.append(coupon.id)
    return {"issued": issued, "failed": failed}


def _ack_lottery(db: Session, payload: dict[str, Any], ok: bool, ref: str | None, error: str | None) -> None:
    try:
        from lottery import ack_delivery  # type: ignore
    except Exception:  # noqa: BLE001 - 抽奖模块未装配：改发回执事件
        ack_delivery = None
    if ack_delivery is not None:
        ack_delivery(db, int(payload["draw_id"]), ok, ref=ref, error=error)
        return
    enqueue_event(db, "lottery.delivery_result", {"draw_id": payload["draw_id"],
                                                  "delivery_key": payload.get("delivery_key"), "ok": ok,
                                                  "ref": ref, "error": error}, "lottery_draw", payload["draw_id"])


def on_prize_won(db: Session, payload: dict[str, Any], event_id: str) -> dict[str, Any]:
    if payload.get("prize_kind") != "coupon":
        return {"skipped": "not_coupon"}
    if event_id and not mark_processed(db, "coupon.lottery_prize", event_id):
        return {"skipped": "duplicate"}
    user_id = _member(payload)
    template_id = int(payload["ref_id"])
    key = payload.get("delivery_key") or f"draw:{payload['draw_id']}"
    quantity = max(1, int(payload.get("quantity") or 1))
    issued: list[int] = []
    for n in range(1, quantity + 1):
        ref = f"lottery:{key}:{n}"[:128]
        coupon, reason = try_issue(db, template_id, user_id, CHANNEL_LOTTERY, ref,
                                   source_ref=f"lottery_draw:{payload['draw_id']}", enforce_limits=False)
        if coupon is None:
            _failed(db, template_id, user_id, ref, TOPIC_LOTTERY_PRIZE_WON, reason)
            _ack_lottery(db, payload, False, None, f"coupon:{reason}")
            return {"issued": issued, "failed": reason}
        issued.append(coupon.id)
    _ack_lottery(db, payload, True, ",".join(str(i) for i in issued), None)
    return {"issued": issued}


def on_issue_requested(db: Session, payload: dict[str, Any], event_id: str) -> dict[str, Any]:
    if event_id and not mark_processed(db, "coupon.issue_requested", event_id):
        return {"skipped": "duplicate"}
    channel = payload.get("channel") or CHANNEL_EVENT
    if channel not in (CHANNEL_EVENT, CHANNEL_COMPENSATION, CHANNEL_LOTTERY):
        channel = CHANNEL_EVENT
    user_id, template_id, ref = _member(payload), int(payload["template_id"]), str(payload["ref"])[:128]
    coupon, reason = try_issue(db, template_id, user_id, channel, ref, source_ref=payload.get("source_ref"),
                               enforce_limits=channel == CHANNEL_EVENT)
    if coupon is None:
        _failed(db, template_id, user_id, ref, TOPIC_ISSUE_REQUESTED, reason)
        return {"issued": [], "failed": reason}
    return {"issued": [coupon.id]}


SUBSCRIPTIONS = {
    TOPIC_MEMBER_REGISTERED: on_registered,
    TOPIC_LOTTERY_PRIZE_WON: on_prize_won,
    TOPIC_ISSUE_REQUESTED: on_issue_requested,
}


def handle_event(db: Session, topic: str, payload: dict[str, Any], event: Any = None) -> Any:
    """便利入口（不经 25-jobs worker 时）：调用订阅函数并提交；异常回滚后抛出。"""
    handler = SUBSCRIPTIONS.get(topic)
    if handler is None:
        return {"skipped": "topic"}
    event_id = getattr(event, "event_id", None) or payload.get("event_id") or ""
    try:
        result = handler(db, payload, event_id)
        db.commit()
        return result
    except Exception:
        db.rollback()
        raise


def make_consumer(session_factory):
    """给 `luzback.dispatch_pending` / OutboxWorker 的 handler(topic, payload, event)：每条事件独立会话。"""
    def handler(topic: str, payload: dict[str, Any], event: Any = None) -> None:
        if topic not in SUBSCRIPTIONS:
            return
        db = session_factory()
        try:
            handle_event(db, topic, payload, event)
        finally:
            db.close()
    return handler
