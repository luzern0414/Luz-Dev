"""订阅的事件主题（25-jobs worker 按此路由，见 MODULE-TEMPLATE 的事件约定）。

主题名来自 config，项目改名时只改配置。handle_event(commit=False) 按 event_id 去重，不提交（worker 统一提交 / 回滚重试）。
"""
from __future__ import annotations

from functools import partial
from typing import Any

from sqlalchemy.orm import Session

from .config import get_config
from .service import handle_event


def _route(topic: str, db: Session, payload: dict, event_id: str) -> Any:
    return handle_event(db, topic, payload, event_id, commit=False)


def _subscriptions() -> dict[str, Any]:
    cfg = get_config()
    topics = (cfg.payment_succeeded_topic, cfg.refund_succeeded_topic, cfg.checkin_topic, cfg.delivery_result_topic)
    routes = {}
    for topic in (t for t in topics if t):
        handler = partial(_route, topic)
        handler.__name__ = f"handle_{topic.replace('.', '_')}"  # type: ignore[attr-defined]
        routes[topic] = handler
    return routes


SUBSCRIPTIONS = _subscriptions()
