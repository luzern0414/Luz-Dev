"""日志与链路：字段齐全（UTC 毫秒 Z、服务、环境、版本、实例、配置版本、span）、字段名黑名单与正则脱敏、控制字符一行 JSON、
截断、队列满丢 DEBUG / INFO 且 WARNING 以上不丢、日志自身异常不外抛、安全事件不受级别影响、事件名登记、
W3C traceparent 解析与传播（入口、响应头、汇总行、发件箱、消息、定时任务、超时线程）。
"""
from __future__ import annotations

import io
import json
import logging
import queue
import re
from pathlib import Path

import pytest
from fastapi import APIRouter
from fastapi.testclient import TestClient

from luzback import log_events as ev
from luzback import logging as luzlog
from luzback.app import create_app
from luzback.config import Settings
from luzback.logging import (
    JsonFormatter, _BoundedQueueHandler, bind_log_fields, configure_logging, flush_logging, is_sensitive_key, log_event,
    request_id_var, set_static, shutdown_logging, span_id_var, trace_id_var,
)
from luzback.mq import InMemoryBroker, outbox_to_broker
from luzback.outbox import OutboxEvent, dispatch_pending, enqueue_event
from luzback.resilience import call_with_timeout
from luzback.runtime_loader import use_snapshot
from luzback.scheduler import InMemoryLock, Job, run_job
from luzback.testing import make_config_store, memory_session
from luzback.tracing import (
    continue_trace, current_traceparent, format_traceparent, new_trace, outgoing_headers, parse_traceparent,
    resolve_incoming, trace_scope, w3c_trace_id,
)

TS = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z$")
BACKEND = Path(__file__).resolve().parents[2]


@pytest.fixture()
def captured():
    """把根 logger 换成 JSON 队列处理器写到 StringIO；结束后还原（pytest 自己的捕获处理器也还原）。"""
    root = logging.getLogger()
    handlers, level, raise_exc = list(root.handlers), root.level, logging.raiseExceptions
    stream = io.StringIO()
    configure_logging("debug", service="luz-test", env="test", version="1.2.3", instance="i-1", stream=stream)
    set_static(config_version="abc123def456")

    def lines() -> list[dict]:
        flush_logging()
        return [json.loads(line) for line in stream.getvalue().splitlines() if line.strip()]

    yield stream, lines
    shutdown_logging()
    for handler in list(root.handlers):
        root.removeHandler(handler)
    for handler in handlers:
        root.addHandler(handler)
    root.setLevel(level)
    logging.raiseExceptions = raise_exc
    set_static(config_version=None, service=None, env=None, version=None, instance=None)


def test_every_line_has_the_standard_fields(captured):
    stream, lines = captured
    log = logging.getLogger("luz.test")
    with trace_scope("0af7651916cd43dd8448eb211c80319c", job="demo"):
        token = request_id_var.set("req-1")
        log_event(log, ev.JOB_FINISHED, outcome="success", duration_ms=12.5)
        request_id_var.reset(token)
    [line] = lines()
    assert TS.match(line["ts"])
    assert line["level"] == "info" and line["logger"] == "luz.test"
    assert (line["service"], line["env"], line["version"], line["instance"]) == ("luz-test", "test", "1.2.3", "i-1")
    assert line["config_version"] == "abc123def456"
    assert line["event"] == "job.finished" and line["message"] == "job.finished" and line["outcome"] == "success"
    assert line["trace_id"] == "0af7651916cd43dd8448eb211c80319c" and re.fullmatch(r"[0-9a-f]{16}", line["span_id"])
    assert line["request_id"] == "req-1" and line["job"] == "demo" and line["duration_ms"] == 12.5
    assert "\n" not in stream.getvalue().rstrip("\n").split("\n")[0]


def test_caller_cannot_forge_context_fields(captured):
    _, lines = captured
    log_event(logging.getLogger("luz.test"), ev.APP_STARTED, trace_id="forged", service="evil", category="x")
    [line] = lines()
    assert line["service"] == "luz-test" and "trace_id" not in line and "category" not in line


def test_redaction_by_field_name_and_pattern(captured):
    _, lines = captured
    log_event(logging.getLogger("luz.test"), ev.APP_STARTED,
              password="hunter2", access_token="abc", Authorization="Bearer abc.def", cookie="sid=1", pay_sign="x",
              card_no="6222020200112233445", api_key="k", sessionKey="s", sms_code="123456", nested={"refresh_token": "t"},
              token_count=42, member_card_id=17, design="flat", assignee="ops",
              note="call 13812345678, Bearer eyJabc.def.ghi, password=letmein&x=1, id 110101199003071234",
              jwt="eyJhbGciOi.eyJzdWIiOiI0MiJ9.c2lnbmF0dXJl")
    [line] = lines()
    for key in ("password", "access_token", "Authorization", "cookie", "pay_sign", "card_no", "api_key", "sessionKey",
                "sms_code", "jwt"):
        assert line[key] == "***", key
    assert line["nested"] == {"refresh_token": "***"}
    assert line["token_count"] == 42 and line["member_card_id"] == 17  # 计数与会员卡编号不是秘密
    assert line["design"] == "flat" and line["assignee"] == "ops"      # 按词切分，不按子串
    note = line["note"]
    assert "138****5678" in note and "letmein" not in note and "password=***" in note
    assert "eyJabc" not in note and "110101********1234" in note
    assert is_sensitive_key("X-Api-Key") and not is_sensitive_key("signed_at")


def test_control_characters_stay_on_one_json_line(captured):
    stream, lines = captured
    evil = "line1\nline2\r\n{\"level\":\"critical\"}\x1b[31m\u2028end\t"
    log_event(logging.getLogger("luz.test"), ev.APP_STARTED, remark=evil, message="msg\nwith newline")
    raw = stream.getvalue()
    flush_logging()
    raw = stream.getvalue()
    assert raw.count("\n") == 1  # 一条日志一行
    [line] = lines()
    assert "\n" not in line["remark"] and "\\n" in line["remark"] and "\\u001b" in line["remark"]
    assert "\\u2028" in line["remark"] and line["message"] == "msg\\nwith newline"


def test_long_values_and_stacks_are_truncated(captured):
    _, lines = captured
    log = logging.getLogger("luz.test")
    try:
        raise ValueError("x" * 5000)
    except ValueError as exc:
        log_event(log, ev.APP_UNHANDLED_ERROR, logging.ERROR, exc_info=exc, remark="y" * 600)
    [line] = lines()
    assert len(line["remark"]) == 513 and line["remark"].endswith("…")
    assert line["error.type"] == "ValueError" and len(line["error.message"]) == 513
    assert len(line["error.stack"]) <= 2000 and line["level"] == "error"


def test_queue_full_drops_info_but_keeps_warnings():
    stream = io.StringIO()
    target = logging.StreamHandler(stream)
    target.setFormatter(JsonFormatter())
    q: queue.Queue = queue.Queue(maxsize=10)  # 没有监听线程消费：队列很快写满
    handler = _BoundedQueueHandler(q, target, 10)
    log = logging.getLogger("luz.test.queue")
    log.propagate, log.level = False, logging.DEBUG
    log.addHandler(handler)
    before = luzlog.dropped_total()
    try:
        for i in range(30):
            log_event(log, ev.APP_STARTED, i=i)
        assert q.qsize() == 9  # 积压到九成后只收 WARNING 以上
        assert luzlog.dropped_total() - before == 21
        log_event(log, ev.APP_ERROR, logging.WARNING, i="w")          # 队列还有一格
        log_event(log, ev.SECURITY_AUTHZ_FAIL, logging.INFO, i="s")    # 队列已满：在调用线程直接写出
        log_event(log, ev.APP_ERROR, logging.ERROR, i="e")
        written = [json.loads(x) for x in stream.getvalue().splitlines()]
        assert [x["i"] for x in written] == ["s", "e"] and written[0]["category"] == "security"
        assert luzlog.dropped_total() - before == 21  # WARNING 以上与安全事件一条没丢
    finally:
        log.removeHandler(handler)
        log.propagate = True


def test_logging_failures_never_reach_the_caller(captured):
    _, lines = captured

    class Boom:
        def __str__(self) -> str:
            raise RuntimeError("cannot render")

    before = luzlog.errors_total()
    log = logging.getLogger("luz.test")
    log_event(log, ev.APP_STARTED, thing=Boom())                  # 格式化失败：写一条最小的错误行
    [line] = lines()
    assert line["event"] == ev.LOG_FORMAT_FAILED and luzlog.errors_total() - before >= 1

    class BrokenStream(io.StringIO):
        def write(self, _text):  # noqa: D401
            raise OSError("disk full")

    bad = logging.getLogger("luz.test.broken")
    handler = luzlog._SafeStreamHandler(BrokenStream())
    handler.setFormatter(JsonFormatter())
    bad.addHandler(handler)
    bad.propagate = False
    try:
        log_event(bad, ev.APP_STARTED)  # 写出失败：不抛出，只计数
        assert luzlog.errors_total() - before >= 2
    finally:
        bad.removeHandler(handler)
        bad.propagate = True


def test_prod_disables_logging_raise_exceptions():
    root = logging.getLogger()
    handlers, raise_exc = list(root.handlers), logging.raiseExceptions
    try:
        configure_logging("info", service="s", env="prod", version="v", instance="i", stream=io.StringIO())
        assert logging.raiseExceptions is False
    finally:
        shutdown_logging()
        for handler in list(root.handlers):
            root.removeHandler(handler)
        for handler in handlers:
            root.addHandler(handler)
        logging.raiseExceptions = raise_exc


def test_security_events_ignore_level_settings(captured):
    _, lines = captured
    quiet = logging.getLogger("luz.test.quiet")
    quiet.setLevel(logging.ERROR)
    try:
        log_event(quiet, ev.APP_STARTED)                                    # 普通 INFO 被级别挡住
        log_event(quiet, ev.SECURITY_AUTHZ_FAIL, logging.WARNING, source_ip="203.0.113.9")
    finally:
        quiet.setLevel(logging.NOTSET)
    [line] = lines()
    assert line["event"] == "security.authz_fail" and line["category"] == "security"


def test_event_names_are_registered_constants():
    for name in ev.EVENTS:
        assert re.fullmatch(r"[a-z][a-z_]*\.[a-z][a-z_]*", name), name
    assert all(n.startswith("security.") for n in ev.EVENTS if ev.is_security(n))
    literal = re.compile(r"log_event\(\s*[\w.]+\s*,\s*[\"']([^\"']+)[\"']")
    found = []
    for path in BACKEND.glob("*/code/**/*.py"):
        for name in literal.findall(path.read_text(encoding="utf-8")):
            found.append((path.name, name))
    unregistered = [(f, n) for f, n in found if n not in ev.EVENTS]
    assert not unregistered, f"log_event names must be registered in luzback/log_events.py: {unregistered}"


# ---------------------------------------------------------------- traceparent

def test_parse_traceparent_rules():
    good = "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01"
    assert parse_traceparent(good) == ("0af7651916cd43dd8448eb211c80319c", "b7ad6b7169203331")
    assert parse_traceparent("01-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-00-extra") is not None  # 新版本可带扩展
    for bad in (None, "", "garbage", good.upper(), "ff" + good[2:], good + "-extra",
                "00-00000000000000000000000000000000-b7ad6b7169203331-01",
                "00-0af7651916cd43dd8448eb211c80319c-0000000000000000-01",
                "00-0af7651916cd43dd8448eb211c80319-b7ad6b7169203331-01"):
        assert parse_traceparent(bad) is None, bad


def test_resolve_incoming_keeps_legacy_ids_round_trip():
    trace, parent = resolve_incoming("00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01", "anything")
    assert trace == "0af7651916cd43dd8448eb211c80319c" and parent == "b7ad6b7169203331"
    assert resolve_incoming(None, "t_abc123") == ("t_abc123", None)
    legacy = "t_abc123"
    downstream = format_traceparent(legacy, "b7ad6b7169203331")  # 上游用旧编号：traceparent 写摘要，同时带 X-Trace-Id
    assert resolve_incoming(downstream, legacy)[0] == legacy
    assert resolve_incoming(downstream, None)[0] == w3c_trace_id(legacy)
    fresh, parent = resolve_incoming("bad", "bad id with spaces")
    assert re.fullmatch(r"[0-9a-f]{32}", fresh) and parent is None


def _traced_app(tmp_path, routes: dict | None = None):
    router = APIRouter(prefix="/api/v1/app")

    @router.get("/items/{item_id}")
    def item(item_id: int):
        return {"code": 0, "message": "ok", "data": {"item": item_id, "trace": trace_id_var.get(),
                                                    "outgoing": outgoing_headers()}}

    @router.get("/boom")
    def boom():
        raise RuntimeError("secret detail must not leak")

    return create_app([router], settings=Settings(), config=make_config_store(tmp_path))


def test_request_continues_incoming_trace_and_writes_summary_line(tmp_path, captured):
    _, lines = captured
    client = TestClient(_traced_app(tmp_path), raise_server_exceptions=False)
    incoming = "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-00"
    resp = client.get("/api/v1/app/items/7", headers={"traceparent": incoming, "X-Client": "miniprogram",
                                                     "X-Client-Version": "1.4.0"})
    body = resp.json()["data"]
    assert body["trace"] == "0af7651916cd43dd8448eb211c80319c"
    out_trace, out_span = parse_traceparent(resp.headers["traceparent"])
    assert out_trace == "0af7651916cd43dd8448eb211c80319c" and out_span != "b7ad6b7169203331"
    assert resp.headers["traceparent"].endswith("-01") and resp.headers["X-Request-Id"]
    assert parse_traceparent(body["outgoing"]["traceparent"])[0] == out_trace  # 调下游带同一个 trace
    client.get("/healthz")  # 探测成功不写汇总行
    summary = [x for x in lines() if x.get("event") == "http.request"]
    assert len(summary) == 1
    line = summary[0]
    assert line["http.route"] == "/api/v1/app/items/{item_id}" and line["http.method"] == "GET"
    assert line["http.status"] == 200 and line["outcome"] == "success" and line["duration_ms"] >= 0
    assert line["trace_id"] == out_trace and line["span_id"] == out_span and line["client.platform"] == "miniprogram"
    assert line["config_version"]


def test_unhandled_error_is_logged_once_with_trace_and_hidden_from_client(tmp_path, captured):
    _, lines = captured
    client = TestClient(_traced_app(tmp_path), raise_server_exceptions=False)
    resp = client.get("/api/v1/app/boom", headers={"X-Trace-Id": "t_agent01"})
    assert resp.status_code == 500 and resp.json()["data"] == {"trace_id": "t_agent01"}
    assert "secret detail" not in resp.text and resp.headers["X-Trace-Id"] == "t_agent01"
    records = lines()
    errors = [x for x in records if x.get("event") == ev.APP_UNHANDLED_ERROR]
    assert len(errors) == 1 and errors[0]["trace_id"] == "t_agent01" and errors[0]["error.type"] == "RuntimeError"
    [summary] = [x for x in records if x.get("event") == "http.request"]
    assert summary["http.status"] == 500 and summary["outcome"] == "failure" and summary["reason"] == "internal"


def test_outbox_and_queue_carry_the_trace():
    db = memory_session()
    with trace_scope("t_agent02"):
        producer_span = span_id_var.get()
        event = enqueue_event(db, "order.paid", {"order_id": 1})
        db.commit()
    assert event.trace_id == "t_agent02" and event.traceparent.split("-")[2] == producer_span
    seen = {}
    broker = InMemoryBroker()
    publish = outbox_to_broker(broker)

    def handler(topic, payload, evt):
        seen["dispatch"] = trace_id_var.get()
        publish(topic, payload, evt)

    assert dispatch_pending(db, handler)["sent"] == 1
    assert seen["dispatch"] == "t_agent02"  # 投递时接上生产者的 trace（旧编号也能接上）
    broker.consume("order.paid", "g", lambda message: seen.setdefault("consume", trace_id_var.get()))
    assert seen["consume"] == "t_agent02"
    stored = db.get(OutboxEvent, event.id)
    assert stored.status == "sent"


def test_scheduler_runs_get_a_new_trace_with_job_and_run_id(captured):
    _, lines = captured
    db = memory_session("luzback.scheduler")
    factory = lambda: db.__class__(bind=db.get_bind())  # noqa: E731
    seen = {}

    def ok_job(_db):
        seen["trace"] = trace_id_var.get()
        return "done"

    def bad_job(_db):
        raise RuntimeError("job failed")

    with trace_scope("caller-trace"):
        assert run_job(Job("demo.ok", 60, ok_job), factory, InMemoryLock()) == "ok"
    assert run_job(Job("demo.bad", 60, bad_job), factory, InMemoryLock()) == "error"
    finished = [x for x in lines() if x.get("event") == ev.JOB_FINISHED]
    ok_line, bad_line = finished
    assert ok_line["job"] == "demo.ok" and ok_line["run_id"] and ok_line["outcome"] == "success"
    assert ok_line["trace_id"] == seen["trace"] != "caller-trace"  # 每次运行新建 trace
    assert bad_line["level"] == "error" and bad_line["error.type"] == "RuntimeError" and bad_line["outcome"] == "failure"


def test_timeout_thread_keeps_context_and_snapshot(tmp_path):
    store = make_config_store(tmp_path, values={"http": {"max_body_kb": 77}})
    from luzback.runtime_loader import current_config

    with trace_scope("t_ctx"), use_snapshot(store.current()), bind_log_fields(order_no="A1"):
        got = call_with_timeout(lambda: (trace_id_var.get(), current_config().http.max_body_kb,
                                         luzlog.fields_var.get()["order_no"]), 2)
    assert got == ("t_ctx", 77, "A1")
    with new_trace():
        assert current_traceparent().startswith("00-")
    assert current_traceparent() is None
    with continue_trace("00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01"):
        assert trace_id_var.get() == "0af7651916cd43dd8448eb211c80319c"
