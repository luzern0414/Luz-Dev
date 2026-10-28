"""模型回退链：主模型 → 备模型 → 模板回答（SPEC §8.1）。

    chain = FallbackChain([("primary", call_primary), ("fallback", call_fallback)])
    result = chain.run(lambda fn: fn(prompt))   # FallbackResult(value, used, degraded)

每一级失败（TransientError / 任意异常，可配置）就进入下一级；全部失败抛 AllFallbacksFailed，
由调用方决定是否走模板回答。`used != 首级` 时 `degraded=["llm_fallback"]`。
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Generic, TypeVar

from .breaker import CircuitBreaker, CircuitOpenError

T = TypeVar("T")


class AllFallbacksFailed(Exception):
    def __init__(self, errors: list[tuple[str, BaseException]]):
        self.errors = errors
        super().__init__("; ".join(f"{n}: {type(e).__name__}: {e}" for n, e in errors))


@dataclass
class FallbackResult(Generic[T]):
    value: T
    used: str
    attempts: list[tuple[str, str | None]] = field(default_factory=list)   # (级别, 错误或 None)

    @property
    def degraded(self) -> list[str]:
        return ["llm_fallback"] if self.attempts and self.attempts[0][1] is not None else []


class FallbackChain:
    def __init__(self, levels: list[tuple[str, Callable[..., Any]]],
                 breakers: dict[str, CircuitBreaker] | None = None,
                 fail_on: tuple[type[BaseException], ...] = (Exception,)):
        if not levels:
            raise ValueError("fallback chain needs at least one level")
        self.levels = levels
        self.breakers = breakers or {}
        self.fail_on = fail_on

    def run(self, invoke: Callable[[Callable[..., Any]], T]) -> FallbackResult[T]:
        errors: list[tuple[str, BaseException]] = []
        attempts: list[tuple[str, str | None]] = []
        for name, fn in self.levels:
            breaker = self.breakers.get(name)
            if breaker is not None and not breaker.allow():
                err: BaseException = CircuitOpenError(name, breaker.retry_after())
                errors.append((name, err))
                attempts.append((name, "circuit_open"))
                continue
            try:
                value = invoke(fn)
            except self.fail_on as exc:
                if breaker is not None:
                    breaker.record_failure()
                errors.append((name, exc))
                attempts.append((name, f"{type(exc).__name__}: {exc}"))
                continue
            if breaker is not None:
                breaker.record_success()
            attempts.append((name, None))
            return FallbackResult(value=value, used=name, attempts=attempts)
        raise AllFallbacksFailed(errors)
