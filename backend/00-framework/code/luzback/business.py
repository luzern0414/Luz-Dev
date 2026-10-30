"""经营设置：商户日常自己调整的经营规则（退款手续费、储值扣减顺序、退券策略、补卡费……），在后台维护、保存即生效。

和运行参数分开（references/config-logging.md §1）：运行参数只在排障与调优时由开发改配置文件；经营设置由运营在后台“经营设置”页改，
每次修改校验、写审计。业务模块在导入时登记自己的键，读取时只用 `business_value(db, key, store_id)`：

    declare_business_settings(BusinessSetting("wallet.customer_refund_fee_bp", "顾客原因退款手续费", "wallet", "int", 0,
                                              unit="万分比", minimum=0, maximum=10_000, risk="high"))
    fee_bp = business_value(db, "wallet.customer_refund_fee_bp")

值存在数据库（23-app-settings 的 business_settings：全局一行，允许按门店覆盖的键每家门店可再有一行），由 app_settings 在导入时
`set_business_store(...)` 接上；没有接上（单模块测试）或没有行时用登记的默认值。业务模块不依赖 app_settings。
测试用 `luzback.testing.override_business(...)` 覆盖，backend/conftest.py 每个测试结束后撤销。
"""
from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

from sqlalchemy.orm import Session

Kind = Literal["bool", "int", "choice"]
Risk = Literal["low", "high"]
# (db, key, store_id) -> (有值, 值)：先门店行后全局行；都没有时 (False, None)
BusinessStore = Callable[[Session, str, "int | None"], "tuple[bool, Any]"]


@dataclass(frozen=True)
class BusinessSetting:
    key: str                                  # 模块.名称，如 wallet.split_policy
    label: str                                # 给运营看的名称
    owner: str                                # 所属模块（后台按它分组）
    kind: Kind
    default: Any
    description: str = ""                     # 影响与举例，后台在输入框旁显示
    unit: str = ""                            # 分、万分比、小时……
    minimum: int | None = None                # int
    maximum: int | None = None                # int
    choices: tuple[tuple[str, str], ...] = ()  # choice：(值, 中文名)
    store_scope: bool = False                 # 能否按门店覆盖
    risk: Risk = "low"                        # high：改动影响资金，后台要求重新验证身份并二次确认
    admin_visible: bool = True
    sort_order: int = 0

    def validate(self, value: Any) -> Any:
        """返回规范化后的值；不合法抛 ValueError（写明允许范围）。默认值在登记时也走这里。"""
        if self.kind == "bool":
            if not isinstance(value, bool):
                raise ValueError("must be true or false")
            return value
        if self.kind == "int":
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValueError("must be an integer")
            if (self.minimum is not None and value < self.minimum) or (self.maximum is not None and value > self.maximum):
                raise ValueError(f"must be between {self.minimum} and {self.maximum}")
            return value
        if self.kind == "choice":
            allowed = [c for c, _ in self.choices]
            if value not in allowed:
                raise ValueError("must be one of: " + ", ".join(allowed))
            return value
        raise ValueError(f"unknown kind {self.kind}")

    def describe(self) -> dict[str, Any]:
        out: dict[str, Any] = {"key": self.key, "label": self.label, "owner": self.owner, "kind": self.kind,
                               "default": self.default, "description": self.description, "unit": self.unit,
                               "store_scope": self.store_scope, "risk": self.risk}
        if self.kind == "int":
            out.update(minimum=self.minimum, maximum=self.maximum)
        if self.kind == "choice":
            out["choices"] = [{"value": c, "label": name} for c, name in self.choices]
        return out


_lock = threading.Lock()
REGISTRY: dict[str, BusinessSetting] = {}
_store: BusinessStore | None = None
_overrides: dict[str, Any] = {}


def declare_business_settings(*items: BusinessSetting) -> None:
    for item in items:
        item.validate(item.default)
        if len(item.key) > 64 or "." not in item.key:
            raise ValueError(f"business setting key must be 'module.name' within 64 chars: {item.key}")
        with _lock:
            REGISTRY[item.key] = item


def get_business_setting(key: str) -> BusinessSetting | None:
    return REGISTRY.get(key)


def business_settings() -> list[BusinessSetting]:
    with _lock:
        return sorted(REGISTRY.values(), key=lambda s: (s.owner, s.sort_order, s.key))


def set_business_store(fn: BusinessStore | None) -> None:
    global _store
    _store = fn


def business_value(db: Session | None, key: str, store_id: int | None = None) -> Any:
    """当前生效值：测试覆盖 → 门店值（允许门店覆盖时）→ 全局值 → 登记的默认值。未登记的键是代码错误。"""
    item = REGISTRY.get(key)
    if item is None:
        raise KeyError(f"business setting not declared: {key}")
    if key in _overrides:
        return _overrides[key]
    if _store is not None and db is not None:
        found, value = _store(db, key, store_id if item.store_scope else None)
        if found:
            try:
                return item.validate(value)
            except (TypeError, ValueError):  # 库里的值被改坏：用默认值，不让业务失败（后台保存时已校验）
                return item.default
    return item.default


def _set_overrides(values: dict[str, Any]) -> None:
    for key, value in values.items():
        item = REGISTRY.get(key)
        if item is None:
            raise KeyError(f"business setting not declared: {key}")
        _overrides[key] = item.validate(value)


def _clear_overrides() -> None:
    _overrides.clear()
