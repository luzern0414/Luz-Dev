"""纯函数规则：奖池校验、选奖、复现、有利变更判定、公示脱敏、期间键。便于单测与离线模拟。"""
from __future__ import annotations

import bisect
import hashlib
import json
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime, timezone
from typing import Any, Protocol
from zoneinfo import ZoneInfo

from luzback import AppError, ErrorCode

from .config import ODDS_BASIS
from .models import (
    ACTIVITY_KINDS, OOS_POLICIES, P_CARD_DAYS, P_COUPON, P_PHYSICAL, P_POINTS, P_THANKS, PRIZE_KINDS,
)


class RandomSource(Protocol):
    name: str

    def randbelow(self, n: int) -> int: ...


def _rule(message: str, data: Any = None) -> AppError:
    return AppError(ErrorCode.BUSINESS_RULE, message, data)


def _bad(message: str, data: Any = None) -> AppError:
    return AppError(ErrorCode.BAD_REQUEST, message, data)


# ---------------------------------------------------------------- 奖品校验

def validate_prize(p: Mapping[str, Any], cap_cents: int) -> None:
    """单个奖品的字段校验（草稿保存时即执行；超过 5 万元直接拒绝）。"""
    kind = p.get("kind")
    if kind not in PRIZE_KINDS:
        raise _bad(f"prize kind must be one of {', '.join(PRIZE_KINDS)}")
    weight = p.get("weight_bp")
    if not isinstance(weight, int) or isinstance(weight, bool) or weight <= 0 or weight > ODDS_BASIS:
        raise _bad("weight_bp must be an integer in 1..10000 (不得设置不可能中出的奖项)", {"seq": p.get("seq")})
    value = int(p.get("value_cents") or 0)
    stock = p.get("stock_total")
    limit = int(p.get("per_member_limit") or 0)
    if limit < 0:
        raise _bad("per_member_limit must be >= 0")
    if stock is not None and (not isinstance(stock, int) or stock < 0):
        raise _bad("stock_total must be >= 0 or null (不限)")
    if kind == P_THANKS:
        if value or p.get("ref_id") or stock is not None or limit:
            raise _bad("thanks prize must have no value, ref, stock or limit")
        return
    if value <= 0:
        raise _bad("value_cents must be > 0 for a real prize (按同期市场价折算)", {"seq": p.get("seq")})
    if value > cap_cents:
        raise _rule(f"prize value exceeds the legal cap {cap_cents // 100} yuan (最高奖不得超过 5 万元)",
                    {"seq": p.get("seq"), "value_cents": value, "cap_cents": cap_cents})
    quantity = int(p.get("quantity") or 0)
    if quantity < 1:
        raise _bad("quantity must be >= 1", {"seq": p.get("seq")})
    if kind == P_COUPON and not p.get("ref_id"):
        raise _bad("coupon prize requires ref_id (coupon template id)", {"seq": p.get("seq")})
    if kind in (P_COUPON, P_POINTS) and not (p.get("description") or "").strip():
        raise _bad("coupon / points prize requires description of redemption rules, scope and validity (第十四条)",
                   {"seq": p.get("seq")})
    if kind == P_PHYSICAL and stock is None:
        raise _bad("physical prize requires a finite stock_total", {"seq": p.get("seq")})


def validate_pool(prizes: Sequence[Mapping[str, Any]], cap_cents: int, budget_cents: int, max_prizes: int) -> None:
    """发布前的奖池整体校验。"""
    if not 2 <= len(prizes) <= max_prizes:
        raise _rule(f"a lottery needs 2..{max_prizes} prizes")
    seqs = [int(p["seq"]) for p in prizes]
    if len(set(seqs)) != len(seqs):
        raise _bad("prize seq must be unique")
    for p in prizes:
        validate_prize(p, cap_cents)
    total = sum(int(p["weight_bp"]) for p in prizes)
    if total != ODDS_BASIS:
        raise _rule("prize weights (including thanks) must sum to exactly 10000 bp", {"sum": total})
    if not any(p["kind"] == P_THANKS for p in prizes):
        raise _rule("at least one thanks (no prize) entry is required")
    if not any(p["kind"] != P_THANKS for p in prizes):
        raise _rule("at least one real prize is required (不得谎称有奖)")
    planned = sum(int(p["value_cents"]) * int(p["stock_total"]) for p in prizes
                  if p["kind"] != P_THANKS and p.get("stock_total") is not None)
    unlimited = any(p["kind"] != P_THANKS and p.get("stock_total") is None for p in prizes)
    if budget_cents <= 0:
        raise _rule("budget_cents must be > 0")
    if planned > budget_cents:
        raise _rule("prize value × stock exceeds the budget", {"planned_cents": planned, "budget_cents": budget_cents})
    if unlimited and budget_cents <= planned:
        raise _rule("unlimited prizes need budget headroom above the limited prizes", {"planned_cents": planned})


def validate_activity_fields(a: Mapping[str, Any]) -> None:
    if a.get("kind") not in ACTIVITY_KINDS:
        raise _bad(f"kind must be one of {', '.join(ACTIVITY_KINDS)}")
    if a.get("out_of_stock_policy") not in OOS_POLICIES:
        raise _bad(f"out_of_stock_policy must be one of {', '.join(OOS_POLICIES)}")
    start, end = a.get("start_at"), a.get("end_at")
    if start is None or end is None or aware(start) >= aware(end):
        raise _bad("start_at must be earlier than end_at")
    for name in ("free_daily_chances", "limit_daily", "limit_monthly", "limit_total", "earn_payment_min_cents",
                 "earn_payment_chances", "earn_checkin_chances", "budget_cents"):
        if int(a.get(name) or 0) < 0:
            raise _bad(f"{name} must be >= 0")
    if int(a.get("claim_days") or 0) < 1:
        raise _bad("claim_days must be >= 1")
    if int(a.get("earn_payment_min_cents") or 0) > 0 and int(a.get("earn_payment_chances") or 0) < 1:
        raise _bad("earn_payment_chances must be >= 1 when earn_payment_min_cents is set")


# ---------------------------------------------------------------- 快照与哈希

def odds_snapshot(prizes: Iterable[Any]) -> list[dict[str, Any]]:
    """奖池快照（按 seq），发布时冻结；抽奖、复现、公示都只读它。"""
    rows = []
    for p in sorted(prizes, key=lambda x: _get(x, "seq")):
        rows.append({
            "prize_id": _get(p, "id"), "seq": _get(p, "seq"), "name": _get(p, "name"), "kind": _get(p, "kind"),
            "ref_id": _get(p, "ref_id"), "quantity": _get(p, "quantity"), "value_cents": _get(p, "value_cents"),
            "weight_bp": _get(p, "weight_bp"), "stock_total": _get(p, "stock_total"),
            "per_member_limit": _get(p, "per_member_limit"),
        })
    return rows


FROZEN_FIELDS = (
    "name", "kind", "store_id", "start_at", "end_at", "rules_text", "audience", "free_daily_chances", "limit_daily",
    "limit_monthly", "limit_total", "earn_payment_min_cents", "earn_payment_chances", "earn_checkin_chances",
    "out_of_stock_policy", "budget_cents", "claim_days", "early_end_allowed", "publicity_threshold_cents",
)


def rules_hash(activity: Mapping[str, Any], odds: list[dict[str, Any]]) -> str:
    body = {k: _jsonable(activity.get(k)) for k in FROZEN_FIELDS}
    body["odds"] = odds
    raw = json.dumps(body, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()


# ---------------------------------------------------------------- 选奖

def pick(odds: Sequence[Mapping[str, Any]], r: int, available: set[int] | None = None) -> tuple[dict[str, Any], int]:
    """累积权重 + 二分定位。available 为 None 时用全奖池（range = 10000）；否则只在 available 内（range = 其权重和）。

    返回 (命中的奖品, range)。r 必须在 [0, range) 内。
    """
    pool = [o for o in odds if available is None or o["prize_id"] in available]
    if not pool:
        raise _rule("no prize available")
    cumulative: list[int] = []
    acc = 0
    for o in pool:
        acc += int(o["weight_bp"])
        cumulative.append(acc)
    if not 0 <= r < acc:
        raise ValueError(f"random value {r} out of range [0, {acc})")
    return dict(pool[bisect.bisect_right(cumulative, r)]), acc


def pool_range(odds: Sequence[Mapping[str, Any]], available: set[int] | None = None) -> int:
    return sum(int(o["weight_bp"]) for o in odds if available is None or o["prize_id"] in available)


def thanks_entry(odds: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    for o in odds:
        if o["kind"] == P_THANKS:
            return dict(o)
    raise _rule("pool has no thanks entry")


def replay(odds: Sequence[Mapping[str, Any]], r: int, rng_range: int) -> int:
    """复现：用记录里的随机值、范围与当次奖池重新选奖，返回命中奖品 id（审计、争议处理）。

    记录里的 odds 是“当次实际参与抽奖的奖池”：thanks 策略为完整奖池；exclude_disclosed 策略为当时有库存的奖品。
    """
    if rng_range != pool_range(odds):
        raise ValueError("random_range does not match the recorded pool")
    return pick(odds, r)[0]["prize_id"]


def simulate(odds: Sequence[Mapping[str, Any]], n: int, rng: RandomSource) -> dict[int, int]:
    """离线模拟 n 次（不扣库存），返回 {prize_id: 次数}。"""
    counts = {int(o["prize_id"]): 0 for o in odds}
    total = pool_range(odds)
    for _ in range(n):
        prize, _ = pick(odds, rng.randbelow(total))
        counts[int(prize["prize_id"])] += 1
    return counts


def percent(weight_bp: int) -> str:
    return f"{weight_bp // 100}.{weight_bp % 100:02d}%"


# ---------------------------------------------------------------- 发布后变更

AMENDABLE = ("end_at", "free_daily_chances", "limit_daily", "limit_monthly", "limit_total", "budget_cents", "claim_days",
             "earn_payment_min_cents", "earn_payment_chances", "earn_checkin_chances")


def _limit_relaxed(old: int, new: int) -> bool:
    """次数上限：0 = 不限。放宽 = 变成不限，或在有限时调大。"""
    if new == 0:
        return True
    return old != 0 and new >= old


def is_favourable(field: str, old: Any, new: Any) -> bool:
    """发布后的变更是否只朝有利于消费者的方向（规范促销行为暂行规定第十三条第一款）。"""
    if old == new:
        return True
    if field == "end_at":
        return aware(new) > aware(old)
    if field in ("free_daily_chances", "earn_payment_chances", "earn_checkin_chances", "claim_days", "budget_cents"):
        return int(new) >= int(old)
    if field in ("limit_daily", "limit_monthly", "limit_total"):
        return _limit_relaxed(int(old), int(new))
    if field == "earn_payment_min_cents":
        return int(new) > 0 and (int(old) == 0 or int(new) <= int(old))
    return False


# ---------------------------------------------------------------- 公示与时间

def mask_member(member_id: int, display: str | None = None) -> str:
    if display:
        return display[0] + "**" if len(display) > 1 else display + "*"
    tail = str(member_id)[-4:].rjust(4, "0")
    return f"用户****{tail}"


def aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def day_key(now: datetime, tz: str) -> str:
    return aware(now).astimezone(ZoneInfo(tz)).strftime("%Y-%m-%d")


def month_key(now: datetime, tz: str) -> str:
    return aware(now).astimezone(ZoneInfo(tz)).strftime("%Y-%m")


def iso(dt: datetime | None) -> str | None:
    return aware(dt).isoformat() if dt is not None else None


def _get(obj: Any, name: str) -> Any:
    return obj[name] if isinstance(obj, Mapping) else getattr(obj, name)


def _jsonable(value: Any) -> Any:
    if isinstance(value, datetime):
        return aware(value).astimezone(timezone.utc).isoformat()
    return value
