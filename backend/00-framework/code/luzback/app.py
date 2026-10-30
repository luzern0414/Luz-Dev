"""应用工厂 v2：请求上下文（traceparent / trace / request id、运行配置快照）、请求体上限、维护模式、多规则限流、CORS、
安全响应头、统一异常处理（含未知异常 → 5000 + trace_id）、每个请求一条 http.request 汇总行、/healthz /readyz /metrics，
并挂载各模块路由。

请求开始时取一次运行配置快照（luzback.runtime_loader.use_snapshot），处理中途热加载不影响本请求。
"""
from __future__ import annotations

import hmac
import ipaddress
import logging
import threading
import time
from collections import defaultdict
from collections.abc import Callable, Iterable
from contextlib import asynccontextmanager

from fastapi import APIRouter, FastAPI, Header, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, PlainTextResponse
from sqlalchemy import text

from . import log_events as ev
from .config import Settings, get_settings
from .errors import AppError, ErrorCode
from .idempotency import validate_key
from .logging import (
    log_event, metrics_lines as log_metrics_lines, request_id_var, span_id_var, store_id_var, trace_id_var, user_id_var,
)
from .ratelimit import RateLimitRule
from .resilience import BreakerRegistry
from .runtime_loader import ConfigStore, config_for, config_store, use_snapshot
from .security import SECURITY_HEADERS, new_request_id
from .tracing import format_traceparent, new_span_id, resolve_incoming

logger = logging.getLogger("luzback.app")


BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0)  # 秒；可算 P50/P95/P99
WRITE_METHODS = ("POST", "PUT", "PATCH", "DELETE")
PROBE_PATHS = ("/healthz", "/readyz", "/metrics")  # 高频探测：成功时不写汇总行（网关访问日志已记录）
CLIENTS = {"miniprogram", "app", "h5", "admin", "staff", "agent"}


class Metrics:
    """进程内计数器与延迟直方图，/metrics 以 Prometheus 文本格式输出。

    计数只覆盖本进程：部署时一个容器一个进程、多副本横向扩展（FastAPI 部署指南），由 Prometheus 按实例汇总。
    collectors：额外的指标行（日志丢弃数、配置版本等），渲染时调用。
    """

    def __init__(self):
        self._lock = threading.Lock()
        self.requests: dict[tuple[str, str, int], int] = defaultdict(int)
        self.duration_sum: dict[tuple[str, str], float] = defaultdict(float)
        self.duration_count: dict[tuple[str, str], int] = defaultdict(int)
        self.counters: dict[str, int] = defaultdict(int)
        self.buckets: dict[tuple[str, str], list[int]] = defaultdict(lambda: [0] * len(BUCKETS))
        self.collectors: list[Callable[[], Iterable[str]]] = []

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
        for collector in list(self.collectors):
            try:
                lines.extend(collector())
            except Exception:  # noqa: BLE001 - 指标收集失败不影响 /metrics 其余部分
                lines.append("# collector failed")
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


def _settings_of(request: Request) -> Settings:
    return getattr(request.app.state, "settings", None) or get_settings()


def client_ip(request: Request) -> str:
    """真实客户端地址。

    只有 TCP 对端在可信代理网段（LUZ_TRUSTED_PROXIES，即 nginx）时才读 X-Forwarded-For，
    并从右往左跳过可信代理，取第一个不可信地址——最左边的值可由客户端伪造，不能用于限流、锁定和审计。
    """
    peer = request.client.host if request.client else "unknown"
    settings = _settings_of(request)
    trusted = _networks(settings.trusted_proxies)
    if not _in(peer, trusted):
        return peer[:64]
    hops = [h.strip() for h in request.headers.get("X-Forwarded-For", "").split(",") if h.strip()]
    for hop in reversed(hops):
        if not _in(hop, trusted):
            return hop[:64]
    return (hops[0] if hops else peer)[:64]


def bearer_matches(request: Request, token: str) -> bool:
    return bool(token) and hmac.compare_digest(request.headers.get("Authorization", ""), f"Bearer {token}")


def is_internal_request(request: Request) -> bool:
    """真实客户端地址在内网网段（LUZ_INTERNAL_NETWORKS）或本机回环（容器内 docker exec 调用）。"""
    address = client_ip(request)
    try:
        if ipaddress.ip_address(address).is_loopback:
            return True
    except ValueError:
        return False
    return _in(address, _networks(_settings_of(request).internal_networks))


def ops_access(request: Request) -> str:
    """运维接口（/internal/*）的访问判断：内网且持有运维令牌（LUZ_OPS_TOKEN）。

    返回 "ok"；不在内网返回 "hidden"（对外 404，不暴露端点存在）；内网但令牌不对或未配置返回 "forbidden"（403）。
    """
    if not is_internal_request(request):
        return "hidden"
    return "ok" if bearer_matches(request, _settings_of(request).ops_token) else "forbidden"


def bind_identity(request: Request | None, user_ref: str | int | None = None, store_id: int | None = None) -> None:
    """鉴权依赖调用：把用户内部编号与门店写进请求上下文，请求汇总行（http.request）与之后的日志带上它们。

    user_ref 只放内部编号（会员 "42"、员工 "staff:7"），不放手机号、openid 或用户名。
    """
    if request is not None:
        if user_ref is not None:
            request.state.user_ref = str(user_ref)
        if store_id is not None:
            request.state.store_id = int(store_id)
    if user_ref is not None:
        user_id_var.set(str(user_ref))
    if store_id is not None:
        store_id_var.set(str(store_id))


def _envelope(code: ErrorCode, message: str, data=None, status: int | None = None, retry_after: int | None = None) -> JSONResponse:
    response = JSONResponse(status_code=status or 400, content={"code": int(code), "message": message, "data": data})
    if retry_after:
        response.headers["Retry-After"] = str(retry_after)
    return response


def _client(value: str | None) -> str | None:
    value = (value or "").strip().lower()
    return value if value in CLIENTS else None


def _client_version(value: str | None) -> str | None:
    value = (value or "").strip()
    return value[:32] if value and all(c.isalnum() or c in ".-+" for c in value) else None


def create_app(
    routers: Iterable[APIRouter] = (),
    settings: Settings | None = None,
    rate_rules: Iterable[RateLimitRule] = (),
    breakers: BreakerRegistry | None = None,
    title: str = "Luz Backend",
    readiness_checks: dict[str, callable] | None = None,
    services: Iterable[object] = (),
    config: ConfigStore | None = None,
) -> FastAPI:
    """services：随应用启动与停止的后台对象（有 start() / stop()，例如运行配置的目录监听）。
    config：运行配置；不传时每个请求取进程内的 config_store()。
    """
    settings = settings or get_settings()
    rules = list(rate_rules)
    metrics = Metrics()
    background = list(services)

    @asynccontextmanager
    async def _lifespan(_app: FastAPI):
        for service in background:
            service.start()
        try:
            yield
        finally:
            for service in reversed(background):
                service.stop()

    app = FastAPI(title=title, docs_url="/docs" if not settings.is_prod else None, redoc_url=None,
                  openapi_url="/openapi.json" if not settings.is_prod else None, lifespan=_lifespan)  # 生产不公开接口文档
    app.state.settings = settings
    app.state.metrics = metrics
    app.state.breakers = breakers or BreakerRegistry()
    app.state.config_store = config  # None = 进程内的 config_store()

    def _store() -> ConfigStore:
        return config or config_store()

    metrics.collectors.append(log_metrics_lines)
    metrics.collectors.append(lambda: _store().metrics_lines() if _store().loaded else [])

    if settings.cors_origins:
        app.add_middleware(CORSMiddleware, allow_origins=list(settings.cors_origins), allow_credentials=True,
                           allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"], allow_headers=["*"], max_age=600)

    def _summary(request: Request, status: int, seconds: float) -> None:
        path = request.url.path
        if status < 400 and path in PROBE_PATHS:
            return
        route = getattr(request.scope.get("route"), "path", "unmatched")
        reason = getattr(request.state, "error_reason", None)
        log_event(logger, ev.HTTP_REQUEST, logging.WARNING if status >= 500 else logging.INFO,
                  outcome="success" if status < 400 else "failure", reason=reason,
                  duration_ms=round(seconds * 1000, 1), user_ref=getattr(request.state, "user_ref", None),
                  store_id=getattr(request.state, "store_id", None),
                  **{"http.method": request.method, "http.route": route, "http.status": status,
                     "client.platform": _client(request.headers.get("X-Client")),
                     "client.version": _client_version(request.headers.get("X-Client-Version")),
                     "client.config_version": _client_version(request.headers.get("X-Config-Version"))})

    @app.middleware("http")
    async def _request_context(request: Request, call_next):
        started = time.perf_counter()
        request_id = new_request_id(request.headers.get("X-Request-Id"))
        trace_id, _parent = resolve_incoming(request.headers.get("traceparent"), request.headers.get("X-Trace-Id"))
        span_id = new_span_id()
        request.state.request_id = request_id
        request.state.trace_id = trace_id
        request.state.span_id = span_id
        snapshot = _store().current()  # 每个请求只取一次
        request.state.config_snapshot = snapshot
        token_r = request_id_var.set(request_id)
        token_t = trace_id_var.set(trace_id)
        token_s = span_id_var.set(span_id)
        token_u = user_id_var.set(None)  # 由鉴权依赖 bind_identity 填写
        token_st = store_id_var.set(None)
        try:
            with use_snapshot(snapshot, channel=_client(request.headers.get("X-Client")),
                              client_version=_client_version(request.headers.get("X-Client-Version"))):
                response = None
                cfg = snapshot.config
                content_length = request.headers.get("content-length")
                if content_length and content_length.isdigit() and int(content_length) > cfg.http.max_body_kb * 1024:
                    request.state.error_reason = "body_too_large"
                    response = _envelope(ErrorCode.BAD_REQUEST, "request body too large", status=413)
                if (response is None and request.method in WRITE_METHODS and request.url.path.startswith("/api/v1/app/")
                        and config_for().features.maintenance_mode):
                    request.state.error_reason = "maintenance"
                    response = _envelope(ErrorCode.SERVICE_UNAVAILABLE, "service under maintenance, please retry later",
                                         status=503, retry_after=60)
                if response is None:
                    for rule in rules:
                        if not rule.matches(request.url.path, request.method):
                            continue
                        key = rule.key_fn(request)
                        if key is None:
                            continue
                        if not rule.limiter.allow(f"{rule.name}:{key}"):
                            metrics.inc(f'rate_limited_total{{rule="{rule.name}"}}')
                            request.state.error_reason = "rate_limited"
                            log_event(logger, ev.SECURITY_EXCESS_RATE_LIMIT_EXCEEDED, logging.WARNING, outcome="failure",
                                      rule=rule.name, source_ip=client_ip(request), **{"http.method": request.method})
                            response = _envelope(ErrorCode.RATE_LIMITED, "too many requests, please retry later",
                                                 status=429, retry_after=rule.retry_after)
                            break
                if response is None:
                    try:
                        response = await call_next(request)
                    except Exception as exc:  # noqa: BLE001 - 未处理异常：在请求上下文里记一次（带堆栈），返回 5000
                        request.state.error_reason = "internal"
                        log_event(logger, ev.APP_UNHANDLED_ERROR, logging.ERROR, exc_info=exc, outcome="failure",
                                  **{"http.method": request.method,
                                     "http.route": getattr(request.scope.get("route"), "path", "unmatched")})
                        metrics.inc("unhandled_errors_total")
                        response = _envelope(ErrorCode.INTERNAL, "internal error", {"trace_id": trace_id}, status=500)
                response.headers["X-Request-Id"] = request_id
                response.headers["X-Trace-Id"] = trace_id
                response.headers["traceparent"] = format_traceparent(trace_id, span_id)
                for name, value in SECURITY_HEADERS.items():
                    response.headers.setdefault(name, value)
                # 未匹配路由（扫描、404）统一记为 unmatched，避免原始路径让标签无限增长
                route = getattr(request.scope.get("route"), "path", "unmatched")
                elapsed = time.perf_counter() - started
                metrics.observe(request.method, route, response.status_code, elapsed)
                _summary(request, response.status_code, elapsed)
                return response
        finally:
            store_id_var.reset(token_st)
            user_id_var.reset(token_u)
            span_id_var.reset(token_s)
            trace_id_var.reset(token_t)
            request_id_var.reset(token_r)

    @app.exception_handler(AppError)
    async def _app_error(request: Request, exc: AppError) -> JSONResponse:
        data = exc.data
        reason = data.get("reason") if isinstance(data, dict) and isinstance(data.get("reason"), str) else None
        request.state.error_reason = reason or exc.code.name.lower()
        if exc.code >= 5000:
            data = {"trace_id": getattr(request.state, "trace_id", None), **(data if isinstance(data, dict) else {})}
            log_event(logger, ev.APP_ERROR, logging.WARNING, outcome="failure", reason=exc.code.name.lower(),
                      code=int(exc.code), **{"http.route": getattr(request.scope.get("route"), "path", "unmatched")})
        return _envelope(exc.code, exc.message, data, status=exc.status, retry_after=exc.retry_after)

    @app.exception_handler(RequestValidationError)
    async def _validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        errors = [{"field": ".".join(str(p) for p in e.get("loc", []) if p not in ("body", "query", "path")),
                   "message": e.get("msg", "invalid")} for e in exc.errors()]
        request.state.error_reason = "invalid_request"
        log_event(logger, ev.SECURITY_INPUT_VALIDATION_FAIL, logging.WARNING, outcome="failure",
                  invalid_fields=[e["field"] for e in errors][:20], source_ip=client_ip(request),
                  **{"http.route": getattr(request.scope.get("route"), "path", "unmatched")})
        return _envelope(ErrorCode.BAD_REQUEST, "invalid request", {"errors": errors}, status=400)

    @app.exception_handler(Exception)
    async def _unexpected(request: Request, exc: Exception) -> JSONResponse:
        # 正常情况下请求中间件已经处理；这里兜住中间件之外（如 CORS 层）抛出的异常
        trace_id = getattr(request.state, "trace_id", None)
        token = trace_id_var.set(trace_id)
        try:
            log_event(logger, ev.APP_UNHANDLED_ERROR, logging.ERROR, exc_info=exc, outcome="failure")
        finally:
            trace_id_var.reset(token)
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
        checks["config_version"] = _store().current().version if _store().loaded else "not loaded"
        return JSONResponse(status_code=200 if ok else 503,
                            content={"code": 0 if ok else int(ErrorCode.SERVICE_UNAVAILABLE), "message": "ok" if ok else "not ready", "data": checks})

    @app.get("/metrics", tags=["system"], response_class=PlainTextResponse, include_in_schema=False)
    def metrics_endpoint(request: Request) -> PlainTextResponse:
        """只对内网或持有 LUZ_METRICS_TOKEN 的抓取方开放；对外返回 404，不暴露端点存在。"""
        allowed = bearer_matches(request, settings.metrics_token) or _in(
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
