"""熔断器：60 秒窗口内 ≥10 次失败且失败率 ≥50% → 打开 30 秒 → 半开放 3 个探测（SPEC §8.1）。"""
from __future__ import annotations

import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any


class CircuitOpenError(Exception):
    def __init__(self, name: str, retry_after: int):
        super().__init__(f"{name} circuit open")
        self.name = name
        self.retry_after = retry_after


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
        state = self.state
        with self._lock:
            return {"name": self.name, "state": state, "opened_count": self.opened_count,
                    "recent_failures": sum(1 for _, ok in self._events if not ok),
                    "recent_total": len(self._events)}

    def reset(self) -> None:
        with self._lock:
            self._events.clear()
            self._state = "closed"


class BreakerRegistry:
    """按依赖分组（每个模型供应商、每个后端服务、知识库）各一个熔断器。"""

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

    def is_open(self, name: str) -> bool:
        b = self._items.get(name)
        return b is not None and b.state == "open"
