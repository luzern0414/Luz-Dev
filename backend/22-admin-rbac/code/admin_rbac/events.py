"""admin_rbac 消费的事件：无。发出的事件见 MODULE.md §7（staff.*）。"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

SUBSCRIPTIONS: dict[str, Callable[..., Any]] = {}
