"""access_token 中控缓存（官方「中控服务器」方案的进程内实现，可接共享存储）。

规则（MODULE.md §4 R1–R6）：
- 只用 getStableAccessToken 普通模式（force_refresh=false）获取；平台在有效期内重复调用返回同一 token。
- 剩余有效期小于余量（默认 300 秒）时刷新；平台保证新旧 token 在 5 分钟内都可用。
- 单飞：同一进程内只有一个线程去取，其他线程等待后读缓存；可选分布式锁让多实例只取一次。
- 被动失效：业务接口返回 40001 / 42001 / 40014 时调用 invalidate(旧 token)。若缓存里仍是旧 token，
  先普通模式重取；平台仍返回同一 token 时才用 force_refresh=true（受 30 秒间隔限制）。
- 刷新失败但旧 token 仍未过期时继续使用旧 token（降级），过期才抛错。
"""
from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from luzback import UpstreamError

Fetcher = Callable[[bool], tuple[str, int]]  # force_refresh → (access_token, expires_in)


@dataclass(frozen=True)
class TokenEntry:
    token: str
    expires_at: float
    refresh_at: float


class TokenStore(Protocol):
    def get(self, key: str) -> TokenEntry | None: ...
    def set(self, key: str, entry: TokenEntry) -> None: ...


class MemoryTokenStore:
    def __init__(self):
        self._items: dict[str, TokenEntry] = {}
        self._lock = threading.Lock()

    def get(self, key: str) -> TokenEntry | None:
        with self._lock:
            return self._items.get(key)

    def set(self, key: str, entry: TokenEntry) -> None:
        with self._lock:
            self._items[key] = entry


class DbTokenStore:
    """存数据库（wechat_mp_access_tokens），多实例共享同一个 token。session_factory 每次新建会话。"""

    def __init__(self, session_factory: Callable[[], object]):
        self.session_factory = session_factory

    def get(self, key: str) -> TokenEntry | None:
        from sqlalchemy import select

        from .models import MpAccessToken

        with self.session_factory() as db:  # type: ignore[attr-defined]
            row = db.execute(select(MpAccessToken).where(MpAccessToken.cache_key == key)).scalar_one_or_none()
            return None if row is None else TokenEntry(row.token, row.expires_at, row.refresh_at)

    def set(self, key: str, entry: TokenEntry) -> None:
        from sqlalchemy import select
        from sqlalchemy.exc import IntegrityError

        from .models import MpAccessToken

        with self.session_factory() as db:  # type: ignore[attr-defined]
            row = db.execute(select(MpAccessToken).where(MpAccessToken.cache_key == key)).scalar_one_or_none()
            if row is None:
                db.add(MpAccessToken(cache_key=key, token=entry.token, expires_at=entry.expires_at, refresh_at=entry.refresh_at))
            else:
                row.token, row.expires_at, row.refresh_at = entry.token, entry.expires_at, entry.refresh_at
            try:
                db.commit()
            except IntegrityError:  # 另一实例同时写入：以对方为准
                db.rollback()


class LockProvider(Protocol):  # 与 luzback.scheduler.LockProvider 同接口（InMemoryLock / RedisLock）
    def acquire(self, name: str, ttl_seconds: int) -> str | None: ...
    def release(self, name: str, token: str) -> None: ...


class AccessTokenCache:
    def __init__(self, fetch: Fetcher, key: str, store: TokenStore | None = None, margin_seconds: int = 300,
                 force_min_interval: int = 30, clock: Callable[[], float] = time.time,
                 lock: LockProvider | None = None, wait: Callable[[float], None] = time.sleep):
        self.fetch = fetch
        self.key = key
        self.store = store or MemoryTokenStore()
        self.margin = margin_seconds
        self.force_min_interval = force_min_interval
        self.clock = clock
        self.dist_lock = lock
        self.wait = wait
        self._lock = threading.Lock()
        self._last_force = -1e18
        self.fetch_count = 0

    def _entry(self, token: str, expires_in: int, now: float) -> TokenEntry:
        expires_in = max(1, int(expires_in))
        if expires_in > self.margin + 60:
            refresh_at = now + expires_in - self.margin
        else:  # 平台还没换新 token（剩余不足余量）：短间隔后再取，避免每次请求都去取
            refresh_at = now + min(30, max(1, expires_in // 2))
        return TokenEntry(token, now + expires_in, refresh_at)

    def _fresh(self, entry: TokenEntry | None, now: float) -> bool:
        return entry is not None and now < entry.refresh_at and now < entry.expires_at

    def get(self) -> str:
        entry = self.store.get(self.key)
        if self._fresh(entry, self.clock()):
            return entry.token  # type: ignore[union-attr]
        with self._lock:
            now = self.clock()
            entry = self.store.get(self.key)
            if self._fresh(entry, now):
                return entry.token  # type: ignore[union-attr]
            return self._refresh(entry, force=False)

    def invalidate(self, bad_token: str) -> str:
        """业务接口报 token 无效时调用；返回可用的新 token。"""
        with self._lock:
            entry = self.store.get(self.key)
            if entry is not None and entry.token != bad_token and self.clock() < entry.expires_at:
                return entry.token  # 其他线程 / 实例已刷新
            token = self._refresh(None, force=False)
            if token != bad_token:
                return token
            now = self.clock()
            if now - self._last_force < self.force_min_interval:
                raise UpstreamError("wechat access_token rejected; force refresh throttled")
            self._last_force = now
            return self._refresh(None, force=True)

    def _refresh(self, stale: TokenEntry | None, force: bool) -> str:
        lock_token = None
        if self.dist_lock is not None:
            lock_token = self.dist_lock.acquire(f"lock:{self.key}", 10)
            if lock_token is None:  # 其他实例正在取：等它写入存储
                for _ in range(50):
                    self.wait(0.1)
                    entry = self.store.get(self.key)
                    if self._fresh(entry, self.clock()) and (stale is None or entry.token != stale.token):
                        return entry.token  # type: ignore[union-attr]
                raise UpstreamError("wechat access_token refresh in progress elsewhere")
        try:
            now = self.clock()
            try:
                self.fetch_count += 1
                token, expires_in = self.fetch(force)
            except Exception:
                if stale is not None and now < stale.expires_at:
                    return stale.token  # 降级：旧 token 仍在有效期内
                raise
            self.store.set(self.key, self._entry(token, expires_in, now))
            return token
        finally:
            if lock_token is not None:
                self.dist_lock.release(f"lock:{self.key}", lock_token)  # type: ignore[union-attr]
