"""熔断 / 重试 / 超时 与 应用工厂。"""
from __future__ import annotations

import time

import pytest
from fastapi import APIRouter, Depends
from fastapi.testclient import TestClient

from luzback.app import create_app, idempotency_key
from luzback.config import Settings
from luzback.errors import AppError, ErrorCode
from luzback.ratelimit import FixedWindowLimiter, RateLimitRule
from luzback.resilience import Bulkhead, BreakerRegistry, CircuitBreaker, CircuitOpenError, TransientError, UpstreamError, resilient, retry_call
from luzback.testing import FakeClock


def test_breaker_opens_half_opens_and_closes():
    clock = FakeClock(0)
    breaker = CircuitBreaker("pay", window_seconds=60, min_failures=3, fail_rate=0.5, open_seconds=30, half_open_probes=1, clock=clock)
    for _ in range(3):
        breaker.record_failure()
    assert breaker.state == "open" and not breaker.allow()
    clock.advance(31)
    assert breaker.state == "half_open" and breaker.allow() and not breaker.allow()
    breaker.record_success()
    assert breaker.state == "closed" and breaker.opened_count == 1
    # 半开探测失败立即再次打开
    for _ in range(3):
        breaker.record_failure()
    clock.advance(31)
    assert breaker.allow()
    breaker.record_failure()
    assert breaker.state == "open" and breaker.opened_count == 3


def test_breaker_ignores_low_failure_rate():
    breaker = CircuitBreaker("x", min_failures=3, fail_rate=0.5)
    for _ in range(10):
        breaker.record_success()
    for _ in range(3):
        breaker.record_failure()
    assert breaker.state == "closed"


def test_retry_only_transient_with_backoff():
    sleeps = []
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise TransientError("try again")
        return "ok"

    assert retry_call(flaky, attempts=3, base_delay=0.1, jitter=0, sleep=sleeps.append) == "ok"
    assert sleeps == [0.1, 0.2]
    with pytest.raises(ValueError):
        retry_call(lambda: (_ for _ in ()).throw(ValueError("permanent")), attempts=3, sleep=sleeps.append)
    assert len(sleeps) == 2  # 永久错误不重试


def test_resilient_timeout_breaker_and_fallback():
    registry = BreakerRegistry(min_failures=2, fail_rate=0.5, open_seconds=30)
    breaker = registry.get("slow")
    slow = lambda: time.sleep(0.5) or "late"  # noqa: E731
    with pytest.raises(UpstreamError):
        resilient("slow", slow, timeout_s=0.05, attempts=1, breaker=breaker, sleep=lambda _: None)
    with pytest.raises(UpstreamError):
        resilient("slow", slow, timeout_s=0.05, attempts=1, breaker=breaker, sleep=lambda _: None)
    assert breaker.state == "open"
    with pytest.raises(CircuitOpenError) as exc:
        resilient("slow", slow, timeout_s=0.05, breaker=breaker)
    assert exc.value.code == ErrorCode.SERVICE_UNAVAILABLE and exc.value.retry_after >= 1
    assert resilient("slow", slow, timeout_s=0.05, breaker=breaker, fallback=lambda e: "cached") == "cached"
    assert registry.snapshot()[0]["state"] == "open"
    # 业务错误不计入熔断
    fresh = registry.get("biz")
    with pytest.raises(AppError):
        resilient("biz", lambda: (_ for _ in ()).throw(AppError(ErrorCode.BUSINESS_RULE, "no")), breaker=fresh)
    assert fresh.snapshot()["recent_failures"] == 0


def test_bulkhead_rejects_when_full():
    bulkhead = Bulkhead("b", 1)
    with bulkhead:
        with pytest.raises(AppError) as exc:
            with bulkhead:
                pass
    assert exc.value.code == ErrorCode.SERVICE_UNAVAILABLE
    with bulkhead:
        pass


def _app():
    router = APIRouter(prefix="/api/v1/app")

    @router.get("/ok")
    def ok():
        return {"code": 0, "message": "ok", "data": 1}

    @router.get("/boom")
    def boom():
        raise RuntimeError("unexpected")

    @router.get("/rule")
    def rule():
        raise AppError(ErrorCode.BUSINESS_RULE, "min order 20", data={"min_cents": 2000})

    @router.post("/write")
    def write(key: str = Depends(idempotency_key)):
        return {"code": 0, "message": "ok", "data": key}

    @router.post("/login")
    def login():
        return {"code": 0, "message": "ok", "data": None}

    clock = FakeClock(0)
    rules = [RateLimitRule("login", FixedWindowLimiter(2, 60, clock=clock), path_prefix="/api/v1/app/login",
                           methods=("POST",), key_fn=lambda r: "ip", retry_after=60)]
    settings = Settings(max_body_bytes=100)
    app = create_app([router], settings=settings, rate_rules=rules)
    return app


def test_app_envelopes_headers_and_errors():
    client = TestClient(_app(), raise_server_exceptions=False)
    ok = client.get("/api/v1/app/ok", headers={"X-Trace-Id": "trace-1"})
    assert ok.json()["data"] == 1 and ok.headers["X-Trace-Id"] == "trace-1" and ok.headers["X-Frame-Options"] == "DENY"
    boom = client.get("/api/v1/app/boom")
    assert boom.status_code == 500 and boom.json()["code"] == 5000 and boom.json()["data"]["trace_id"]
    rule = client.get("/api/v1/app/rule")
    assert rule.status_code == 422 and rule.json()["data"] == {"min_cents": 2000}
    missing = client.post("/api/v1/app/write")
    assert missing.status_code == 400 and "Idempotency-Key" in missing.json()["message"]
    good = client.post("/api/v1/app/write", headers={"Idempotency-Key": "3fa85f64-5717-4562-b3fc-2c963f66afa6"})
    assert good.json()["data"].startswith("3fa85f64")
    big = client.post("/api/v1/app/write", content=b"x" * 200, headers={"Content-Length": "200", "Idempotency-Key": "3fa85f64-5717-4562"})
    assert big.status_code == 413 and big.json()["code"] == 4001


def test_app_rate_rule_and_system_endpoints():
    client = TestClient(_app(), raise_server_exceptions=False)
    assert client.post("/api/v1/app/login").status_code == 200
    assert client.post("/api/v1/app/login").status_code == 200
    limited = client.post("/api/v1/app/login")
    assert limited.status_code == 429 and limited.headers["Retry-After"] == "60"
    assert client.get("/healthz").json()["data"]["status"] == "up"
    ready = client.get("/readyz")
    assert ready.status_code in (200, 503) and "database" in ready.json()["data"]
    assert client.get("/metrics").status_code == 404  # 外网地址看不到监控端点
    internal = TestClient(client.app, raise_server_exceptions=False, client=("10.0.0.8", 5000))
    metrics = internal.get("/metrics").text
    assert "http_requests_total" in metrics and 'rule="login"' in metrics
    assert 'http_request_duration_seconds_bucket' in metrics and 'le="+Inf"' in metrics


def test_client_ip_trusts_only_configured_proxies():
    from starlette.requests import Request

    from luzback.app import client_ip

    app = _app()

    def request(peer: str, xff: str) -> Request:
        scope = {"type": "http", "headers": [(b"x-forwarded-for", xff.encode())], "client": (peer, 1), "app": app}
        return Request(scope)

    # 直连的客户端伪造 X-Forwarded-For：忽略，取对端地址
    assert client_ip(request("203.0.113.9", "1.2.3.4")) == "203.0.113.9"
    # 经过本机 nginx：取最右边的不可信地址，最左边伪造的值不被采用
    assert client_ip(request("127.0.0.1", "1.2.3.4, 198.51.100.7")) == "198.51.100.7"
    assert client_ip(request("127.0.0.1", "198.51.100.7")) == "198.51.100.7"
