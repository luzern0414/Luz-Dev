"""弹性：超时、有界抖动重试、熔断、模型回退链、预算、降级梯度（SPEC §8）。

独立进程的智能体服务不导入 luzback；本包是其精简副本加智能体特有部分（回退链、预算、降级级别）。
"""
from .breaker import BreakerRegistry, CircuitBreaker, CircuitOpenError
from .budget import BudgetExceeded, DailyTurnBudget, TurnTokenBudget
from .degrade import DEGRADE_MESSAGES, DegradeLevel, degrade_level, notice_for
from .fallback import AllFallbacksFailed, FallbackChain, FallbackResult
from .retry import TransientError, call_with_timeout, resilient, retry_call

__all__ = [
    "BreakerRegistry", "CircuitBreaker", "CircuitOpenError",
    "BudgetExceeded", "DailyTurnBudget", "TurnTokenBudget",
    "DEGRADE_MESSAGES", "DegradeLevel", "degrade_level", "notice_for",
    "AllFallbacksFailed", "FallbackChain", "FallbackResult",
    "TransientError", "call_with_timeout", "resilient", "retry_call",
]
