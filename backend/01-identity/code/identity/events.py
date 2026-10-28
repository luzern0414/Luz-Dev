"""identity 消费的发件箱主题（25-jobs worker 加载）。

本模块目前不消费发件箱事件：头像审核结果来自微信消息推送，经 28-wechat-mp 的
register_push_handler("wxa_media_check", handle_media_check_event) 同步处理。
发出的事件见 MODULE.md §7：member.registered / member.phone_bound / member.disabled / member.deleted。
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

SUBSCRIPTIONS: dict[str, Callable[..., Any]] = {}
