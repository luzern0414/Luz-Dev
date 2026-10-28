"""工具调用的失败重试策略。

规则：
- 只重试 TransientToolError（超时、5xx、网络抖动）。
- ToolError（参数错误、业务拒绝）不重试，直接交给模型解释。
- 指数退避：base_delay * 2^n，最多 attempts 次；sleep 可注入，测试时不真正等待。
- 有副作用的工具（create_order、cancel_order）只有在携带幂等键时才允许重试，否则只执行一次。
"""
from __future__ import annotations

import time
from collections.abc import Callable
from typing import TypeVar

from .tools import TransientToolError

T = TypeVar("T")


def call_with_retry(
    fn: Callable[[], T],
    *,
    attempts: int = 3,
    base_delay: float = 0.5,
    sleep: Callable[[float], None] = time.sleep,
    idempotent: bool = True,
) -> T:
    """执行 fn，遇到 TransientToolError 时退避重试。idempotent=False 时只执行一次。"""
    if not idempotent:
        attempts = 1
    last: Exception | None = None
    for attempt in range(attempts):
        try:
            return fn()
        except TransientToolError as exc:
            last = exc
            if attempt < attempts - 1:
                sleep(base_delay * (2 ** attempt))
    assert last is not None
    raise last
