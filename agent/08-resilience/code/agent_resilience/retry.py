"""超时 + 有界指数退避重试（带抖动）+ 熔断的组合调用。

- 只重试 TransientError（超时、连接、429、5xx），且调用必须幂等（由调用方保证：读 3 次，写只在带幂等键时重试）。
- `sleep`/`rand` 可注入，测试不真正等待、不随机。
"""
from __future__ import annotations

import random
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from typing import TypeVar

from .breaker import CircuitBreaker, CircuitOpenError

T = TypeVar("T")


class TransientError(Exception):
    """可重试错误：超时、连接失败、429、5xx。"""


_executor = ThreadPoolExecutor(max_workers=32, thread_name_prefix="agent-timeout")


def call_with_timeout(fn: Callable[[], T], timeout_s: float | None) -> T:
    """同步调用加硬超时；超时抛 TransientError。timeout_s 为 None 时直接调用（测试）。"""
    if timeout_s is None:
        return fn()
    future = _executor.submit(fn)
    try:
        return future.result(timeout=timeout_s)
    except FutureTimeout as exc:
        raise TransientError(f"timeout after {timeout_s}s") from exc


def retry_call(
    fn: Callable[[], T],
    attempts: int = 3,
    base_delay: float = 0.5,
    max_delay: float = 5.0,
    jitter: float = 0.2,
    retry_on: tuple[type[BaseException], ...] = (TransientError,),
    sleep: Callable[[float], None] = time.sleep,
    rand: Callable[[], float] = random.random,
    on_retry: Callable[[int, BaseException], None] | None = None,
) -> T:
    """有界指数退避 + 抖动；只对 retry_on 重试。attempts=1 表示不重试。"""
    last: BaseException | None = None
    for attempt in range(1, max(1, attempts) + 1):
        try:
            return fn()
        except retry_on as exc:  # type: ignore[misc]
            last = exc
            if attempt >= attempts:
                break
            delay = min(max_delay, base_delay * (2 ** (attempt - 1))) + rand() * jitter
            if on_retry:
                on_retry(attempt, exc)
            sleep(delay)
    assert last is not None
    raise last


def resilient(
    name: str,
    fn: Callable[[], T],
    timeout_s: float | None = 3.0,
    attempts: int = 1,
    breaker: CircuitBreaker | None = None,
    retry_on: tuple[type[BaseException], ...] = (TransientError,),
    sleep: Callable[[float], None] = time.sleep,
    rand: Callable[[], float] = random.random,
    base_delay: float = 0.5,
    fallback: Callable[[BaseException], T] | None = None,
) -> T:
    """超时 + 重试 + 熔断；失败且提供 fallback 时返回降级结果，否则抛出最后的异常。"""
    if breaker is not None and not breaker.allow():
        err: BaseException = CircuitOpenError(name, breaker.retry_after())
        if fallback:
            return fallback(err)
        raise err

    def guarded() -> T:
        try:
            result = call_with_timeout(fn, timeout_s)
        except retry_on:
            if breaker is not None:
                breaker.record_failure()
            raise
        if breaker is not None:
            breaker.record_success()
        return result

    try:
        return retry_call(guarded, attempts=attempts, retry_on=retry_on, sleep=sleep, rand=rand,
                          base_delay=base_delay)
    except retry_on as exc:  # type: ignore[misc]
        if fallback:
            return fallback(exc)
        raise
