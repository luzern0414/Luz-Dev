"""事件契约测试：每个订阅者只读取生产者真实发出的字段。

1. 生产者字段来自源码（tests/event_contracts.py：AST 解析每个 `enqueue_event` 调用，含辅助函数与调用点的额外字段）。
2. 每个被订阅的主题必须有生产者（防止 `payment.refunded` 这类改名后无人发布的主题）。
3. 用生产者字段造出载荷（字段值按命名给出合理类型；枚举类字段遍历几种取值），在内存库里逐个运行全部订阅者：
   读取不存在的字段（`payload["x"]` → KeyError）即失败；`payload.get("x")` 读取未发布的字段也记为失败
   （除非在 OPTIONAL_READS 白名单里写明原因）。业务异常（AppError，如“记录不存在”）不算契约问题。
"""
from __future__ import annotations

import importlib
import itertools
from datetime import datetime, timezone
from typing import Any

import pytest

from event_contracts import Produced, collect as _collect


def collect() -> dict:
    produced = _collect()
    for topic, keys in DOCUMENTED_INBOUND.items():
        produced.setdefault(topic, Produced(topic, set(keys), False, {"documented"}))
    return produced
from luzback import AppError
from luzback.db import Base, get_session_factory, init_engine

# 入站“命令”主题：基础包里没有生产者，由项目模块按文档载荷发布（16-coupon MODULE.md §7）
DOCUMENTED_INBOUND: dict[str, set[str]] = {
    "coupon.issue_requested": {"template_id", "user_id", "ref", "channel", "source_ref"},
}

# 订阅者为兼容多种生产者而 .get 读取、但当前生产者不发的字段（主题, 字段）→ 原因
OPTIONAL_READS: dict[tuple[str, str], str] = {}

ENUM_VARIANTS: dict[str, tuple[Any, ...]] = {
    "business_type": ("order", "wallet_recharge", "member_card", "membership"),
    "prize_kind": ("coupon", "points", "member_card", "physical", "balance", "none"),
    "status": ("succeeded", "failed", "abnormal"),
    "type": ("refund_only", "return_refund"),
    "route": ("original", "offline"),
}


class TrackingPayload(dict):
    """记录订阅者读取过、但载荷里没有的字段。"""

    def __init__(self, data: dict, topic: str):
        super().__init__(data)
        self.topic = topic
        self.missing: set[str] = set()

    def __getitem__(self, key):
        if key not in self:
            self.missing.add(key)
        return super().__getitem__(key)

    def get(self, key, default=None):
        if key not in self:
            self.missing.add(key)
        return super().get(key, default)


def _value(key: str, i: int) -> Any:
    now = datetime(2026, 10, 28, 4, 0, tzinfo=timezone.utc)
    if key.endswith("_at") or key in ("until", "valid_to", "ends_at", "date", "deleted_at"):
        return now.date().isoformat() if key == "date" else now.isoformat()
    if key in ("lines",):
        return [{"order_item_id": 1, "sku_id": 1, "qty": 1, "quantity": 1, "amount_cents": 100, "refund_cents": 100,
                 "fulfil_ref": "member_card:1", "kind": "member_card", "product_id": 1}]
    if key in ("coupon_ids", "recipient_ids"):
        return [1]
    if key in ("data", "audience"):
        return {} if key == "data" else "member"
    if key in ("ok", "won", "auto", "full", "full_refund", "order_full_refund", "needs_manual", "fully_reversed"):
        return i % 2 == 0
    if key.endswith(("_id", "_cents", "_delta")) or key in (
            "quantity", "attempt", "chances", "days", "days_left", "seconds", "remaining", "sessions_remaining",
            "actual_days", "diff", "by", "tier"):
        return 1
    if key == "business_ref":
        return "after_sales:1"
    if key == "ref":
        return "ref-1"
    if key == "delivery_key":
        return "lottery:draw:1"
    return f"{key}-x"


def _payloads(topic: str, keys: set[str]) -> list[dict]:
    base = {k: _value(k, 0) for k in keys}
    enum_keys = [k for k in keys if k in ENUM_VARIANTS]
    if not enum_keys:
        return [base]
    out = []
    for combo in itertools.product(*(ENUM_VARIANTS[k] for k in enum_keys)):
        p = dict(base)
        p.update(dict(zip(enum_keys, combo)))
        out.append(p)
    return out[:24]


def _packages() -> list[str]:
    from jobs.runner import module_packages
    from luzback.config import Settings

    return module_packages(Settings(features=()))


@pytest.fixture(scope="module")
def produced():
    return collect()


@pytest.fixture(scope="module")
def subscriptions():
    from jobs.runner import discover_subscriptions

    pkgs = _packages()
    for name in pkgs:  # 注册全部模型
        importlib.import_module(name)
        if importlib.util.find_spec(f"{name}.models") is not None:
            importlib.import_module(f"{name}.models")
    return discover_subscriptions(pkgs)


def test_every_subscribed_topic_has_a_producer(produced, subscriptions):
    orphans = {topic: [n for n, _ in subs] for topic, subs in subscriptions.items() if topic not in produced}
    assert not orphans, f"subscribed topics nobody publishes: {orphans}"


def test_canonical_payment_topics(produced):
    for topic in ("payment.succeeded", "payment.closed", "refund.succeeded", "refund.failed", "refund.abnormal"):
        assert topic in produced, topic
    assert "payment.refunded" not in produced
    for field in ("payment_id", "business_type", "business_id", "user_id", "store_id", "amount_cents", "paid_at"):
        assert field in produced["payment.succeeded"].keys, field
    for field in ("refund_id", "payment_id", "business_type", "business_id", "business_ref", "amount_cents", "status"):
        assert field in produced["refund.succeeded"].keys, field


def _cases():
    produced = collect()
    from jobs.runner import discover_subscriptions

    pkgs = _packages()
    for name in pkgs:
        importlib.import_module(name)
    routes = discover_subscriptions(pkgs)
    for topic, subs in sorted(routes.items()):
        if topic not in produced:
            continue
        for name, handler in subs:
            yield pytest.param(topic, name, handler, sorted(produced[topic].keys), id=f"{topic}->{name}")


@pytest.fixture()
def db():
    engine = init_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = get_session_factory()()
    yield session
    session.close()


@pytest.mark.parametrize("topic,name,handler,keys", list(_cases()))
def test_subscriber_reads_only_produced_fields(db, topic, name, handler, keys):
    problems: list[str] = []
    for n, data in enumerate(_payloads(topic, set(keys))):
        payload = TrackingPayload(data, topic)
        try:
            handler(db, payload, f"contract-{topic}-{n}")
        except KeyError as exc:
            problems.append(f"KeyError {exc} with payload {sorted(data)}")
        except AppError:
            pass  # 业务拒绝（记录不存在等）不是契约问题
        except Exception as exc:  # noqa: BLE001
            if isinstance(exc, (AttributeError, TypeError)) and "payload" in str(exc):
                problems.append(f"{type(exc).__name__}: {exc}")
        finally:
            db.rollback()
        unexpected = {k for k in payload.missing if (topic, k) not in OPTIONAL_READS}
        if unexpected:
            problems.append(f"reads fields the producer never sends: {sorted(unexpected)}")
    assert not problems, f"{name} on {topic}: " + " | ".join(sorted(set(problems)))
