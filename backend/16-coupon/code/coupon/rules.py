"""coupon 纯函数规则：模板校验、规则快照、有效期、适用范围、抵扣计算（经 17-campaign `pricing`）、
发布后编辑规则、规则说明文案、兑换码生成。不访问数据库。
"""
from __future__ import annotations

import json
import re
import secrets
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from typing import Any

import pricing
from luzback import AppError, ErrorCode
from luzback.export import cents_to_yuan

from .models import (
    AUDIENCE_ALL, AUDIENCE_LEVELS, AUDIENCE_NEW_MEMBERS, AUDIENCE_TAGS, AUDIENCE_TYPES, CHANNEL_EVENT, CHANNELS,
    SCOPE_ALL, SCOPE_CATEGORIES, SCOPE_PRODUCT_KINDS, SCOPE_PRODUCTS, SCOPE_TYPES, TEMPLATE_TYPES, TYPE_AMOUNT_OFF,
    TYPE_EXCHANGE, TYPE_PERCENT_OFF, VALID_FIXED, VALID_RELATIVE, VALID_TYPES,
)

# 领取后写进用户券的“使用规则”字段（快照）；这些字段的修改只影响之后发出的券
USAGE_FIELDS = (
    "type", "amount_cents", "rate_bp", "max_discount_cents", "exchange_qty", "threshold_cents",
    "scope_type", "scope_ids", "exclude_product_ids", "stack_group", "stack_with_campaign", "stack_with_member",
)
VALIDITY_FIELDS = ("valid_type", "use_start_at", "use_end_at", "valid_days", "start_delay_days")
MAX_SCOPE_IDS = 500
MAX_STOCK = 100_000_000
CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # 去掉易混的 I O 0 1
CODE_PATTERN = re.compile(r"^[A-Z0-9]{8,32}$")


def aware(value: datetime | None) -> datetime | None:
    """SQLite 读回的时间不带时区，统一按 UTC。"""
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def bad(message: str, **data: Any) -> AppError:
    return AppError(ErrorCode.BAD_REQUEST, message, data or None)


def yuan(cents: int) -> str:
    text = cents_to_yuan(cents)
    return text[:-3] if text.endswith(".00") else text


# ---------------------------------------------------------------- 购物车行


@dataclass(frozen=True)
class CartLine:
    """券计算用的订单行。价格必须来自服务端（购物车 / 商品模块），不能取自客户端。

    category_id 与 17-campaign 的 Promotion.categories 对应时，按 `str(category_id)` 比较。
    product_kind：商品种类（goods / member_card / course / set_meal …），用于“按商品种类”适用范围。
    campaign_priced：已享活动改价（不与活动同享的券跳过此行）。
    """

    sku_id: int
    product_id: int
    qty: int
    unit_price_cents: int
    category_id: int | None = None
    product_kind: str = ""
    campaign_priced: bool = False

    @property
    def amount(self) -> int:
        return self.qty * self.unit_price_cents


def from_quote_line(line: Any, product_kind: str = "") -> CartLine:
    """luzback.contracts.QuoteLine → CartLine。"""
    return CartLine(sku_id=line.sku_id or line.product_id, product_id=line.product_id, qty=line.qty,
                    unit_price_cents=line.unit_price_cents, category_id=line.category_id,
                    product_kind=product_kind or getattr(line, "product_kind", "") or "",
                    campaign_priced=bool(getattr(line, "campaign_priced", False)))


def validate_lines(lines: list[CartLine]) -> None:
    if not lines:
        raise bad("order lines are empty")
    if len(lines) > 200:
        raise bad("too many order lines")
    for line in lines:
        if line.qty <= 0 or line.unit_price_cents < 0:
            raise bad("invalid order line", sku_id=line.sku_id)


# ---------------------------------------------------------------- 模板校验


def _ids(value: Any, name: str, kinds: bool = False) -> list:
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > MAX_SCOPE_IDS:
        raise bad(f"{name}: at most {MAX_SCOPE_IDS} items")
    out: list = []
    for item in value:
        if kinds:
            if not isinstance(item, str) or not 1 <= len(item) <= 32:
                raise bad(f"{name}: product kinds must be 1-32 character strings")
        elif not isinstance(item, int) or isinstance(item, bool) or item <= 0:
            raise bad(f"{name}: ids must be positive integers")
        if item not in out:
            out.append(item)
    return out


def validate_template(data: dict[str, Any]) -> dict[str, Any]:
    """校验一份完整的模板配置并返回规范化后的副本；不合法 4001。"""
    d = dict(data)
    if d.get("type") not in TEMPLATE_TYPES:
        raise bad(f"type must be one of {', '.join(TEMPLATE_TYPES)}")
    name = (d.get("name") or "").strip()
    if not 1 <= len(name) <= 64:
        raise bad("name must be 1-64 characters")
    d["name"] = name
    d["description"] = (d.get("description") or "").strip()[:500]
    for key in ("amount_cents", "threshold_cents"):
        d[key] = int(d.get(key) or 0)
        if d[key] < 0:
            raise bad(f"{key} must be >= 0")
    d["rate_bp"] = int(d.get("rate_bp") or 10000)
    d["exchange_qty"] = int(d.get("exchange_qty") or 1)
    cap = d.get("max_discount_cents")
    if cap is not None and int(cap) <= 0:
        raise bad("max_discount_cents must be > 0")
    d["max_discount_cents"] = None if cap is None else int(cap)
    if d["type"] == TYPE_AMOUNT_OFF:
        if d["amount_cents"] <= 0:
            raise bad("amount_cents must be > 0 for amount_off")
        d["max_discount_cents"] = None
    elif d["type"] == TYPE_PERCENT_OFF:
        # pricing 引擎按整数“折”计算（rate_percent），因此 rate_bp 需为 100 的倍数（九五折 = 9500）
        if not 100 <= d["rate_bp"] <= 9900 or d["rate_bp"] % 100:
            raise bad("rate_bp must be a multiple of 100 between 100 and 9900 (8500 = 85% payable)")
        d["amount_cents"] = 0
    else:
        if not 1 <= d["exchange_qty"] <= 99:
            raise bad("exchange_qty must be 1-99")
        d["amount_cents"] = 0
        d["max_discount_cents"] = None
    scope = d.get("scope_type") or SCOPE_ALL
    if scope not in SCOPE_TYPES:
        raise bad(f"scope_type must be one of {', '.join(SCOPE_TYPES)}")
    d["scope_type"] = scope
    d["scope_ids"] = _ids(d.get("scope_ids"), "scope_ids", kinds=scope == SCOPE_PRODUCT_KINDS)
    if scope == SCOPE_ALL and d["scope_ids"]:
        raise bad("scope_ids must be empty when scope_type is all")
    if scope != SCOPE_ALL and not d["scope_ids"]:
        raise bad("scope_ids is required for a limited scope")
    if d["type"] == TYPE_EXCHANGE and scope == SCOPE_ALL:
        raise bad("exchange coupons must name the products, categories or product kinds they exchange")
    d["exclude_product_ids"] = _ids(d.get("exclude_product_ids"), "exclude_product_ids")
    group = (d.get("stack_group") or "").strip()
    if len(group) > 32:
        raise bad("stack_group: at most 32 characters")
    d["stack_group"] = group
    d["stack_with_campaign"] = bool(d.get("stack_with_campaign", True))
    d["stack_with_member"] = bool(d.get("stack_with_member", True))

    valid_type = d.get("valid_type") or VALID_RELATIVE
    if valid_type not in VALID_TYPES:
        raise bad(f"valid_type must be one of {', '.join(VALID_TYPES)}")
    d["valid_type"] = valid_type
    for key in ("use_start_at", "use_end_at", "claim_start_at", "claim_end_at"):
        d[key] = aware(d.get(key))
    d["start_delay_days"] = int(d.get("start_delay_days") or 0)
    if valid_type == VALID_FIXED:
        if d["use_start_at"] is None or d["use_end_at"] is None or d["use_start_at"] >= d["use_end_at"]:
            raise bad("fixed validity needs use_start_at < use_end_at")
        d["valid_days"] = None
        d["start_delay_days"] = 0
    else:
        days = d.get("valid_days")
        if days is None or not 1 <= int(days) <= 3650:
            raise bad("relative validity needs valid_days between 1 and 3650")
        d["valid_days"] = int(days)
        if not 0 <= d["start_delay_days"] <= 365:
            raise bad("start_delay_days must be 0-365")
        d["use_start_at"] = d["use_end_at"] = None
    if d["claim_start_at"] and d["claim_end_at"] and d["claim_start_at"] >= d["claim_end_at"]:
        raise bad("claim_start_at must be earlier than claim_end_at")
    if valid_type == VALID_FIXED and d["claim_end_at"] and d["claim_end_at"] > d["use_end_at"]:
        raise bad("claim_end_at must not be later than use_end_at")

    stock = d.get("total_stock")
    if stock is not None and not 1 <= int(stock) <= MAX_STOCK:
        raise bad(f"total_stock must be 1-{MAX_STOCK} or empty for unlimited")
    d["total_stock"] = None if stock is None else int(stock)
    store = d.get("store_id")
    if store is not None and int(store) <= 0:
        raise bad("store_id must be positive")
    d["store_id"] = None if store is None else int(store)
    d["per_member_limit"] = int(d.get("per_member_limit") if d.get("per_member_limit") is not None else 1)
    d["per_member_daily_limit"] = int(d.get("per_member_daily_limit") or 0)
    if not 0 <= d["per_member_limit"] <= 1000 or not 0 <= d["per_member_daily_limit"] <= 1000:
        raise bad("per-member limits must be 0-1000 (0 = unlimited)")
    if d["per_member_limit"] and d["per_member_daily_limit"] > d["per_member_limit"]:
        raise bad("per_member_daily_limit must not exceed per_member_limit")

    d["audience"] = validate_audience(d.get("audience") or {"type": AUDIENCE_ALL})
    channels = d.get("channels") or []
    if not channels or any(c not in CHANNELS for c in channels):
        raise bad(f"channels must be a non-empty subset of {', '.join(CHANNELS)}")
    d["channels"] = list(dict.fromkeys(channels))
    topic = (d.get("trigger_topic") or "").strip() or None
    if topic and CHANNEL_EVENT not in d["channels"]:
        raise bad("trigger_topic requires the event channel")
    d["trigger_topic"] = topic
    return d


def validate_audience(audience: Any) -> dict[str, Any]:
    if not isinstance(audience, dict) or audience.get("type") not in AUDIENCE_TYPES:
        raise bad(f"audience.type must be one of {', '.join(AUDIENCE_TYPES)}")
    kind = audience["type"]
    if kind == AUDIENCE_ALL:
        return {"type": kind}
    if kind == AUDIENCE_NEW_MEMBERS:
        days = audience.get("days", 7)
        if not isinstance(days, int) or not 1 <= days <= 365:
            raise bad("audience.days must be 1-365")
        return {"type": kind, "days": days}
    if kind == AUDIENCE_LEVELS:
        levels = audience.get("levels")
        if not isinstance(levels, list) or not levels or not all(isinstance(x, int) for x in levels):
            raise bad("audience.levels must be a non-empty list of integers")
        return {"type": kind, "levels": sorted(set(levels))}
    tags = audience.get("tags")
    if not isinstance(tags, list) or not tags or not all(isinstance(x, str) and 0 < len(x) <= 32 for x in tags):
        raise bad("audience.tags must be a non-empty list of strings")
    return {"type": kind, "tags": sorted(set(tags))}


# ---------------------------------------------------------------- 快照与有效期


def snapshot(fields: dict[str, Any]) -> dict[str, Any]:
    return {key: fields.get(key) for key in USAGE_FIELDS}


def validity_window(fields: dict[str, Any], claimed_at: datetime) -> tuple[datetime, datetime]:
    """领取时计算券的可用时间段 [valid_from, valid_to)。"""
    if fields["valid_type"] == VALID_FIXED:
        return aware(fields["use_start_at"]), aware(fields["use_end_at"])
    start = claimed_at + timedelta(days=fields.get("start_delay_days") or 0)
    return start, start + timedelta(days=fields["valid_days"])


def describe(rules: dict[str, Any]) -> str:
    """面向顾客的规则说明（促销规定第七、二十条：公示门槛、范围、叠加与限制条件）。"""
    parts: list[str] = []
    threshold = rules.get("threshold_cents") or 0
    if rules["type"] == TYPE_AMOUNT_OFF:
        amount = yuan(rules["amount_cents"])
        parts.append(f"满{yuan(threshold)}元减{amount}元" if threshold else f"无门槛减{amount}元")
    elif rules["type"] == TYPE_PERCENT_OFF:
        rate = rules["rate_bp"] / 1000
        text = f"{rate:g}折"
        parts.append(f"满{yuan(threshold)}元享{text}" if threshold else text)
        if rules.get("max_discount_cents"):
            parts.append(f"最多优惠{yuan(rules['max_discount_cents'])}元")
    else:
        prefix = f"满{yuan(threshold)}元可" if threshold else "可"
        parts.append(f"{prefix}兑换适用商品{rules['exchange_qty']}件（按最低单价）")
    scope = rules.get("scope_type")
    parts.append({SCOPE_ALL: "全场通用", SCOPE_CATEGORIES: "限指定分类", SCOPE_PRODUCTS: "限指定商品",
                  SCOPE_PRODUCT_KINDS: "限指定商品种类"}.get(scope, ""))
    if rules.get("exclude_product_ids"):
        parts.append("部分商品除外")
    parts.append("可与其他组的优惠券叠加" if rules.get("stack_group") else "不可与其他优惠券同时使用")
    if not rules.get("stack_with_campaign", True):
        parts.append("不与活动优惠同享")
    if not rules.get("stack_with_member", True):
        parts.append("不与会员折扣同享（取优惠大者）")
    return "；".join(p for p in parts if p)


def describe_validity(fields: dict[str, Any], tz: str = "Asia/Shanghai") -> str:
    from zoneinfo import ZoneInfo

    if fields["valid_type"] == VALID_FIXED:
        fmt = "%Y-%m-%d %H:%M"
        start = aware(fields["use_start_at"]).astimezone(ZoneInfo(tz)).strftime(fmt)
        end = aware(fields["use_end_at"]).astimezone(ZoneInfo(tz)).strftime(fmt)
        return f"有效期 {start} 至 {end}"
    delay = fields.get("start_delay_days") or 0
    head = f"领取后第{delay}天生效，" if delay else "领取后立即生效，"
    return f"{head}有效{fields['valid_days']}天"


# ---------------------------------------------------------------- 适用范围与抵扣


def in_scope(rules: dict[str, Any], line: CartLine) -> bool:
    if line.product_id in (rules.get("exclude_product_ids") or []):
        return False
    scope = rules.get("scope_type", SCOPE_ALL)
    ids = rules.get("scope_ids") or []
    if scope == SCOPE_ALL:
        return True
    if scope == SCOPE_CATEGORIES:
        return line.category_id is not None and line.category_id in ids
    if scope == SCOPE_PRODUCTS:
        return line.product_id in ids
    return line.product_kind in ids


REASONS = {
    "not_started": "未到可用时间",
    "expired": "已过期",
    "unavailable": "当前状态不可用",
    "no_scope_items": "没有适用商品",
    "campaign_conflict": "适用商品已享受活动优惠，本券不与活动同享",
    "below_threshold": "未达使用门槛",
    "no_discount": "无可优惠金额",
    "stack_conflict": "不能与已选优惠券同时使用",
}


@dataclass
class Rejection:
    reason: str
    data: dict[str, Any] = field(default_factory=dict)

    @property
    def message(self) -> str:
        if self.reason == "below_threshold":
            return f"还差{yuan(self.data['shortfall_cents'])}元可用"
        return REASONS.get(self.reason, self.reason)

    def to_dict(self) -> dict[str, Any]:
        return {"reason": self.reason, "message": self.message, **self.data}


@dataclass
class Evaluation:
    quote: pricing.Quote
    discounts: dict[str, dict[str, Any]]           # key → {"amount_cents", "lines": [{"index", "amount_cents"}]}
    rejected: dict[str, Rejection]
    line_coupon_cents: list[int]

    @property
    def coupon_cents(self) -> int:
        return sum(item["amount_cents"] for item in self.discounts.values())


def _pricing_lines(lines: list[CartLine]) -> list[pricing.Line]:
    # pricing 用 sku_id 做适用判断；这里用“行序号 + 1”作为 sku_id，保证按行精确匹配
    return [pricing.Line(sku_id=i + 1, qty=line.qty, unit_price_cents=line.unit_price_cents,
                         category="" if line.category_id is None else str(line.category_id))
            for i, line in enumerate(lines)]


def _translate_campaign(promotions: list[pricing.Promotion], lines: list[CartLine]) -> list[pricing.Promotion]:
    out = []
    for promo in promotions:
        if promo.kind in pricing.COUPON_KINDS:
            raise ValueError("campaign promotions must not contain coupon kinds; pass coupons separately")
        if promo.sku_ids:
            rows = frozenset(i + 1 for i, line in enumerate(lines) if line.sku_id in promo.sku_ids)
            if not rows:
                continue  # 活动不适用于本单任何行
            promo = replace(promo, sku_ids=rows)
        out.append(promo)
    return out


def _exchange_value(rules: dict[str, Any], lines: list[CartLine], rows: list[int]) -> int:
    need = rules.get("exchange_qty") or 1
    value = 0
    for index in sorted(rows, key=lambda i: (lines[i].unit_price_cents, i)):
        take = min(need, lines[index].qty)
        value += take * lines[index].unit_price_cents
        need -= take
        if need == 0:
            break
    return value


def evaluate(coupons: list[tuple[str, dict[str, Any]]], lines: list[CartLine],
             campaign_promotions: list[pricing.Promotion] | tuple = (), shipping_cents: int = 0) -> Evaluation:
    """在活动优惠之后计算一张或多张券的抵扣（17-campaign `pricing.quote`），并给出不可用原因。

    coupons: [(key, 规则快照)]；多张券之间是否允许同用由调用方先用 `stack_conflicts` 判断。
    """
    validate_lines(lines)
    plines = _pricing_lines(lines)
    campaign = _translate_campaign(list(campaign_promotions), lines)
    base_quote = pricing.quote(plines, campaign, shipping_cents)
    remaining = [line.amount - base_quote.line_discount_cents[i] for i, line in enumerate(lines)]
    campaign_rows = {i for i, line in enumerate(lines) if line.campaign_priced or base_quote.line_discount_cents[i] > 0}

    rejected: dict[str, Rejection] = {}
    promos: list[pricing.Promotion] = []
    for key, rules in coupons:
        rows = [i for i, line in enumerate(lines) if in_scope(rules, line)]
        if not rows:
            rejected[key] = Rejection("no_scope_items")
            continue
        if not rules.get("stack_with_campaign", True):
            rows = [i for i in rows if i not in campaign_rows]
            if not rows:
                rejected[key] = Rejection("campaign_conflict")
                continue
        base = sum(remaining[i] for i in rows)
        threshold = rules.get("threshold_cents") or 0
        if base < threshold:
            rejected[key] = Rejection("below_threshold", {"shortfall_cents": threshold - base})
            continue
        common = {"id": key, "threshold_cents": threshold, "sku_ids": frozenset(i + 1 for i in rows),
                  "exclusive_group": f"coupon:{key}"}
        if rules["type"] == TYPE_AMOUNT_OFF:
            promo = pricing.Promotion(kind=pricing.COUPON_AMOUNT, amount_cents=rules["amount_cents"], **common)
        elif rules["type"] == TYPE_PERCENT_OFF:
            promo = pricing.Promotion(kind=pricing.COUPON_PERCENT, rate_percent=rules["rate_bp"] // 100,
                                      cap_cents=rules.get("max_discount_cents"), **common)
        else:
            promo = pricing.Promotion(kind=pricing.COUPON_AMOUNT, amount_cents=_exchange_value(rules, lines, rows),
                                      **common)
        promos.append(promo)

    full = pricing.quote(plines, campaign + promos, shipping_cents)
    keys = {p.id for p in promos}
    discounts: dict[str, dict[str, Any]] = {}
    line_coupon = [0] * len(lines)
    for item in full.discounts:
        if item["promotion_id"] in keys:
            discounts[item["promotion_id"]] = {"amount_cents": item["amount_cents"], "lines": item["lines"]}
            for share in item["lines"]:
                line_coupon[share["index"]] += share["amount_cents"]
    for item in full.rejected:
        key = item["promotion_id"]
        if key in keys and key not in discounts:
            if item["reason"] == "未达门槛":
                # 多券时后一张的门槛按前一张抵扣后的金额判断
                rejected[key] = Rejection("below_threshold", {"shortfall_cents": 0})
            else:
                rejected[key] = Rejection("no_discount")
    return Evaluation(quote=full, discounts=discounts, rejected=rejected, line_coupon_cents=line_coupon)


def stack_conflicts(groups: list[str]) -> bool:
    """多张券能否同用：空组的券不与任何券叠加；同组券互斥；不同的非空组可叠加。"""
    if len(groups) <= 1:
        return False
    if any(not g for g in groups):
        return True
    return len(set(groups)) != len(groups)


# ---------------------------------------------------------------- 发布后编辑


def _limit(value: int | None) -> float:
    return float("inf") if not value else float(value)


def unfavorable_changes(old: dict[str, Any], new: dict[str, Any]) -> list[str]:
    """新规则相对旧规则对顾客不利的字段（用于“把有利修改同步给已发出的券”）。"""
    bad_fields: list[str] = []
    if old["type"] != new["type"]:
        return ["type"]
    if new["type"] == TYPE_AMOUNT_OFF and new["amount_cents"] < old["amount_cents"]:
        bad_fields.append("amount_cents")
    if new["type"] == TYPE_PERCENT_OFF:
        if new["rate_bp"] > old["rate_bp"]:
            bad_fields.append("rate_bp")
        if _limit(new.get("max_discount_cents")) < _limit(old.get("max_discount_cents")):
            bad_fields.append("max_discount_cents")
    if new["type"] == TYPE_EXCHANGE and new["exchange_qty"] < old["exchange_qty"]:
        bad_fields.append("exchange_qty")
    if (new.get("threshold_cents") or 0) > (old.get("threshold_cents") or 0):
        bad_fields.append("threshold_cents")
    if new["scope_type"] != SCOPE_ALL:
        if new["scope_type"] != old["scope_type"] or not set(old.get("scope_ids") or []) <= set(new.get("scope_ids") or []):
            bad_fields.append("scope")
    if not set(new.get("exclude_product_ids") or []) <= set(old.get("exclude_product_ids") or []):
        bad_fields.append("exclude_product_ids")
    if old.get("stack_group") and new.get("stack_group") != old.get("stack_group"):
        bad_fields.append("stack_group")
    for flag in ("stack_with_campaign", "stack_with_member"):
        if old.get(flag) and not new.get(flag):
            bad_fields.append(flag)
    return bad_fields


def check_published_edit(old: dict[str, Any], new: dict[str, Any], issued_count: int) -> None:
    """已发布模板的编辑约束：类型与门店不可改；库存不低于已发数量；限领、领取期只能放宽。不满足 4220。"""
    def fail(message: str) -> AppError:
        return AppError(ErrorCode.BUSINESS_RULE, message)

    if new["type"] != old["type"]:
        raise fail("coupon type cannot change after publish")
    if new.get("store_id") != old.get("store_id"):
        raise fail("store cannot change after publish")
    if new.get("valid_type") != old.get("valid_type"):
        raise fail("validity type cannot change after publish")
    stock = new.get("total_stock")
    if stock is not None and stock < issued_count:
        raise fail("total_stock cannot be lower than the number already issued")
    if _limit(new["per_member_limit"]) < _limit(old["per_member_limit"]):
        raise fail("per_member_limit can only be raised after publish")
    if _limit(new["per_member_daily_limit"]) < _limit(old["per_member_daily_limit"]):
        raise fail("per_member_daily_limit can only be raised after publish")
    old_end, new_end = aware(old.get("claim_end_at")), aware(new.get("claim_end_at"))
    if old_end is None and new_end is not None or (old_end and new_end and new_end < old_end):
        raise fail("claim period can only be extended after publish; use end to stop claiming")
    old_start, new_start = aware(old.get("claim_start_at")), aware(new.get("claim_start_at"))
    if new_start and (old_start is None or new_start > old_start):
        raise fail("claim start cannot be postponed after publish")


def validity_favorable(old: dict[str, Any], new: dict[str, Any]) -> bool:
    if new["valid_type"] != old["valid_type"]:
        return False
    if new["valid_type"] == VALID_FIXED:
        return aware(new["use_start_at"]) <= aware(old["use_start_at"]) and aware(new["use_end_at"]) >= aware(old["use_end_at"])
    return new["valid_days"] == old["valid_days"] and new["start_delay_days"] == old["start_delay_days"]


# ---------------------------------------------------------------- 兑换码


def generate_code(length: int) -> str:
    return "".join(secrets.choice(CODE_ALPHABET) for _ in range(length))


def normalize_code(raw: str) -> str:
    return re.sub(r"[\s\-]", "", raw or "").upper()


def loads(text: str | None, default: Any) -> Any:
    if not text:
        return default
    return json.loads(text)


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
