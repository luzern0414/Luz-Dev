"""模块配置的统一读法（references/config-logging.md §1、§2）：

    模块配置对象 = 部署参数与秘密（环境变量）+ 扩展钩子（代码）+ 运行参数（运行配置里该模块的分组，同名属性）

- 运行参数只有一个来源：`config/runtime/*.toml` 的 `[<模块>]` 表，字段说明在 `luzback/runtime_config.py`。
  每次 `get()` 都从当前快照取（请求、任务、事件投递内是同一份），热加载后下一次读取就是新值；按快照缓存，不重复构造。
- 部署参数与秘密仍从环境变量读：模块数据类里这些字段带环境变量默认值。运行参数字段的默认值取字段说明的默认值
  （`runtime_default`），只为直接构造数据类时可用，读取时总被快照里的值覆盖。
- `shared`：几个模块共用的键（储值与会员卡的 `[prepaid]`），属性名 → "分组.键"。
- `set(...)`：项目启动或测试时替换部署参数与钩子；运行参数只能在测试里替换（转给 `luzback.testing.override_runtime`，
  用 make_config_store 生成一份测试配置并设为进程内配置），生产环境调用会报错——运行参数没有第二个来源。

    _module = ModuleConfig("order", OrderConfig)
    def get_config() -> OrderConfig: return _module.get()
"""
from __future__ import annotations

import dataclasses
import threading
from collections.abc import Callable, Mapping
from typing import Any, Generic, TypeVar

from .runtime_config import RuntimeConfig
from .runtime_loader import current_config

T = TypeVar("T")


def runtime_default(group: str, key: str) -> Callable[[], Any]:
    """数据类字段的 default_factory：取字段说明里该键的默认值。"""
    def factory() -> Any:
        model = RuntimeConfig.model_fields[group].annotation
        return model.model_fields[key].default

    return factory


class ModuleConfig(Generic[T]):
    def __init__(self, group: str, factory: Callable[[], T], *, shared: Mapping[str, str] | None = None):
        if group not in RuntimeConfig.model_fields:
            raise KeyError(f"no runtime config group [{group}]")
        self.group = group
        self.factory = factory
        self.shared = dict(shared or {})
        self._base: T | None = None
        self._cache: tuple[Any, Any, T] | None = None
        self._lock = threading.Lock()

    # ---- 字段 ----
    def runtime_fields(self) -> dict[str, str]:
        """属性名 → "分组.键"：本模块分组的全部键 + 共用的键。"""
        model = RuntimeConfig.model_fields[self.group].annotation
        fields = {name: f"{self.group}.{name}" for name in model.model_fields}
        fields.update(self.shared)
        return fields

    def _values(self, cfg: RuntimeConfig) -> dict[str, Any]:
        values: dict[str, Any] = {}
        for attr, path in self.runtime_fields().items():
            group, _, key = path.partition(".")
            values[attr] = getattr(getattr(cfg, group), key)
        return values

    def _base_config(self) -> T:
        base = self._base
        if base is None:
            with self._lock:
                if self._base is None:
                    self._base = self.factory()
                base = self._base
        return base

    # ---- 读 ----
    def get(self) -> T:
        base = self._base_config()
        cfg = current_config()
        cached = self._cache
        if cached is not None and cached[0] is base and cached[1] is cfg:
            return cached[2]
        merged = dataclasses.replace(base, **self._values(cfg))
        self._cache = (base, cfg, merged)
        return merged

    # ---- 替换（项目启动或测试）----
    def set(self, config: T | None = None, **overrides: Any) -> T:
        """config：整份替换部署参数与钩子（其中的运行参数按测试覆盖处理）；overrides：逐项替换。"""
        runtime = self.runtime_fields()
        runtime_values: dict[str, Any] = {}
        if config is not None:
            base = config
            runtime_values.update({attr: getattr(config, attr) for attr in runtime})
        else:
            base = self._base_config()
        plain = {k: v for k, v in overrides.items() if k not in runtime}
        runtime_values.update({k: v for k, v in overrides.items() if k in runtime})
        if plain:
            base = dataclasses.replace(base, **plain)
        if runtime_values:
            from .testing import override_runtime  # noqa: PLC0415 - 只给测试用

            grouped: dict[str, dict[str, Any]] = {}
            for attr, value in runtime_values.items():
                group, _, key = runtime[attr].partition(".")
                grouped.setdefault(group, {})[key] = list(value) if isinstance(value, tuple) else value
            override_runtime(grouped)
        self._base = base
        self._cache = None
        return self.get()

    def reload(self) -> T:
        """部署参数重新从环境变量读（改了环境变量要重启进程；这里给测试与命令行用）；运行参数不受影响。"""
        self._base = None
        self._cache = None
        return self.get()

    def reset(self) -> None:
        """部署参数重新从环境变量读，并撤销本模块分组（及共用分组）的测试覆盖。"""
        from .testing import reset_runtime_overrides  # noqa: PLC0415

        self._base = None
        self._cache = None
        groups = {self.group} | {path.partition(".")[0] for path in self.shared.values()}
        reset_runtime_overrides(*groups)
