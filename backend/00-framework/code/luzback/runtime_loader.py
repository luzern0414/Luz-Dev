"""运行配置的加载、校验、热加载与读取（references/config-logging.md §2，CONF-001~003、OPS-005）。

    store = config_store()                          # 进程内唯一；目录与环境来自 LUZ_CONFIG_DIR、LUZ_ENV
    store.load()                                    # 启动：失败抛 ConfigInvalid，入口逐条打印后非零退出
    cfg = current_config()                          # 读：请求 / 任务开始时取的快照；不在请求里时取最新快照
    cfg = config_for(store_id=12, user_ref="42")    # 带门店、渠道、客户端版本、用户分桶的覆盖规则
    store.reload("file_change")                     # 热加载：失败保留旧快照、写 config.reload_rejected、计数并告警

- 文件：`<目录>/base.toml` + `<目录>/<LUZ_ENV>.toml`；表按键深度合并，标量与数组整体替换；只放纯数据。
- 校验：先 TOML 语法与类型，再范围、跨字段与覆盖规则（键已登记、维度在该键的 scope 内、套用后仍合法）；
  未登记的键一律报错；错误逐条给出“键、当前值（秘密打码）、原因、允许范围”。
- 版本：合并、校验后的规范化 JSON（键排序）取 SHA-256 前 12 位；注释、空白与键顺序不影响版本。
  热加载时不能热加载的键保留启动时的值，版本按实际生效的内容计算，和刚重启的实例不同，直到重启。
- 原子：在旁路整份校验成不可变快照，通过后一次性替换引用；加载串行加锁，读取不加锁。
- 监听：ConfigWatcher 监听整个目录（watchfiles，不可用时轮询），防抖 1.5 秒（1–2 秒内）；另有内部重载接口与 SIGHUP。
- 每次加载的结果交给监听者：日志级别随之调整；23-app-settings 写 config_loads。

检查文件（CI 与发布脚本）：`python -m luzback.config_check --env all`。
"""
from __future__ import annotations

import copy
import hashlib
import json
import logging
import os
import re
import socket
import sys
import threading
import tomllib
from collections import deque
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from . import log_events as ev
from .logging import (
    config_version_var, configure_logging, flush_logging, is_sensitive_key, log_event, set_levels, set_static,
)
from .runtime_config import Override, RuntimeConfig, RuntimeFile, group_keys, registry, version_tuple

log = logging.getLogger("luzback.config")

ENVIRONMENTS = ("dev", "staging", "prod")
DEFAULT_DIR = Path(__file__).resolve().parents[3] / "config" / "runtime"
COMMIT_FILE = "COMMIT"
TRIGGERS = ("startup", "file_change", "reload_api", "signal")
_MASK = "***"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ---------- 错误 ----------

@dataclass(frozen=True)
class ConfigError:
    key: str
    value: Any
    reason: str
    allowed: str

    def line(self) -> str:
        return f"{self.key} = {_show(self.value)}: {self.reason} (allowed: {self.allowed})"

    def as_dict(self) -> dict[str, Any]:
        return {"key": self.key, "value": _show(self.value), "reason": self.reason, "allowed": self.allowed}


class ConfigInvalid(ValueError):
    """配置不合法（ValueError 的子类）：errors 是逐条的键、当前值、原因、允许范围。"""

    def __init__(self, errors: list[ConfigError]):
        self.errors = list(errors)
        super().__init__("; ".join(e.line() for e in self.errors[:5]))


def _show(value: Any) -> str:
    text = json.dumps(value, ensure_ascii=False, default=str) if not isinstance(value, str) or value == _MASK else repr(value)
    return text if len(text) <= 120 else text[:117] + "..."


def _masked(key: str, value: Any) -> Any:
    last = key.rsplit(".", 1)[-1]
    return _MASK if is_sensitive_key(last) else value


def _allowed_for(path: str) -> str:
    info = registry().get(path)
    if info is not None:
        return info.allowed
    group, _, _name = path.partition(".")
    if path in RuntimeConfig.model_fields:
        return "consistent values within [" + path + "] (see the reason)"
    if group in RuntimeConfig.model_fields and "." in path:
        return "registered keys in [" + group + "]: " + ", ".join(group_keys(group))
    groups = list(RuntimeConfig.model_fields) + ["overrides"]
    return "registered tables: " + ", ".join(groups)


def _loc(parts: tuple[Any, ...]) -> str:
    return ".".join(str(p) for p in parts if not (isinstance(p, str) and p.startswith("function-")))


def _from_validation(exc: ValidationError, prefix: str = "") -> list[ConfigError]:
    errors: list[ConfigError] = []
    for item in exc.errors(include_url=False):
        key = _loc(tuple(item.get("loc", ())))
        key = f"{prefix}{key}" if key else prefix.rstrip(".") or "(file)"
        kind = item.get("type", "")
        if kind == "extra_forbidden":
            reason = "unknown key (a misspelt key would silently fall back to the default)"
        else:
            reason = str(item.get("msg", "invalid"))
            if reason.startswith("Value error, "):
                reason = reason[len("Value error, "):]
        allowed = _allowed_for(key) if kind != "model_type" else "TOML table"
        errors.append(ConfigError(key, _masked(key, item.get("input")), reason, allowed))
    return errors


# ---------- 读取与合并 ----------

def deep_merge(base: dict[str, Any], top: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for key, value in top.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def _read_toml(path: Path) -> dict[str, Any]:
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ConfigInvalid([ConfigError(path.name, None, "file not found", f"file in {path.parent}")]) from None
    except tomllib.TOMLDecodeError as exc:
        raise ConfigInvalid([ConfigError(path.name, None, f"TOML syntax error: {exc}", "TOML 1.0")]) from None
    except (OSError, UnicodeDecodeError) as exc:
        raise ConfigInvalid([ConfigError(path.name, None, f"cannot read file: {type(exc).__name__}", "UTF-8 TOML file")]) from None


def read_files(config_dir: Path, env: str) -> tuple[dict[str, Any], tuple[str, ...]]:
    """base.toml + <env>.toml 合并后的原始数据与参与的文件名。读不全、语法错都按校验失败处理。"""
    if env not in ENVIRONMENTS:
        raise ConfigInvalid([ConfigError("LUZ_ENV", env, "unknown environment", " | ".join(ENVIRONMENTS))])
    base = _read_toml(config_dir / "base.toml")
    top = _read_toml(config_dir / f"{env}.toml")
    return deep_merge(base, top), ("base.toml", f"{env}.toml")


def read_source_commit(config_dir: Path) -> str | None:
    """发布脚本同步配置时写入的 COMMIT 文件（配置文件所在的代码提交）。"""
    try:
        text = (config_dir / COMMIT_FILE).read_text(encoding="utf-8").strip().split()[0]
    except (OSError, IndexError):
        return None
    return text[:40] if re.fullmatch(r"[0-9a-fA-F]{7,40}", text) else None


def _flatten(dump: dict[str, Any]) -> dict[str, Any]:
    flat: dict[str, Any] = {}
    for group, values in dump.items():
        if group == "overrides":
            for name, rule in (values or {}).items():
                flat[f"overrides.{name}"] = rule
        elif isinstance(values, dict):
            for name, value in values.items():
                flat[f"{group}.{name}"] = value
    return flat


def _digest(dump: dict[str, Any]) -> str:
    canonical = json.dumps(dump, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:12]


def _apply_values(dump: dict[str, Any], values: dict[str, Any]) -> dict[str, Any]:
    out = copy.deepcopy(dump)
    for path, value in values.items():
        group, _, name = path.partition(".")
        out[group][name] = value
    return out


# ---------- 快照 ----------

def user_bucket(rule_name: str, user_ref: str | int) -> int:
    """0–99；同一用户在同一条规则里总落在同一桶，提高百分比时已命中的用户保持命中。"""
    digest = hashlib.sha256(f"{rule_name}:{user_ref}".encode()).hexdigest()
    return int(digest[:8], 16) % 100


def override_matches(name: str, rule: Override, *, store_id: int | None = None, channel: str | None = None,
                     client_version: str | None = None, user_ref: str | int | None = None) -> bool:
    if rule.stores and (store_id is None or int(store_id) not in rule.stores):
        return False
    if rule.channels and channel not in rule.channels:
        return False
    if rule.client_version_min or rule.client_version_max:
        current = version_tuple(client_version)
        if current is None:
            return False
        if rule.client_version_min and current < version_tuple(rule.client_version_min):
            return False
        if rule.client_version_max and current > version_tuple(rule.client_version_max):
            return False
    if rule.percent is not None:
        if user_ref in (None, ""):
            return False
        if user_bucket(name, user_ref) >= rule.percent:
            return False
    return True


@dataclass(frozen=True, eq=False)
class ConfigSnapshot:
    """一次加载的结果：不可变；请求开始时取一次，全程用这一份。"""

    config: RuntimeConfig
    overrides: tuple[tuple[str, Override], ...]
    version: str
    env: str
    loaded_at: datetime
    source_commit: str | None = None
    files: tuple[str, ...] = ()
    restart_pending: tuple[str, ...] = ()
    _resolved: dict[tuple[str, ...], RuntimeConfig] = field(default_factory=dict, repr=False)

    def matched(self, **context: Any) -> tuple[str, ...]:
        return tuple(name for name, rule in self.overrides if override_matches(name, rule, **context))

    def resolve(self, *, store_id: int | None = None, channel: str | None = None, client_version: str | None = None,
                user_ref: str | int | None = None) -> RuntimeConfig:
        """套用命中的覆盖规则（按文件里的先后顺序，后者覆盖前者）；结果按命中组合缓存。"""
        names = self.matched(store_id=store_id, channel=channel, client_version=client_version, user_ref=user_ref)
        if not names:
            return self.config
        cached = self._resolved.get(names)
        if cached is not None:
            return cached
        rules = dict(self.overrides)
        dump = self.config.model_dump()
        for name in names:
            dump = _apply_values(dump, rules[name].values)
        try:
            resolved = RuntimeConfig.model_validate(dump)
        except ValidationError:  # 单条规则加载时都已校验；组合后不合法时退回基础值并记录
            log_event(log, ev.CONFIG_RELOAD_REJECTED, logging.ERROR, reason="override_combination_invalid",
                      overrides=list(names))
            resolved = self.config
        self._resolved[names] = resolved
        return resolved

    def describe(self) -> dict[str, Any]:
        return {"config_version": self.version, "env": self.env, "loaded_at": self.loaded_at.isoformat(),
                "source_commit": self.source_commit, "files": list(self.files),
                "restart_pending": list(self.restart_pending), "overrides": [name for name, _ in self.overrides]}


def build_snapshot(data: dict[str, Any], *, env: str, source_commit: str | None = None, files: tuple[str, ...] = (),
                   keep: RuntimeConfig | None = None) -> tuple[ConfigSnapshot, dict[str, Any]]:
    """校验合并后的数据，返回 (快照, 文件里写的期望值摊平)。keep：热加载时保留其中不能热加载的键的当前值。"""
    try:
        parsed = RuntimeFile.model_validate(data)
    except ValidationError as exc:
        raise ConfigInvalid(_from_validation(exc)) from None
    errors: list[ConfigError] = []
    keys = registry()
    base = RuntimeConfig.model_validate(parsed.model_dump(exclude={"overrides"}))
    desired = _flatten(parsed.model_dump(mode="json", by_alias=True))
    restart: list[str] = []
    if keep is not None:
        kept = keep.model_dump()
        dump = base.model_dump()
        for path, info in keys.items():
            if not info.reloadable and dump[info.group][info.name] != kept[info.group][info.name]:
                dump[info.group][info.name] = kept[info.group][info.name]
                restart.append(path)
        base = RuntimeConfig.model_validate(dump)
    overrides: list[tuple[str, Override]] = []
    for name, rule in parsed.overrides.items():
        prefix = f"overrides.{name}.set."
        bad = False
        for path in rule.values:
            info = keys.get(path)
            if info is None:
                errors.append(ConfigError(prefix + path, _masked(path, rule.values[path]), "unknown key", _allowed_for(path)))
                bad = True
                continue
            missing = rule.dimensions() - set(info.scope)
            if missing:
                errors.append(ConfigError(prefix + path, rule.values[path],
                                          f"cannot be overridden by {', '.join(sorted(missing))}",
                                          "override scopes: " + ", ".join(info.scope)))
                bad = True
        if bad:
            continue
        try:
            RuntimeConfig.model_validate(_apply_values(base.model_dump(), rule.values))
        except ValidationError as exc:
            errors.extend(_from_validation(exc, prefix=prefix))
            continue
        overrides.append((name, rule))
    if not errors and overrides:
        dump = base.model_dump()
        for _name, rule in overrides:
            dump = _apply_values(dump, rule.values)
        try:  # 全部规则同时命中时也要合法（跨字段约束）
            RuntimeConfig.model_validate(dump)
        except ValidationError as exc:
            errors.extend(_from_validation(exc, prefix="overrides(all)."))
    if errors:
        raise ConfigInvalid(errors)
    effective = base.model_dump(mode="json")
    effective["overrides"] = {name: rule.model_dump(mode="json", by_alias=True) for name, rule in overrides}
    snapshot = ConfigSnapshot(config=base, overrides=tuple(overrides), version=_digest(effective), env=env,
                              loaded_at=_utcnow(), source_commit=source_commit, files=files,
                              restart_pending=tuple(restart))
    return snapshot, desired


@dataclass(frozen=True)
class LoadResult:
    trigger: str
    result: str                            # loaded | unchanged | rejected
    version: str | None                    # 加载后生效的版本（rejected 时为仍在用的旧版本）
    previous_version: str | None
    instance: str
    at: datetime
    source_commit: str | None = None
    changed_keys: tuple[str, ...] = ()
    restart_keys: tuple[str, ...] = ()
    errors: tuple[ConfigError, ...] = ()

    def error_summary(self) -> str | None:
        if not self.errors:
            return None
        return "; ".join(e.line() for e in self.errors)[:2000]

    def as_dict(self) -> dict[str, Any]:
        return {"trigger": self.trigger, "result": self.result, "config_version": self.version,
                "previous_version": self.previous_version, "instance": self.instance, "at": self.at.isoformat(),
                "source_commit": self.source_commit, "changed_keys": list(self.changed_keys),
                "restart_keys": list(self.restart_keys), "errors": [e.as_dict() for e in self.errors]}


Listener = Callable[[LoadResult, "ConfigSnapshot | None"], None]


class ConfigStore:
    """进程内的配置快照持有者：load / reload 串行（锁），current 无锁读引用。"""

    def __init__(self, config_dir: str | Path | None = None, env: str | None = None, *, instance: str | None = None,
                 history: int = 50):
        self.config_dir = Path(config_dir) if config_dir else DEFAULT_DIR
        self.env = env or "dev"
        self.instance = (instance or socket.gethostname() or "unknown")[:64]
        self._lock = threading.Lock()
        self._snapshot: ConfigSnapshot | None = None
        self._desired: dict[str, Any] = {}
        self._listeners: list[Listener] = []
        self.history: deque[LoadResult] = deque(maxlen=history)
        self.failures_total = 0
        self.last_failure: LoadResult | None = None

    # ---- 读 ----
    @property
    def loaded(self) -> bool:
        return self._snapshot is not None

    def current(self) -> ConfigSnapshot:
        snapshot = self._snapshot
        if snapshot is None:  # 第一次读取时加载（测试、脚本）；服务入口在启动时显式 load()
            self.load()
            snapshot = self._snapshot
        assert snapshot is not None
        return snapshot

    # ---- 加载 ----
    def load(self, trigger: str = "startup") -> LoadResult:
        """启动加载：失败抛 ConfigInvalid（已有快照时退化为 reload 语义）。"""
        return self._load(trigger, strict=True)

    def reload(self, trigger: str = "file_change") -> LoadResult:
        """热加载：失败不抛，保留旧快照。"""
        return self._load(trigger, strict=False)

    def _load(self, trigger: str, strict: bool) -> LoadResult:
        if trigger not in TRIGGERS:
            raise ValueError(f"unknown trigger {trigger}")
        with self._lock:
            previous = self._snapshot
            commit = read_source_commit(self.config_dir)
            try:
                data, files = read_files(self.config_dir, self.env)
                snapshot, desired = build_snapshot(data, env=self.env, source_commit=commit, files=files,
                                                   keep=previous.config if previous else None)
            except ConfigInvalid as exc:
                result = LoadResult(trigger, "rejected", previous.version if previous else None,
                                    previous.version if previous else None, self.instance, _utcnow(), commit,
                                    errors=tuple(exc.errors))
                self.failures_total += 1
                self.last_failure = result
                self.history.append(result)
                self._log(result)
                failure: ConfigInvalid | None = exc
            else:
                failure = None
                if previous is None:
                    result = LoadResult(trigger, "loaded", snapshot.version, None, self.instance, snapshot.loaded_at,
                                        commit)
                else:
                    before = _flatten(previous.config.model_dump(mode="json")
                                      | {"overrides": {n: r.model_dump(mode="json", by_alias=True) for n, r in previous.overrides}})
                    after = _flatten(snapshot.config.model_dump(mode="json")
                                     | {"overrides": {n: r.model_dump(mode="json", by_alias=True) for n, r in snapshot.overrides}})
                    changed = tuple(sorted(k for k in set(before) | set(after) if before.get(k) != after.get(k)))
                    state = "unchanged" if snapshot.version == previous.version else "loaded"
                    result = LoadResult(trigger, state, snapshot.version, previous.version, self.instance,
                                        snapshot.loaded_at, commit, changed_keys=changed,
                                        restart_keys=snapshot.restart_pending)
                self._snapshot = snapshot  # 一次性替换引用；进行中的请求仍持有旧快照
                self._desired = desired
                self.history.append(result)
                set_static(config_version=snapshot.version)
                self._log(result)
        self._notify(result)
        if failure is not None and (strict or previous is None):
            raise failure
        return result

    def _log(self, result: LoadResult) -> None:
        # 日志上下文里的 config_version 是发起加载的请求所用的版本；这次加载的结果写在 loaded_version
        fields = {"loaded_version": result.version, "previous_version": result.previous_version,
                  "trigger": result.trigger, "source_commit": result.source_commit}
        if result.result == "rejected":
            event, level = (ev.CONFIG_STARTUP_REJECTED, logging.CRITICAL) if result.previous_version is None \
                else (ev.CONFIG_RELOAD_REJECTED, logging.ERROR)
            log_event(log, event, level, outcome="failure", reason="config_invalid",
                      errors=[e.as_dict() for e in result.errors[:20]], **fields)
            return
        if result.restart_keys:
            log_event(log, ev.CONFIG_RESTART_REQUIRED, logging.WARNING, restart_keys=list(result.restart_keys), **fields)
        if result.result == "unchanged":
            log_event(log, ev.CONFIG_UNCHANGED, **fields)
        elif result.previous_version is None:
            log_event(log, ev.CONFIG_LOADED, outcome="success", **fields)
        else:
            log_event(log, ev.CONFIG_RELOADED, outcome="success", changed_keys=list(result.changed_keys), **fields)

    # ---- 监听者 ----
    def add_listener(self, fn: Listener, replay: bool = True) -> None:
        """登记加载结果的监听者；replay=True 时先把已有的加载结果按顺序交给它（启动时 DB 尚未就绪的记录不丢）。"""
        with self._lock:
            if fn in self._listeners:
                return
            self._listeners.append(fn)
            past = list(self.history) if replay else []
            snapshot = self._snapshot
        for result in past:
            self._call(fn, result, snapshot)

    def remove_listener(self, fn: Listener) -> None:
        with self._lock:
            if fn in self._listeners:
                self._listeners.remove(fn)

    def _notify(self, result: LoadResult) -> None:
        with self._lock:
            listeners = list(self._listeners)
            snapshot = self._snapshot
        for fn in listeners:
            self._call(fn, result, snapshot)

    @staticmethod
    def _call(fn: Listener, result: LoadResult, snapshot: ConfigSnapshot | None) -> None:
        try:
            fn(result, snapshot)
        except Exception as exc:  # noqa: BLE001 - 监听者失败不影响加载结果
            log_event(log, ev.CONFIG_LISTENER_FAILED, logging.ERROR, listener=getattr(fn, "__qualname__", repr(fn)),
                      **{"error.type": type(exc).__name__})

    # ---- 观察 ----
    def status(self) -> dict[str, Any]:
        """/internal/config：版本、加载时间、来源提交、待重启的键、最近一次失败；不含任何值。"""
        snapshot = self._snapshot
        failure = self.last_failure
        return {
            "instance": self.instance, "env": self.env,
            **(snapshot.describe() if snapshot else {"config_version": None}),
            "reload_failures_total": self.failures_total,
            "last_failure": failure.as_dict() if failure else None,
            "file_matches_running": self._file_matches_running(),
        }

    def _file_matches_running(self) -> bool | None:
        snapshot = self._snapshot
        if snapshot is None:
            return None
        return not snapshot.restart_pending

    def metrics_lines(self) -> list[str]:
        snapshot = self._snapshot
        lines = ["# TYPE luz_config_reload_failures_total counter",
                 f"luz_config_reload_failures_total {self.failures_total}"]
        if snapshot is not None:
            lines += ["# TYPE luz_config_info gauge",
                      f'luz_config_info{{version="{snapshot.version}",env="{snapshot.env}"}} 1',
                      "# TYPE luz_config_restart_pending gauge",
                      f"luz_config_restart_pending {len(snapshot.restart_pending)}"]
        return lines


# ---------- 进程内单例与请求上下文 ----------

_store: ConfigStore | None = None
_store_lock = threading.Lock()
_snapshot_var: ContextVar[ConfigSnapshot | None] = ContextVar("luz_config_snapshot", default=None)
_client_var: ContextVar[tuple[str | None, str | None]] = ContextVar("luz_config_client", default=(None, None))


def _apply_logging(result: LoadResult, snapshot: ConfigSnapshot | None) -> None:
    if snapshot is not None and result.result != "rejected":
        set_levels(snapshot.config.logging.level, snapshot.config.logging.modules)


def config_store() -> ConfigStore:
    global _store
    if _store is None:
        with _store_lock:
            if _store is None:
                from .config import get_settings  # noqa: PLC0415

                settings = get_settings()
                store = ConfigStore(settings.config_dir or None, settings.env, instance=settings.instance_id)
                store.add_listener(_apply_logging, replay=False)
                _store = store
    return _store


def set_config_store(store: ConfigStore | None) -> ConfigStore | None:
    """替换进程内的配置（测试、脚本）；返回原来的，便于还原。"""
    global _store
    with _store_lock:
        previous, _store = _store, store
    if store is not None:
        store.add_listener(_apply_logging, replay=False)
    return previous


def current_snapshot() -> ConfigSnapshot:
    snapshot = _snapshot_var.get()
    return snapshot if snapshot is not None else config_store().current()


def current_config() -> RuntimeConfig:
    """当前请求 / 任务的运行参数（全局值，不含覆盖规则）。"""
    return current_snapshot().config


def config_for(*, store_id: int | None = None, user_ref: str | int | None = None, channel: str | None = None,
               client_version: str | None = None) -> RuntimeConfig:
    """带覆盖规则的运行参数；渠道与客户端版本默认取请求头（X-Client、X-Client-Version）。"""
    req_channel, req_version = _client_var.get()
    return current_snapshot().resolve(store_id=store_id, user_ref=user_ref, channel=channel or req_channel,
                                      client_version=client_version or req_version)


@contextmanager
def use_snapshot(snapshot: ConfigSnapshot | None = None, *, channel: str | None = None,
                 client_version: str | None = None) -> Iterator[ConfigSnapshot]:
    """请求、任务或智能体运行开始时取一次快照，全程用这一份（结算、支付与长任务不受中途热加载影响）。"""
    snapshot = snapshot or config_store().current()
    tokens = (_snapshot_var.set(snapshot), _client_var.set((channel, client_version)),
              config_version_var.set(snapshot.version))
    try:
        yield snapshot
    finally:
        config_version_var.reset(tokens[2])
        _client_var.reset(tokens[1])
        _snapshot_var.reset(tokens[0])


# ---------- 监听配置目录 ----------

def _watched(path: str) -> bool:
    name = os.path.basename(path)
    return name.endswith(".toml") or name == COMMIT_FILE or name.startswith("..")  # ..data：Kubernetes 挂载的原子切换


def directory_fingerprint(config_dir: Path) -> tuple:
    items = []
    try:
        for entry in sorted(os.scandir(config_dir), key=lambda e: e.name):
            if _watched(entry.path):
                try:
                    stat = entry.stat()
                except OSError:
                    continue
                items.append((entry.name, stat.st_size, stat.st_mtime_ns))
    except OSError:
        return ("missing",)
    return tuple(items)


class ConfigWatcher:
    """监听整个配置目录（不是单个文件：编辑器与同步脚本常先写临时文件再改名），变化后防抖再热加载。

    mode：auto（watchfiles，不可用时轮询）、poll（容器文件系统不发通知时）、off。
    debounce_ms：目录安静这么久才加载（默认 1.5 秒）；持续有变化时最多等 2 秒（watchfiles 的 debounce）。
    """

    def __init__(self, store: ConfigStore, *, mode: str = "auto", debounce_ms: int = 1500, poll_s: float = 2.0):
        if mode not in ("auto", "poll", "off"):
            raise ValueError("LUZ_CONFIG_WATCH must be auto, poll or off")
        self.store = store
        self.mode = mode
        self.debounce_ms = debounce_ms
        self.poll_s = poll_s
        self.backend = "off" if mode == "off" else None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self.mode == "off" or (self._thread and self._thread.is_alive()):
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="luz-config-watch", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout)
            self._thread = None

    def _fire(self) -> None:
        self.store.reload("file_change")

    def _run(self) -> None:
        if self.mode == "auto":
            try:
                import watchfiles  # noqa: PLC0415
            except ImportError:
                watchfiles = None
            if watchfiles is not None:
                try:
                    self.backend = "watchfiles"
                    log_event(log, ev.CONFIG_WATCH_STARTED, backend="watchfiles", config_dir=str(self.store.config_dir))
                    for _changes in watchfiles.watch(self.store.config_dir, watch_filter=lambda _c, p: _watched(p),
                                                     step=self.debounce_ms, debounce=max(2000, self.debounce_ms),
                                                     stop_event=self._stop, recursive=False, rust_timeout=1000,
                                                     yield_on_timeout=False):
                        self._fire()
                    return
                except Exception as exc:  # noqa: BLE001 - 通知不可用（inotify 上限、网络盘）时改为轮询
                    log_event(log, ev.CONFIG_WATCH_FAILED, logging.WARNING, backend="watchfiles", fallback="poll",
                              **{"error.type": type(exc).__name__})
        self._poll()

    def _poll(self) -> None:
        self.backend = "poll"
        log_event(log, ev.CONFIG_WATCH_STARTED, backend="poll", config_dir=str(self.store.config_dir), poll_s=self.poll_s)
        last = directory_fingerprint(self.store.config_dir)
        while not self._stop.wait(self.poll_s):
            current = directory_fingerprint(self.store.config_dir)
            if current == last:
                continue
            while not self._stop.wait(self.debounce_ms / 1000):  # 防抖：目录在一个间隔内不再变化才加载
                settled = directory_fingerprint(self.store.config_dir)
                if settled == current:
                    break
                current = settled
            if self._stop.is_set():
                return
            last = current
            self._fire()


# ---------- 进程启动（api、worker、scheduler 共用）----------

def start_process(settings: Any = None) -> ConfigStore:
    """日志 → 运行配置（失败逐条打印“键、当前值、原因、允许范围”后以 2 退出）→ 按 [logging] 调整 → 扫描环境变量。"""
    from .config import env_warnings, get_settings  # noqa: PLC0415

    settings = settings or get_settings()
    identity = {"service": settings.service_name, "env": settings.env, "version": settings.app_version,
                "instance": settings.instance_id}
    configure_logging("info", **identity)
    store = config_store()
    try:
        store.load("startup")
    except ConfigInvalid as exc:
        flush_logging()
        print(f"runtime config rejected ({store.config_dir}, env={store.env}):", file=sys.stderr)
        for error in exc.errors:
            print(f"  {error.line()}", file=sys.stderr)
        raise SystemExit(2) from None
    cfg = store.current().config
    if cfg.logging.queue_capacity != 10_000:
        configure_logging(cfg.logging.level, queue_capacity=cfg.logging.queue_capacity, **identity)
    set_levels(cfg.logging.level, cfg.logging.modules)
    for name, note in env_warnings():
        log_event(log, ev.CONFIG_UNKNOWN_ENV, logging.WARNING, variable=name, note=note)
    return store


def watcher_for(store: ConfigStore, settings: Any = None) -> ConfigWatcher:
    from .config import get_settings  # noqa: PLC0415

    settings = settings or get_settings()
    return ConfigWatcher(store, mode=settings.config_watch)


# ---------- 命令行：CI 与发布脚本检查配置文件 ----------

def check(config_dir: Path, envs: list[str]) -> int:
    status = 0
    for env in envs:
        try:
            data, _files = read_files(config_dir, env)
            snapshot, _ = build_snapshot(data, env=env, source_commit=read_source_commit(config_dir))
        except ConfigInvalid as exc:
            status = 1
            print(f"{env}: INVALID", file=sys.stderr)
            for error in exc.errors:
                print(f"  {error.line()}", file=sys.stderr)
            continue
        print(f"{env}: ok version={snapshot.version} overrides={len(snapshot.overrides)}")
    base = _read_toml(config_dir / "base.toml") if (config_dir / "base.toml").exists() else {}
    missing = [path for path in registry() if path.split(".", 1)[1] not in (base.get(path.split(".", 1)[0]) or {})]
    if missing:  # base.toml 写全部键与默认值，字段说明与它不一致直接失败
        status = 1
        print("base.toml is missing keys: " + ", ".join(missing), file=sys.stderr)
    return status
