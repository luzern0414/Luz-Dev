"""应用工厂 v2：请求上下文（trace / request id）、请求体上限、多规则限流、CORS、安全响应头、
统一异常处理（含未知异常 → 5000 + trace_id）、/healthz /readyz /metrics，并挂载各模块路由。
"""
from __future__ import annotations

import hmac
import ipaddress
import logging
import threading
import time
from collections import defaultdict
from collections.abc import Iterable

from fastapi import APIRouter, FastAPI, Header, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, PlainTextResponse
from sqlalchemy import text

from .config import Settings, get_settings
from .errors import AppError, ErrorCode
from .idempotency import validate_key
from .logging import request_id_var, trace_id_var
from .ratelimit import RateLimitRule
from .resilience import BreakerRegistry
from .security import SECURITY_HEADERS, new_request_id, new_trace_id

logger = logging.getLogger("luzback.app")


BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0)  # 秒；可算 P50/P95/P99


class Metrics:
    """进程内计数器与延迟直方图，/metrics 以 Prometheus 文本格式输出。

    计数只覆盖本进程：部署时一个容器一个进程、多副本横向扩展（FastAPI 部署指南），由 Prometheus 按实例汇总。
    """

    def __init__(self):
        self._lock = threading.Lock()
        self.requests: dict[tuple[str, str, int], int] = defaultdict(int)
        self.duration_sum: dict[tuple[str, str], float] = defaultdict(float)
        self.duration_count: dict[tuple[str, str], int] = defaultdict(int)
        self.counters: dict[str, int] = defaultdict(int)
        self.buckets: dict[tuple[str, str], list[int]] = defaultdict(lambda: [0] * len(BUCKETS))

    def observe(self, method: str, route: str, status: int, seconds: float) -> None:
        with self._lock:
            self.requests[(method, route, status)] += 1
            self.duration_sum[(method, route)] += seconds
            self.duration_count[(method, route)] += 1
            counts = self.buckets[(method, route)]
            for i, bound in enumerate(BUCKETS):
                if seconds <= bound:
                    counts[i] += 1

    def inc(self, name: str, value: int = 1) -> None:
        with self._lock:
            self.counters[name] += value

    def render(self) -> str:
        lines = ["# TYPE http_requests_total counter"]
        with self._lock:
            for (method, route, status), count in sorted(self.requests.items()):
                lines.append(f'http_requests_total{{method="{method}",route="{route}",status="{status}"}} {count}')
            lines.append("# TYPE http_request_duration_seconds histogram")
            for (method, route), total in sorted(self.duration_sum.items()):
                count = self.duration_count[(method, route)]
                for bound, value in zip(BUCKETS, self.buckets[(method, route)]):
                    lines.append(f'http_request_duration_seconds_bucket{{method="{method}",route="{route}",le="{bound}"}} {value}')
                lines.append(f'http_request_duration_seconds_bucket{{method="{method}",route="{route}",le="+Inf"}} {count}')
                lines.append(f'http_request_duration_seconds_sum{{method="{method}",route="{route}"}} {total:.6f}')
                lines.append(f'http_request_duration_seconds_count{{method="{method}",route="{route}"}} {count}')
            for name, value in sorted(self.counters.items()):
                lines.append(f"{name} {value}")
        return "\n".join(lines) + "\n"


def _networks(cidrs: Iterable[str]) -> list:
    out = []
    for cidr in cidrs:
        try:
            out.append(ipaddress.ip_network(cidr, strict=False))
        except ValueError:
            logger.warning("invalid network in settings: %s", cidr)
    return out


def _in(address: str, networks: list) -> bool:
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    return any(ip.version == net.version and ip in net for net in networks)


def client_ip(request: Request) -> str:
    """真实客户端地址。

    只有 TCP 对端在可信代理网段（LUZ_TRUSTED_PROXIES，即 nginx）时才读 X-Forwarded-For，
    并从右往左跳过可信代理，取第一个不可信地址——最左边的值可由客户端伪造，不能用于限流、锁定和审计。
    """
    peer = request.client.host if request.client else "unknown"
    settings = getattr(request.app.state, "settings", None) or get_settings()
    trusted = _networks(settings.trusted_proxies)
    if not _in(peer, trusted):
        return peer[:64]
    hops = [h.strip() for h in request.headers.get("X-Forwarded-For", "").split(",") if h.strip()]
    for hop in reversed(hops):
        if not _in(hop, trusted):
            return hop[:64]
    return (hops[0] if hops else peer)[:64]


def _envelope(code: ErrorCode, message: str, data=None, status: int | None = None, retry_after: int | None = None) -> JSONResponse:
    response = JSONResponse(status_code=status or 400, content={"code": int(code), "message": message, "data": data})
    if retry_after:
        response.headers["Retry-After"] = str(retry_after)
    return response


def create_app(
    routers: Iterable[APIRouter] = (),
    settings: Settings | None = None,
    rate_rules: Iterable[RateLimitRule] = (),
    breakers: BreakerRegistry | None = None,
    title: str = "Luz Backend",
    readiness_checks: dict[str, callable] | None = None,
) -> FastAPI:
    settings = settings or get_settings()
    rules = list(rate_rules)
    metrics = Metrics()
    app = FastAPI(title=title, docs_url="/docs" if not settings.is_prod else None, redoc_url=None,
                  openapi_url="/openapi.json" if not settings.is_prod else None)  # 生产不公开接口文档
    app.state.settings = settings
    app.state.metrics = metrics
    app.state.breakers = breakers or BreakerRegistry()

    if settings.cors_origins:
        app.add_middleware(CORSMiddleware, allow_origins=list(settings.cors_origins), allow_credentials=True,
                           allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"], allow_headers=["*"], max_age=600)

    @app.middleware("http")
    async def _request_context(request: Request, call_next):
        started = time.perf_counter()
        request_id = new_request_id(request.headers.get("X-Request-Id"))
        trace_id = new_trace_id(request.headers.get("X-Trace-Id"))
        request.state.request_id = request_id
        request.state.trace_id = trace_id
        token_r = request_id_var.set(request_id)
        token_t = trace_id_var.set(trace_id)
        try:
            response = None
            content_length = request.headers.get("content-length")
            if content_length and content_length.isdigit() and int(content_length) > settings.max_body_bytes:
                response = _envelope(ErrorCode.BAD_REQUEST, "request body too large", status=413)
            if response is None:
                for rule in rules:
                    if not rule.matches(request.url.path, request.method):
                        continue
                    key = rule.key_fn(request)
                    if key is None:
                        continue
                    if not rule.limiter.allow(f"{rule.name}:{key}"):
                        metrics.inc(f'rate_limited_total{{rule="{rule.name}"}}')
                        response = _envelope(ErrorCode.RATE_LIMITED, "too many requests, please retry later",
                                             status=429, retry_after=rule.retry_after)
                        break
            if response is None:
                response = await call_next(request)
        finally:
            request_id_var.reset(token_r)
            trace_id_var.reset(token_t)
        response.headers["X-Request-Id"] = request_id
        response.headers["X-Trace-Id"] = trace_id
        for name, value in SECURITY_HEADERS.items():
            response.headers.setdefault(name, value)
        # 未匹配路由（扫描、404）统一记为 unmatched，避免原始路径让标签无限增长
        route = getattr(request.scope.get("route"), "path", "unmatched")
        metrics.observe(request.method, route, response.status_code, time.perf_counter() - started)
        return response

    @app.exception_handler(AppError)
    async def _app_error(request: Request, exc: AppError) -> JSONResponse:
        data = exc.data
        if exc.code >= 5000:
            data = {"trace_id": getattr(request.state, "trace_id", None), **(data if isinstance(data, dict) else {})}
            logger.warning("app_error", extra={"fields": {"event": "app_error", "code": int(exc.code), "path": request.url.path}})
        return _envelope(exc.code, exc.message, data, status=exc.status, retry_after=exc.retry_after)

    @app.exception_handler(RequestValidationError)
    async def _validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
        errors = [{"field": ".".join(str(p) for p in e.get("loc", []) if p not in ("body", "query", "path")),
                   "message": e.get("msg", "invalid")} for e in exc.errors()]
        return _envelope(ErrorCode.BAD_REQUEST, "invalid request", {"errors": errors}, status=400)

    @app.exception_handler(Exception)
    async def _unexpected(request: Request, exc: Exception) -> JSONResponse:
        trace_id = getattr(request.state, "trace_id", None)
        logger.error("unhandled_error", exc_info=exc, extra={"fields": {"event": "unhandled_error", "path": request.url.path}})
        metrics.inc("unhandled_errors_total")
        return _envelope(ErrorCode.INTERNAL, "internal error", {"trace_id": trace_id}, status=500)

    @app.get("/healthz", tags=["system"])
    def healthz() -> dict:
        return {"code": 0, "message": "ok", "data": {"status": "up"}}

    @app.get("/readyz", tags=["system"])
    def readyz() -> JSONResponse:
        checks: dict[str, str] = {}
        ok = True
        try:
            from .db import get_session_factory
            session = get_session_factory()()
            try:
                session.execute(text("SELECT 1"))
                checks["database"] = "ok"
            finally:
                session.close()
        except Exception as exc:  # noqa: BLE001
            checks["database"] = f"error: {type(exc).__name__}"
            ok = False
        for name, fn in (readiness_checks or {}).items():
            try:
                checks[name] = "ok" if fn() else "degraded"
            except Exception as exc:  # noqa: BLE001
                checks[name] = f"error: {type(exc).__name__}"
                ok = False
        checks["breakers"] = ",".join(f"{b['name']}={b['state']}" for b in app.state.breakers.snapshot()) or "none"
        return JSONResponse(status_code=200 if ok else 503,
                            content={"code": 0 if ok else int(ErrorCode.SERVICE_UNAVAILABLE), "message": "ok" if ok else "not ready", "data": checks})

    @app.get("/metrics", tags=["system"], response_class=PlainTextResponse, include_in_schema=False)
    def metrics_endpoint(request: Request) -> PlainTextResponse:
        """只对内网或持有 LUZ_METRICS_TOKEN 的抓取方开放；对外返回 404，不暴露端点存在。"""
        token = settings.metrics_token
        auth = request.headers.get("Authorization", "")
        allowed = (token and hmac.compare_digest(auth, f"Bearer {token}")) or _in(
            request.client.host if request.client else "", _networks(settings.internal_networks))
        if not allowed:
            return PlainTextResponse("not found", status_code=404)
        return PlainTextResponse(metrics.render(), media_type="text/plain; version=0.0.4")

    for router in routers:
        app.include_router(router)
    return app


def idempotency_key(idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")) -> str:
    """FastAPI 依赖：写接口必须带合法幂等键。"""
    return validate_key(idempotency_key, require=True)  # type: ignore[return-value]


def optional_idempotency_key(idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")) -> str | None:
    return validate_key(idempotency_key, require=False)
