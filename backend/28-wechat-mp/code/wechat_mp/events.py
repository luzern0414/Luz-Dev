"""wechat_mp 消费的发件箱主题（25-jobs worker 加载）：无。

消息推送事件不走发件箱，由 /api/v1/webhooks/wechat-mp/push 验签解密后同步分发给 register_push_handler 注册的函数。
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

SUBSCRIPTIONS: dict[str, Callable[..., Any]] = {}
