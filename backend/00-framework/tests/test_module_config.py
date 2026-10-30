"""模块配置：部署参数来自环境变量、运行参数只来自运行配置（同名合并、共用分组）、热加载后读到新值、
测试覆盖走 make_config_store 并在测试结束撤销、生产环境不能用测试覆盖、旧环境变量名启动时报去处。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import partial

import pytest

from luzback.config import env_warnings
from luzback.module_config import ModuleConfig, runtime_default
from luzback.runtime_config import RuntimeConfig, legacy_env_index, registry
from luzback.runtime_loader import ConfigInvalid, config_store, current_config, set_config_store, use_snapshot
from luzback.testing import dump_toml, make_config_store, override_runtime, reset_runtime_overrides

_rt = partial(runtime_default, "wallet")


@dataclass(frozen=True)
class DemoConfig:
    region: str = field(default_factory=lambda: os.getenv("DEMO_REGION", "cn"))  # 部署参数
    pay_timeout_minutes: int = field(default_factory=_rt("pay_timeout_minutes"))
    custom_amount_enabled: bool = field(default_factory=_rt("custom_amount_enabled"))
    adjust_max_cents: int = field(default_factory=_rt("adjust_max_cents"))
    refund_requires_approval: bool = field(default_factory=_rt("refund_requires_approval"))
    custom_amount_min_cents: int = field(default_factory=_rt("custom_amount_min_cents"))
    original_refund_max_days: int = field(default_factory=_rt("original_refund_max_days"))
    stop_prepaid: bool = field(default_factory=partial(runtime_default, "prepaid")("stop_prepaid"))


def _module() -> ModuleConfig[DemoConfig]:
    return ModuleConfig("wallet", DemoConfig, shared={"stop_prepaid": "prepaid.stop_prepaid"})


def test_runtime_values_come_from_the_snapshot_and_follow_reloads(tmp_path, monkeypatch):
    store = make_config_store(tmp_path, values={"wallet": {"pay_timeout_minutes": 20}, "prepaid": {"stop_prepaid": True}})
    previous = set_config_store(store)
    monkeypatch.setenv("DEMO_REGION", "hk")
    try:
        module = _module()
        cfg = module.get()
        assert (cfg.region, cfg.pay_timeout_minutes, cfg.stop_prepaid) == ("hk", 20, True)
        assert module.get() is cfg  # 同一快照不重复构造
        (tmp_path / "dev.toml").write_text(dump_toml({"wallet": {"pay_timeout_minutes": 25}}), encoding="utf-8")
        snapshot = store.current()
        assert store.reload("file_change").result == "loaded"
        assert module.get().pay_timeout_minutes == 25 and module.get().stop_prepaid is False  # 热加载后下一次读取就是新值
        with use_snapshot(snapshot):  # 进行中的请求仍用开始时的快照
            assert module.get().pay_timeout_minutes == 20
        monkeypatch.setenv("DEMO_REGION", "mo")
        assert module.get().region == "hk" and module.reload().region == "mo"  # 部署参数改了要重启（reload 给测试用）
    finally:
        set_config_store(previous)


def test_set_routes_runtime_values_into_a_test_config_and_reset_undoes_it():
    module = _module()
    before = config_store()
    cfg = module.set(region="test", pay_timeout_minutes=5, stop_prepaid=True)
    assert (cfg.region, cfg.pay_timeout_minutes, cfg.stop_prepaid) == ("test", 5, True)
    assert current_config().wallet.pay_timeout_minutes == 5 and current_config().prepaid.stop_prepaid is True
    assert config_store() is not before  # 运行参数只有运行配置一个来源：测试也是改运行配置
    with pytest.raises(ConfigInvalid):  # 越界值同样被字段说明拒绝，原配置不变
        module.set(pay_timeout_minutes=0)
    assert current_config().wallet.pay_timeout_minutes == 5
    module.reset()
    assert current_config().wallet.pay_timeout_minutes == RuntimeConfig().wallet.pay_timeout_minutes
    assert module.get().region == os.getenv("DEMO_REGION", "cn") and config_store() is before


def test_override_runtime_accumulates_and_reset_restores(monkeypatch):
    original = config_store()
    override_runtime(order={"pay_timeout_minutes": 7})
    override_runtime(coupon={"max_coupons_per_order": 1})
    assert current_config().order.pay_timeout_minutes == 7 and current_config().coupon.max_coupons_per_order == 1
    reset_runtime_overrides("order")
    assert current_config().order.pay_timeout_minutes == 15 and current_config().coupon.max_coupons_per_order == 1
    reset_runtime_overrides()
    assert config_store() is original
    with pytest.raises(ConfigInvalid):
        override_runtime(order={"no_such_key": 1})  # 拼错的键报错，不会悄悄用默认值


def test_override_runtime_is_refused_outside_tests(monkeypatch):
    import sys

    from luzback import config as luz_config

    monkeypatch.setattr(luz_config, "get_settings", lambda: luz_config.Settings(env="prod"))
    monkeypatch.delitem(sys.modules, "pytest")
    try:
        import luzback.testing as testing

        monkeypatch.setattr(testing, "get_settings", lambda: luz_config.Settings(env="prod"))
        with pytest.raises(RuntimeError):
            testing.override_runtime(order={"pay_timeout_minutes": 5})
    finally:
        sys.modules["pytest"] = pytest


def test_every_module_group_lists_old_env_names_and_startup_points_to_them():
    index = legacy_env_index()
    for name in ("ORDER_PAY_TIMEOUT_MINUTES", "IDENTITY_SMS_TTL", "WECHAT_PAY_TIMEOUT_PREPAY", "AFTER_SALE_NO_REASON_DAYS",
                 "WALLET_PAY_TIMEOUT_MINUTES", "MEMBER_CARD_REMINDER_DAYS", "COUPON_MAX_PER_ORDER", "LUZ_LOTTERY_IP_PER_MIN",
                 "NOTIFY_SMS_PER_DAY", "ADMIN_LOGIN_MAX_FAILURES", "WECHAT_MP_TIMEOUT", "LUZ_STOP_PREPAID", "SMS_DAILY_LIMIT"):
        assert name in index, name
    warnings = dict(env_warnings({"ORDER_PAY_TIMEOUT_MINUTES": "5", "LUZ_STOP_PREPAID": "1", "WECHAT_PAY_MCHID": "1"}))
    assert warnings["ORDER_PAY_TIMEOUT_MINUTES"] == "config/runtime [order] pay_timeout_minutes"
    assert warnings["LUZ_STOP_PREPAID"] == "config/runtime [prepaid] stop_prepaid"
    assert "WECHAT_PAY_MCHID" not in warnings  # 商户号是部署参数，仍在环境变量
    for group in ("identity", "order", "wechat_pay", "after_sales", "wallet", "member_card", "coupon", "lottery",
                  "notification", "admin_rbac", "wechat_mp"):
        assert any(info.group == group for info in registry().values()), group


def test_module_groups_reject_cross_field_mistakes(tmp_path):
    cases = [
        ({"identity": {"refresh_idle_days": 20, "refresh_absolute_days": 10}}, "refresh_idle_days"),
        ({"admin_rbac": {"session_idle_seconds": 7200, "session_absolute_seconds": 3600}}, "session_idle_seconds"),
        ({"member_card": {"reminder_days": [1, 3, 7]}}, "descending"),
        ({"notification": {"sms_per_minute": 20, "sms_per_day": 10}}, "sms_per_minute"),
        ({"lottery": {"prize_value_cap_cents": 6_000_000}}, "less than or equal"),
    ]
    for index, (values, reason) in enumerate(cases):
        with pytest.raises(ConfigInvalid) as exc:
            make_config_store(tmp_path / str(index), values=values)
        assert any(reason in e.reason for e in exc.value.errors), (values, exc.value.errors)
