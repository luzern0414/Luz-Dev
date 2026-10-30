"""弹性：超时、有界重试、熔断、舱壁（SPEC §9）。

    breaker = registry.get("wechat_pay")
    result = resilient("wechat_pay", lambda: client.post(...), timeout_s=8, attempts=1, breaker=breaker)

- 只重试 TransientError（超时、连接、429、5xx），且调用必须幂等。
- 熔断：60 秒窗口内 ≥10 次失败且失败率 ≥50% → open 30 秒 → half_open 放 3 个探测。
- 熔断打开抛 CircuitOpenError（5030，带 Retry-After），由调用方决定降级。
"""
from __future__ import annotations

import contextvars
import random
import threading
import time
from collections import deque
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from dataclasses import dataclass, field
from typing import Any, TypeVar

from .errors import AppError, ErrorCode

T = TypeVar("T")


class TransientError(Exception):
    """可重试错误：超时、连接失败、429、5xx。"""


class UpstreamError(AppError):
    def __init__(self, message: str = "upstream failed", data: Any = None):
        super().__init__(ErrorCode.UPSTREAM_FAILED, message, data)


class CircuitOpenError(AppError):
    def __init__(self, name: str, retry_after: int):
        super().__init__(ErrorCode.SERVICE_UNAVAILABLE, f"{name} temporarily unavailable", retry_after=retry_after)
        self.name = name


@dataclass
class CircuitBreaker:
    name: str
    window_seconds: int = 60
    min_failures: int = 10
    fail_rate: float = 0.5
    open_seconds: int = 30
    half_open_probes: int = 3
    clock: Callable[[], float] = time.monotonic
    _events: deque[tuple[float, bool]] = field(default_factory=deque)
    _state: str = "closed"
    _opened_at: float = 0.0
    _probes: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock)
    opened_count: int = 0

    @property
    def state(self) -> str:
        with self._lock:
            self._tick(self.clock())
            return self._state

    def _tick(self, now: float) -> None:
        while self._events and self._events[0][0] < now - self.window_seconds:
            self._events.popleft()
        if self._state == "open" and now - self._opened_at >= self.open_seconds:
            self._state = "half_open"
            self._probes = 0

    def allow(self) -> bool:
        now = self.clock()
        with self._lock:
            self._tick(now)
            if self._state == "closed":
                return True
            if self._state == "half_open" and self._probes < self.half_open_probes:
                self._probes += 1
                return True
            return False

    def record_success(self) -> None:
        now = self.clock()
        with self._lock:
            self._events.append((now, True))
            if self._state == "half_open":
                self._state = "closed"
                self._events.clear()

    def record_failure(self) -> None:
        now = self.clock()
        with self._lock:
            self._events.append((now, False))
            self._tick(now)
            if self._state == "half_open":
                self._open(now)
                return
            failures = sum(1 for _, ok in self._events if not ok)
            total = len(self._events)
            if failures >= self.min_failures and failures / total >= self.fail_rate:
                self._open(now)

    def _open(self, now: float) -> None:
        self._state = "open"
        self._opened_at = now
        self.opened_count += 1

    def retry_after(self) -> int:
        with self._lock:
            remaining = self.open_seconds - (self.clock() - self._opened_at)
            return max(1, int(remaining))

    def snapshot(self) -> dict[str, Any]:
        return {"name": self.name, "state": self.state, "opened_count": self.opened_count,
                "recent_failures": sum(1 for _, ok in self._events if not ok), "recent_total": len(self._events)}

    def reset(self) -> None:
        with self._lock:
            self._events.clear()
            self._state = "closed"


class BreakerRegistry:
    def __init__(self, **defaults: Any):
        self.defaults = defaults
        self._items: dict[str, CircuitBreaker] = {}
        self._lock = threading.Lock()

    def get(self, name: str, **overrides: Any) -> CircuitBreaker:
        with self._lock:
            if name not in self._items:
                self._items[name] = CircuitBreaker(name=name, **{**self.defaults, **overrides})
            return self._items[name]

    def snapshot(self) -> list[dict[str, Any]]:
        return [b.snapshot() for b in self._items.values()]


class Bulkhead:
    """限制某依赖的并发数；拿不到槽位立即失败而不是排队。"""

    def __init__(self, name: str, max_concurrent: int):
        self.name = name
        self._sem = threading.BoundedSemaphore(max_concurrent)

    def __enter__(self) -> "Bulkhead":
        if not self._sem.acquire(blocking=False):
            raise AppError(ErrorCode.SERVICE_UNAVAILABLE, f"{self.name} is busy", retry_after=1)
        return self

    def __exit__(self, *_exc: Any) -> None:
        self._sem.release()


_executor = ThreadPoolExecutor(max_workers=64, thread_name_prefix="luz-timeout")


def call_with_timeout(fn: Callable[[], T], timeout_s: float) -> T:
    """同步调用加硬超时。超时抛 TransientError；底层线程继续运行到结束（仅用于幂等的外部调用）。

    在调用方的上下文副本里执行：链路编号、运行配置快照与日志字段在超时线程里同样可用。
    """
    future = _executor.submit(contextvars.copy_context().run, fn)
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
            delay = min(max_delay, base_delay * (2 ** (attempt - 1)))
            delay += random.uniform(0, jitter)
            if on_retry:
                on_retry(attempt, exc)
            sleep(delay)
    assert last is not None
    raise last


def resilient(
    name: str,
    fn: Callable[[], T],
    timeout_s: float = 3.0,
    attempts: int = 1,
    breaker: CircuitBreaker | None = None,
    bulkhead: Bulkhead | None = None,
    retry_on: tuple[type[BaseException], ...] = (TransientError,),
    sleep: Callable[[float], None] = time.sleep,
    fallback: Callable[[BaseException], T] | None = None,
) -> T:
    """超时 + 重试 + 熔断 + 舱壁的组合调用；失败且提供 fallback 时返回降级结果。"""
    if breaker is not None and not breaker.allow():
        err: BaseException = CircuitOpenError(name, breaker.retry_after())
        if fallback:
            return fallback(err)
        raise err

    def guarded() -> T:
        try:
            if bulkhead is not None:
                with bulkhead:
                    result = call_with_timeout(fn, timeout_s)
            else:
                result = call_with_timeout(fn, timeout_s)
        except retry_on:
            if breaker is not None:
                breaker.record_failure()
            raise
        except Exception:
            # 非瞬时错误（如 4xx 业务错误）不计入熔断
            raise
        if breaker is not None:
            breaker.record_success()
        return result

    try:
        return retry_call(guarded, attempts=attempts, retry_on=retry_on, sleep=sleep)
    except retry_on as exc:  # type: ignore[misc]
        if fallback:
            return fallback(exc)
        raise UpstreamError(f"{name} failed: {exc}") from exc
