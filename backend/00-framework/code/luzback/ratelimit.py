"""限流：令牌桶 + 固定窗口计数，内存实现与 Redis 实现同一接口。

规则（SPEC §12）：按 IP 20 次/秒（突发 40）；按用户写 3 次/秒、读 20 次/秒；
登录按设备 5 次/分钟；短信按手机号 1 次/分钟且 10 次/天。规则由 RateLimitRule 声明，
中间件按路径前缀与方法匹配；Redis 不可用时放行并记日志（降级，不阻塞交易）。
"""
from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol

from .errors import AppError, ErrorCode


class RateLimiter(Protocol):
    def allow(self, key: str, cost: float = 1.0) -> bool: ...


@dataclass
class TokenBucketLimiter:
    """每个 key 独立令牌桶。rate 每秒补充，capacity 桶容量。"""

    rate: float
    capacity: float
    clock: Callable[[], float] = time.monotonic
    _buckets: dict[str, tuple[float, float]] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def allow(self, key: str, cost: float = 1.0) -> bool:
        now = self.clock()
        with self._lock:
            tokens, last = self._buckets.get(key, (self.capacity, now))
            tokens = min(self.capacity, tokens + (now - last) * self.rate)
            if tokens >= cost:
                self._buckets[key] = (tokens - cost, now)
                return True
            self._buckets[key] = (tokens, now)
            if len(self._buckets) > 100_000:  # 防止内存无限增长
                self._buckets = {k: v for k, v in self._buckets.items() if v[1] > now - 60}
            return False


@dataclass
class FixedWindowLimiter:
    """固定窗口计数：limit 次 / window_seconds。用于登录、短信等低频高风险动作。"""

    limit: int
    window_seconds: int
    clock: Callable[[], float] = time.time
    _counts: dict[str, tuple[int, float]] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def allow(self, key: str, cost: float = 1.0) -> bool:
        now = self.clock()
        window = now // self.window_seconds
        with self._lock:
            count, win = self._counts.get(key, (0, window))
            if win != window:
                count = 0
            if count + cost > self.limit:
                self._counts[key] = (count, window)
                return False
            self._counts[key] = (count + int(cost), window)
            return True

    def retry_after(self) -> int:
        return int(self.window_seconds - (self.clock() % self.window_seconds)) or 1


class RedisTokenBucket:  # pragma: no cover - 需要 Redis
    """Redis Lua 令牌桶，与 TokenBucketLimiter 同接口；连接失败时放行并计数。"""

    SCRIPT = """
    local tokens_key = KEYS[1]
    local ts_key = KEYS[2]
    local rate = tonumber(ARGV[1])
    local capacity = tonumber(ARGV[2])
    local now = tonumber(ARGV[3])
    local cost = tonumber(ARGV[4])
    local tokens = tonumber(redis.call('get', tokens_key) or capacity)
    local last = tonumber(redis.call('get', ts_key) or now)
    tokens = math.min(capacity, tokens + (now - last) * rate)
    local allowed = 0
    if tokens >= cost then tokens = tokens - cost; allowed = 1 end
    redis.call('set', tokens_key, tokens, 'EX', 120)
    redis.call('set', ts_key, now, 'EX', 120)
    return allowed
    """

    def __init__(self, client, rate: float, capacity: float, prefix: str = "rl"):
        self.client = client
        self.rate = rate
        self.capacity = capacity
        self.prefix = prefix
        self.degraded_calls = 0
        self._sha = None

    def allow(self, key: str, cost: float = 1.0) -> bool:
        try:
            if self._sha is None:
                self._sha = self.client.script_load(self.SCRIPT)
            result = self.client.evalsha(self._sha, 2, f"{self.prefix}:{key}:t", f"{self.prefix}:{key}:ts",
                                         self.rate, self.capacity, time.time(), cost)
            return int(result) == 1
        except Exception:  # noqa: BLE001
            self.degraded_calls += 1
            return True


@dataclass
class RateLimitRule:
    """一条限流规则：匹配路径前缀与方法，用 key_fn 从请求提取维度（IP / 用户 / 手机号）。"""

    name: str
    limiter: RateLimiter
    path_prefix: str = "/api/"
    methods: tuple[str, ...] = ("GET", "POST", "PUT", "PATCH", "DELETE")
    key_fn: Callable[[object], str | None] = lambda request: None  # 返回 None 表示不适用
    retry_after: int = 1

    def matches(self, path: str, method: str) -> bool:
        return path.startswith(self.path_prefix) and method.upper() in self.methods


def require_rate_limit(limiter: RateLimiter, key: str, retry_after: int = 1) -> None:
    if not limiter.allow(key):
        raise AppError(ErrorCode.RATE_LIMITED, "too many requests, please retry later", retry_after=retry_after)
