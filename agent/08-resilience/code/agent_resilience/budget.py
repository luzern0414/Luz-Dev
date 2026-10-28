"""预算：每轮 token 上限、每用户每日轮次（SPEC §8.1）。租户每日成本预算只留接口（`DailyTurnBudget` 同样可按租户键使用）。"""
from __future__ import annotations

import threading
import time
from collections.abc import Callable


class BudgetExceeded(Exception):
    def __init__(self, what: str, limit: int):
        super().__init__(f"{what} budget exceeded (limit {limit})")
        self.what = what
        self.limit = limit


class TurnTokenBudget:
    """一轮内的 token 预算；每次模型调用后 `consume(n)`，超出抛 BudgetExceeded。"""

    def __init__(self, max_tokens: int = 8000):
        self.max_tokens = max_tokens
        self.used = 0

    def consume(self, tokens: int) -> None:
        self.used += max(0, int(tokens))
        if self.used > self.max_tokens:
            raise BudgetExceeded("turn_tokens", self.max_tokens)

    @property
    def remaining(self) -> int:
        return max(0, self.max_tokens - self.used)


class DailyTurnBudget:
    """按键（用户或租户）的每日计数；跨自然日（UTC）自动清零。"""

    def __init__(self, limit: int = 200, clock: Callable[[], float] = time.time):
        self.limit = limit
        self.clock = clock
        self._counts: dict[tuple[str, int], int] = {}
        self._lock = threading.Lock()

    def _day(self) -> int:
        return int(self.clock() // 86400)

    def check_and_increment(self, key: str) -> bool:
        """未超限则计数 +1 并返回 True；超限返回 False 且不计数。"""
        with self._lock:
            day = self._day()
            self._counts = {k: v for k, v in self._counts.items() if k[1] == day}
            used = self._counts.get((key, day), 0)
            if used >= self.limit:
                return False
            self._counts[(key, day)] = used + 1
            return True

    def used(self, key: str) -> int:
        with self._lock:
            return self._counts.get((key, self._day()), 0)
