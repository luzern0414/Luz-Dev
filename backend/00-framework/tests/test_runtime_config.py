"""运行配置：字段说明与 base.toml 一致、三个环境文件可加载、启动校验（坏类型 / 超范围 / 拼错键 / 跨字段）、版本摘要、
热加载（坏文件被拒且旧值保留、新请求用新值、进行中的请求用旧值、不可热加载的键待重启）、覆盖规则与分桶、目录监听、环境变量扫描。
"""
from __future__ import annotations

import logging
import re
import sys
import threading
import time
import tomllib
import types
from pathlib import Path

import pytest
from fastapi import APIRouter
from fastapi.testclient import TestClient

from luzback import log_events as ev
from luzback.app import create_app
from luzback.config import Settings, env_warnings
from luzback.config_check import main as config_check
from luzback.runtime_config import RuntimeConfig, registry
from luzback.runtime_loader import (
    DEFAULT_DIR, ENVIRONMENTS, ConfigInvalid, ConfigStore, ConfigWatcher, build_snapshot, config_for, current_config,
    read_files, set_config_store, start_process, use_snapshot, user_bucket,
)
from luzback.testing import dump_toml, make_config_store


def _events(caplog) -> list[str]:
    return [getattr(r, "fields", {}).get("event") for r in caplog.records]


def _write(store: ConfigStore, values: dict, raw: str | None = None) -> None:
    (store.config_dir / f"{store.env}.toml").write_text(raw if raw is not None else dump_toml(values), encoding="utf-8")


# ---------------------------------------------------------------- 字段说明与文件

def test_repository_env_files_load_and_base_lists_every_key(capsys):
    assert config_check(["--env", "all"]) == 0
    out = capsys.readouterr().out
    for env in ENVIRONMENTS:
        assert f"{env}: ok version=" in out
    base = tomllib.loads((DEFAULT_DIR / "base.toml").read_text(encoding="utf-8"))
    in_file = {f"{group}.{key}" for group, table in base.items() for key in table}
    assert in_file == set(registry())  # 缺键、多键都失败
    for info in registry().values():
        assert info.description and info.owner and info.allowed
        default = list(info.default) if isinstance(info.default, tuple) else info.default
        assert base[info.group][info.name] == default, info.path  # base.toml 的默认值与字段说明一致


UNIT_SUFFIXES = {  # 说明括号里第一个词是单位时，键名要带对应的后缀
    "毫秒": ("_ms",), "秒": ("_s", "_seconds"), "分钟": ("_min", "_minutes"), "小时": ("_hours",), "时": ("_hour",),
    "天": ("_days",), "KB": ("_kb",), "MB": ("_mb",), "KiB": ("_kib",), "分": ("_cents",), "万分比": ("_bp",),
    "次/秒": ("_per_s",), "次/分钟": ("_per_min", "_per_minute"),
}


def test_defaults_validate_and_units_are_in_key_names():
    RuntimeConfig()  # validate_default：默认值也要合法
    checked = 0
    for path, info in registry().items():
        if "（" not in info.description:
            continue
        unit = re.split(r"[，；、）]", info.description.split("（", 1)[1], maxsplit=1)[0].strip()
        suffixes = UNIT_SUFFIXES.get(unit)
        if suffixes is None:
            continue
        checked += 1
        assert any(s in info.name for s in suffixes), f"{path}: unit {unit} must be in the key name {suffixes}"
    assert checked > 50


def test_base_toml_has_a_comment_above_every_key():
    lines = (DEFAULT_DIR / "base.toml").read_text(encoding="utf-8").splitlines()
    for index, line in enumerate(lines):
        if "=" in line and not line.lstrip().startswith(("#", "[")):
            assert lines[index - 1].lstrip().startswith("#"), f"no comment above: {line}"


# ---------------------------------------------------------------- 启动校验

@pytest.mark.parametrize("values, key, reason", [
    ({"http": {"max_body_kb": "big"}}, "http.max_body_kb", "valid integer"),                 # 坏类型
    ({"db": {"statement_timeout_ms": 999_999_999}}, "db.statement_timeout_ms", "less than or equal"),  # 超范围
    ({"http": {"max_body_kbs": 10}}, "http.max_body_kbs", "unknown key"),                      # 拼错键
    ({"loging": {"level": "info"}}, "loging", "unknown key"),                                 # 拼错表
    ({"logging": {"level": "verbose"}}, "logging.level", "Input should be"),                  # 枚举
    ({"rate_limit": {"user_write_per_s": 50, "ip_per_s": 20}}, "rate_limit", "must not exceed"),  # 跨字段
])
def test_startup_rejects_bad_files_with_key_value_reason_and_range(tmp_path, values, key, reason):
    store = make_config_store(tmp_path, values=values, load=False)
    with pytest.raises(ConfigInvalid) as exc:
        store.load()
    error = next(e for e in exc.value.errors if e.key == key)
    assert reason in error.reason and error.allowed
    line = error.line()
    assert key in line and "allowed:" in line


def test_error_output_masks_secret_looking_values(tmp_path):
    store = make_config_store(tmp_path, values={"http": {"api_token": "sk-live-should-not-print"}}, load=False)
    with pytest.raises(ConfigInvalid) as exc:
        store.load()
    text = " ".join(e.line() for e in exc.value.errors)
    assert "http.api_token" in text and "sk-live" not in text and "***" in text


def test_syntax_error_and_missing_env_file_are_rejected(tmp_path):
    store = make_config_store(tmp_path, load=False)
    _write(store, {}, raw="[http\nmax_body_kb = 1\n")
    with pytest.raises(ConfigInvalid) as exc:
        store.load()
    assert "TOML syntax error" in exc.value.errors[0].reason
    (tmp_path / "dev.toml").unlink()
    with pytest.raises(ConfigInvalid) as exc:
        store.load()
    assert exc.value.errors[0].reason == "file not found"
    with pytest.raises(ConfigInvalid):
        read_files(tmp_path, "production")  # 只认 dev / staging / prod


def test_start_process_prints_every_error_and_exits_non_zero(tmp_path, monkeypatch, capsys):
    bad = make_config_store(tmp_path, values={"http": {"max_body_kb": 0}, "db": {"oops": 1}}, load=False)
    previous = set_config_store(bad)
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    try:
        with pytest.raises(SystemExit) as exc:
            start_process(Settings())
        assert exc.value.code == 2
        err = capsys.readouterr().err
        assert "http.max_body_kb = 0" in err and "db.oops" in err and "allowed:" in err
    finally:
        from luzback.logging import shutdown_logging

        shutdown_logging()
        for handler in list(root.handlers):
            root.removeHandler(handler)
        for handler in handlers:
            root.addHandler(handler)
        root.setLevel(level)
        set_config_store(previous)


# ---------------------------------------------------------------- 版本

def test_version_is_digest_of_normalised_content(tmp_path):
    a = make_config_store(tmp_path / "a", values={"http": {"max_body_kb": 300}, "upload": {"doc_max_mb": 8}})
    b = make_config_store(tmp_path / "b", load=False)
    _write(b, {}, raw="# 注释与键顺序不影响版本\n[upload]\ndoc_max_mb = 8\n\n[http]\nmax_body_kb    =   300\n")
    b.load()
    assert a.current().version == b.current().version
    assert len(a.current().version) == 12 and int(a.current().version, 16) >= 0
    c = make_config_store(tmp_path / "c", values={"http": {"max_body_kb": 301}, "upload": {"doc_max_mb": 8}})
    assert c.current().version != a.current().version


# ---------------------------------------------------------------- 热加载

def test_reload_rejects_bad_file_and_keeps_old_snapshot(tmp_path, caplog):
    store = make_config_store(tmp_path, values={"http": {"max_body_kb": 300}})
    old = store.current()
    _write(store, {"http": {"max_body_kb": -5}})
    with caplog.at_level(logging.INFO):
        result = store.reload("file_change")
    assert result.result == "rejected" and result.version == old.version
    assert store.current() is old and store.current().config.http.max_body_kb == 300
    assert store.failures_total == 1 and store.last_failure is result
    assert ev.CONFIG_RELOAD_REJECTED in _events(caplog)
    assert "luz_config_reload_failures_total 1" in store.metrics_lines()
    _write(store, {"http": {"max_body_kb": 400}})  # 修好文件后再加载
    fixed = store.reload("file_change")
    assert fixed.result == "loaded" and fixed.changed_keys == ("http.max_body_kb",)
    assert store.current().config.http.max_body_kb == 400 and store.status()["last_failure"]["result"] == "rejected"


def test_unchanged_reload_and_listener_replay(tmp_path):
    store = make_config_store(tmp_path, values={"http": {"max_body_kb": 300}})
    seen = []
    store.add_listener(lambda result, snapshot: seen.append(result.result))  # 先补上启动那次
    assert seen == ["loaded"]
    assert store.reload("reload_api").result == "unchanged"
    assert seen == ["loaded", "unchanged"]


def test_non_reloadable_key_is_listed_as_restart_pending(tmp_path, caplog):
    store = make_config_store(tmp_path, values={"db": {"statement_timeout_ms": 5000}})
    old_version = store.current().version
    _write(store, {"db": {"statement_timeout_ms": 9000}, "http": {"max_body_kb": 128}})
    with caplog.at_level(logging.INFO):
        result = store.reload("file_change")
    snapshot = store.current()
    assert result.result == "loaded" and result.restart_keys == ("db.statement_timeout_ms",)
    assert snapshot.config.db.statement_timeout_ms == 5000          # 不应用，下次重启生效
    assert snapshot.config.http.max_body_kb == 128                  # 可热加载的照常生效
    assert snapshot.restart_pending == ("db.statement_timeout_ms",) and snapshot.version != old_version
    assert store.status()["restart_pending"] == ["db.statement_timeout_ms"]
    assert ev.CONFIG_RESTART_REQUIRED in _events(caplog)
    restarted = make_config_store(tmp_path / "restart", values={"db": {"statement_timeout_ms": 9000}, "http": {"max_body_kb": 128}})
    assert restarted.current().config.db.statement_timeout_ms == 9000  # 重启后生效，版本与热加载的实例不同直到重启


def test_in_flight_request_keeps_old_snapshot_and_new_request_sees_new_value(tmp_path):
    store = make_config_store(tmp_path, values={"http": {"max_body_kb": 300}})
    router = APIRouter()

    @router.get("/probe")
    def probe(reload: bool = False):
        before = current_config().http.max_body_kb
        if reload:  # 处理中途热加载
            _write(store, {"http": {"max_body_kb": 500}})
            assert store.reload("file_change").result == "loaded"
        return {"before": before, "after": current_config().http.max_body_kb}

    client = TestClient(create_app([router], settings=Settings(), config=store))
    assert client.get("/probe", params={"reload": True}).json() == {"before": 300, "after": 300}
    assert client.get("/probe").json() == {"before": 500, "after": 500}


# ---------------------------------------------------------------- 覆盖规则（灰度）

def _override_store(tmp_path, overrides: dict) -> ConfigStore:
    return make_config_store(tmp_path, values={"overrides": overrides})


def test_overrides_by_store_channel_client_version_and_bucket(tmp_path):
    store = _override_store(tmp_path, {
        "stores_7_8": {"stores": [7, 8], "set": {"features.maintenance_mode": True}},
        "old_miniprogram": {"channels": ["miniprogram"], "client_version_max": "1.9.9",
                            "set": {"upload.doc_max_mb": 2}},
        "half_users": {"percent": 50, "set": {"features.maintenance_mode": True}},
    })
    snap = store.current()
    assert snap.resolve(store_id=7).features.maintenance_mode is True
    assert snap.resolve(store_id=9).features.maintenance_mode is False
    assert snap.resolve(channel="miniprogram", client_version="1.2.0").upload.doc_max_mb == 2
    assert snap.resolve(channel="miniprogram", client_version="2.0.0").upload.doc_max_mb == 5
    assert snap.resolve(channel="app", client_version="1.0.0").upload.doc_max_mb == 5
    assert snap.resolve(channel="miniprogram").upload.doc_max_mb == 5  # 不知道版本就不命中
    hits = [snap.resolve(user_ref=str(uid)).features.maintenance_mode for uid in range(400)]
    assert 120 < sum(hits) < 280  # 约一半用户
    assert hits == [snap.resolve(user_ref=str(uid)).features.maintenance_mode for uid in range(400)]  # 稳定落桶
    assert user_bucket("half_users", "42") == user_bucket("half_users", "42")
    assert snap.resolve().features.maintenance_mode is False  # 没有用户编号不进百分比灰度


def test_override_rules_are_validated(tmp_path):
    cases = [
        ({"x": {"stores": [1], "set": {"http.nope": 1}}}, "unknown key"),
        ({"x": {"stores": [1], "set": {"rate_limit.ip_per_s": 1}}}, "cannot be overridden by store"),
        ({"x": {"set": {"features.maintenance_mode": True}}}, "at least one condition"),
        ({"x": {"channels": ["miniprogram"], "set": {"upload.doc_max_mb": 0}}}, "greater than or equal"),
        ({"x": {"channels": ["wechat"], "set": {"upload.doc_max_mb": 3}}}, "Input should be"),
    ]
    for index, (overrides, reason) in enumerate(cases):
        store = make_config_store(tmp_path / str(index), values={"overrides": overrides}, load=False)
        with pytest.raises(ConfigInvalid) as exc:
            store.load()
        assert any(reason in e.reason for e in exc.value.errors), (overrides, exc.value.errors)


def test_config_for_uses_request_client_headers(tmp_path):
    store = _override_store(tmp_path, {"mp": {"channels": ["miniprogram"], "set": {"upload.doc_max_mb": 9}}})
    with use_snapshot(store.current(), channel="miniprogram", client_version="1.0.0"):
        assert config_for().upload.doc_max_mb == 9
        assert current_config().upload.doc_max_mb == 5  # current_config 不含覆盖规则
    with use_snapshot(store.current(), channel="h5"):
        assert config_for().upload.doc_max_mb == 5


# ---------------------------------------------------------------- 监听

def _wait(predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def test_poll_watcher_reloads_after_debounce(tmp_path):
    store = make_config_store(tmp_path, values={"http": {"max_body_kb": 300}})
    watcher = ConfigWatcher(store, mode="poll", poll_s=0.05, debounce_ms=100)
    watcher.start()
    try:
        _write(store, {"http": {"max_body_kb": 301}})
        assert _wait(lambda: store.current().config.http.max_body_kb == 301)
        assert watcher.backend == "poll"
    finally:
        watcher.stop()


def test_watchfiles_backend_triggers_reload(tmp_path, monkeypatch):
    store = make_config_store(tmp_path, values={"http": {"max_body_kb": 300}})
    calls = {}

    def fake_watch(path, **kwargs):  # 模拟 watchfiles.watch：参数正确、产生一次变化后结束
        calls.update(kwargs, path=path)
        _write(store, {"http": {"max_body_kb": 302}})
        yield {("modified", str(Path(path) / "dev.toml"))}

    monkeypatch.setitem(sys.modules, "watchfiles", types.SimpleNamespace(watch=fake_watch))
    watcher = ConfigWatcher(store, mode="auto")  # 默认防抖 1.5 秒
    watcher.start()
    assert _wait(lambda: store.current().config.http.max_body_kb == 302)
    watcher.stop()
    assert calls["step"] == 1500 and calls["debounce"] == 2000 and calls["recursive"] is False and calls["stop_event"] is not None
    assert calls["watch_filter"](None, "x/dev.toml") and not calls["watch_filter"](None, "x/dev.toml.swp")


def test_off_watcher_does_nothing(tmp_path):
    watcher = ConfigWatcher(make_config_store(tmp_path), mode="off")
    watcher.start()
    assert watcher._thread is None
    with pytest.raises(ValueError):
        ConfigWatcher(make_config_store(tmp_path / "b"), mode="sometimes")


# ---------------------------------------------------------------- 环境变量

def test_env_scan_reports_moved_and_unknown_names_only():
    warnings = dict(env_warnings({"LOG_LEVEL": "debug", "LUZ_DATABSE_URL": "x", "LUZ_DATABASE_URL": "y",
                                  "RATE_IP_PER_SECOND": "1", "PATH": "/bin"}))
    assert "logging" in warnings["LOG_LEVEL"] and "rate_limit" in warnings["RATE_IP_PER_SECOND"]
    assert "unknown" in warnings["LUZ_DATABSE_URL"]
    assert "LUZ_DATABASE_URL" not in warnings and "PATH" not in warnings
    assert all("debug" not in note for note in warnings.values())  # 只给名字，不回显值


def test_build_snapshot_keep_only_affects_non_reloadable_keys():
    data = {"db": {"statement_timeout_ms": 7000}, "http": {"max_body_kb": 64}}
    kept = RuntimeConfig.model_validate({"db": {"statement_timeout_ms": 1000}})
    snapshot, desired = build_snapshot(data, env="dev", keep=kept)
    assert snapshot.config.db.statement_timeout_ms == 1000 and snapshot.config.http.max_body_kb == 64
    assert desired["db.statement_timeout_ms"] == 7000


def test_metrics_endpoint_reports_config_and_log_counters(tmp_path):
    store = make_config_store(tmp_path)
    client = TestClient(create_app([], settings=Settings(), config=store), client=("10.0.0.8", 5000))
    text = client.get("/metrics").text
    assert f'luz_config_info{{version="{store.current().version}",env="dev"}} 1' in text
    assert "luz_log_dropped_total" in text and "luz_config_reload_failures_total 0" in text


def test_concurrent_reloads_are_serialised(tmp_path):
    store = make_config_store(tmp_path, values={"http": {"max_body_kb": 300}})
    _write(store, {"http": {"max_body_kb": 301}})
    results: list[str] = []
    threads = [threading.Thread(target=lambda: results.append(store.reload("reload_api").result)) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results.count("loaded") == 1 and results.count("unchanged") == 7


def test_check_fails_when_base_misses_keys(tmp_path, capsys):
    (tmp_path / "base.toml").write_text("[http]\nmax_body_kb = 1\n", encoding="utf-8")
    for env in ENVIRONMENTS:
        (tmp_path / f"{env}.toml").write_text("", encoding="utf-8")
    assert config_check(["--env", "dev", "--dir", str(tmp_path)]) == 1  # base.toml 缺键
    assert "missing keys" in capsys.readouterr().err
