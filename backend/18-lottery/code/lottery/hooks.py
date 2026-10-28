"""扩展钩子：随机源、参与人群、风控、公示昵称、限流器。项目定制在启动时调用 set_*。

约束（防内定，规范促销行为暂行规定第十六条）：
- 人群与风控钩子只在抽奖前决定“能不能参加”，拿不到随机数，也不能指定或改变奖品。
- 随机源默认 `secrets`（操作系统 CSPRNG）；只有测试注入可复现的随机源，生产禁止替换为可预测实现。
"""
from __future__ import annotations

import random
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from luzback.ratelimit import FixedWindowLimiter

from .config import get_config


class SecretsRandom:
    """生产随机源：secrets.randbelow（CSPRNG），客户端无法影响。"""

    name = "secrets"

    def randbelow(self, n: int) -> int:
        return secrets.randbelow(n)


class SeededRandom:
    """仅测试 / 离线模拟用：固定种子，可复现。"""

    name = "seeded"

    def __init__(self, seed: int):
        self._r = random.Random(seed)

    def randbelow(self, n: int) -> int:
        return self._r.randrange(n)


class FixedRandom:
    """仅测试用：总是返回固定值（取模），用于确定命中某个奖品。"""

    name = "fixed"

    def __init__(self, value: int):
        self.value = value

    def randbelow(self, n: int) -> int:
        return self.value % n


@dataclass(frozen=True)
class RiskContext:
    activity_id: int
    member_id: int
    store_id: int | None
    ip: str | None
    request_key: str


AudienceChecker = Callable[[Session, Any, int], bool]       # (db, activity, member_id) -> 是否属于参与人群
RiskChecker = Callable[[Session, RiskContext], None]         # 拒绝时抛 AppError（建议 4220 / 4290）
MemberDisplay = Callable[[Session, int], str | None]         # 公示用昵称（脱敏前）；None 时用会员号尾号

_random = SecretsRandom()
_audience: AudienceChecker = lambda db, activity, member_id: True  # noqa: E731
_risk: RiskChecker = lambda db, ctx: None  # noqa: E731
_display: MemberDisplay = lambda db, member_id: None  # noqa: E731
_member_limiter: FixedWindowLimiter | None = None
_ip_limiter: FixedWindowLimiter | None = None


def random_source():
    return _random


def set_random_source(source) -> None:
    global _random
    _random = source or SecretsRandom()


def audience_checker() -> AudienceChecker:
    return _audience


def set_audience_checker(fn: AudienceChecker | None) -> None:
    global _audience
    _audience = fn or (lambda db, activity, member_id: True)


def risk_checker() -> RiskChecker:
    return _risk


def set_risk_checker(fn: RiskChecker | None) -> None:
    global _risk
    _risk = fn or (lambda db, ctx: None)


def member_display() -> MemberDisplay:
    return _display


def set_member_display(fn: MemberDisplay | None) -> None:
    global _display
    _display = fn or (lambda db, member_id: None)


def limiters() -> tuple[FixedWindowLimiter | None, FixedWindowLimiter | None]:
    """进程内固定窗口限流（每分钟）；多实例部署换成 Redis 实现（同 allow 接口）通过 set_limiters 注入。"""
    global _member_limiter, _ip_limiter
    cfg = get_config()
    if _member_limiter is None and cfg.member_draws_per_minute > 0:
        _member_limiter = FixedWindowLimiter(cfg.member_draws_per_minute, 60)
    if _ip_limiter is None and cfg.ip_draws_per_minute > 0:
        _ip_limiter = FixedWindowLimiter(cfg.ip_draws_per_minute, 60)
    return _member_limiter, _ip_limiter


def set_limiters(member=None, ip=None) -> None:
    global _member_limiter, _ip_limiter
    _member_limiter, _ip_limiter = member, ip


def reset_hooks() -> None:
    """测试用：恢复全部默认实现。"""
    set_random_source(None)
    set_audience_checker(None)
    set_risk_checker(None)
    set_member_display(None)
    set_limiters(None, None)
