# 模块：00-framework（基础框架 `luzback`）

| 项 | 内容 |
|---|---|
| 状态 | 已实现，测试 3 组（核心、持久化、弹性与应用）|
| 代码 | `code/luzback/`，测试 `tests/` |
| 依赖 | fastapi、sqlalchemy 2、pydantic 2；可选 cryptography（字段加密、支付 v3）、argon2-cffi（口令）、redis（限流 / 锁 / 队列）|

## 1 职责与边界

| 负责 | 不负责 |
|---|---|
| 配置与生产安全断言 | 任何业务规则 |
| 数据库引擎、会话、模型基类与 mixin、约束命名 | 迁移脚本内容（`backend/alembic/`）|
| 错误码与响应信封 | 业务错误文案 |
| JWT、口令哈希、TOTP、服务间签名、脱敏、字段加密 | 账号表与登录流程（01-identity、22-admin-rbac）|
| 幂等执行器、发件箱、消费去重、消息队列抽象 | 事件的业务含义 |
| 熔断 / 重试 / 超时 / 舱壁 | 具体外部客户端 |
| 多规则限流、请求体上限、安全头、CORS、全局异常、trace、指标、健康检查 | 网关 |
| 审计表与写入函数、游标分页、定时任务注册与锁、测试工具 | — |

## 2 文件

| 文件 | 内容 | 关键 API |
|---|---|---|
| `config.py` | 环境变量配置（存储、令牌、加密、微信、业务默认值、限流、弹性、请求限制、功能开关） | `get_settings()`, `reload_settings()`, `Settings.assert_production_safe()`（框架项 + 模块登记的检查）, `register_production_check(name, fn)`, `service_secret(name)` |
| `errors.py` | 错误码（SPEC §4）与 `AppError` | `ErrorCode`, `AppError(code, message, data, retry_after)`, `not_found()`, `conflict()`, `rule()` |
| `db.py` | 引擎、会话、`Base`（约束命名）、`BigIdMixin`、`TimestampMixin`、`SoftDeleteMixin`、`transaction()`、`utcnow()`、`supports_skip_locked()` | `init_engine(url, pool_size, statement_timeout_ms)`, `get_db()` |
| `responses.py` | 信封与分页包装 | `ok()`, `cursor_page()`, `offset_page()` |
| `security.py` | JWT（iss / jti / kind / leeway）、口令哈希与策略、TOTP、服务签名与 nonce 去重、脱敏、安全头、请求 / trace id | `create_token`, `decode_token`, `hash_password`, `verify_password`, `check_password_policy`（NIST 800-63B-4：长度 15 / MFA 时 8、上限 128、黑名单与上下文，无组成规则）, `totp_*`, `service_signature`, `verify_service_signature`, `NonceStore`, `mask_phone` |
| `crypto.py` | AES-256-GCM 字段加密与哈希查询；微信 APIv3 解密 | `FieldCipher(key_b64).encrypt/decrypt/lookup_hash`, `aes_gcm_decrypt` |
| `ratelimit.py` | 令牌桶、固定窗口、Redis 令牌桶、规则 | `TokenBucketLimiter`, `FixedWindowLimiter`, `RedisTokenBucket`, `RateLimitRule`, `require_rate_limit` |
| `logging.py` | JSON 日志、contextvar、脱敏 | `configure_logging`, `log_event`, `redact`, `trace_id_var` |
| `idempotency.py` | 幂等执行（作用域含操作者、指纹、僵尸接管、清理） | `run_idempotent(db, scope, key, fn, actor, fingerprint, require_key)`, `validate_key`, `purge_idempotency` |
| `outbox.py` | 发件箱事件、退避投递（SKIP LOCKED）、消费去重、失败重放、worker | `enqueue_event`, `dispatch_pending`, `mark_processed`, `requeue_failed`, `OutboxWorker` |
| `mq.py` | 消息抽象：内存与 Redis Streams | `InMemoryBroker`, `RedisStreamsBroker`, `outbox_to_broker` |
| `resilience.py` | 瞬时错误、熔断器与注册表、重试、超时、舱壁、组合调用 | `CircuitBreaker`, `BreakerRegistry`, `retry_call`, `call_with_timeout`, `Bulkhead`, `resilient(name, fn, timeout_s, attempts, breaker, bulkhead, fallback)` |
| `audit.py` | 审计表与写入 | `AuditLog`, `write_audit`, `diff` |
| `pagination.py` | 签名游标、页码、排序白名单 | `encode_cursor`, `decode_cursor`, `parse_limit`, `parse_page`, `parse_sort` |
| `scheduler.py` | 任务、注册表、锁、运行记录、调度循环 | `Job`, `JobRegistry`, `InMemoryLock`, `RedisLock`, `run_job`, `Scheduler` |
| `contracts.py` | 跨模块数据结构 | `Quote`, `QuoteLine`, `allocate`, `RangeResult`, `PaymentInfo`, `RefundInfo` |
| `app.py` | 应用工厂与系统端点 | `create_app(routers, settings, rate_rules, breakers, readiness_checks)`, `idempotency_key`, `client_ip`, `Metrics` |
| `testing.py` | 内存会话、测试应用（可传 rate_rules / breakers；每个应用独立 `app.state.metrics`，测试直接读计数，/metrics 端点只对内网或令牌开放）、可控时钟 | `memory_session`, `make_test_app`, `FakeClock` |

## 3 用法

```python
from luzback import create_app, get_settings
from luzback.ratelimit import FixedWindowLimiter, RateLimitRule, TokenBucketLimiter
from luzback.app import client_ip

settings = get_settings()
settings.assert_production_safe()
rules = [
    RateLimitRule("ip", TokenBucketLimiter(settings.rate_ip_per_second, settings.rate_ip_per_second * 2), key_fn=client_ip),
    RateLimitRule("login", FixedWindowLimiter(settings.rate_login_per_minute, 60), path_prefix="/api/v1/app/auth/login",
                  methods=("POST",), key_fn=client_ip, retry_after=60),
]
app = create_app(routers=[...], settings=settings, rate_rules=rules)
```

写接口：

```python
@router.post("/orders")
def create(body: CreateIn, request: Request, key: str = Depends(idempotency_key), user_id: int = Depends(current_user), db=Depends(get_db)):
    fingerprint = sha256_hex(body.model_dump_json())
    return ok(run_idempotent(db, "order.create", key, lambda: service.create(db, user_id, body), actor=f"user:{user_id}",
                             fingerprint=fingerprint, require_key=True))
```

外部调用：

```python
breakers = BreakerRegistry(min_failures=settings.breaker_min_failures, fail_rate=settings.breaker_fail_rate, open_seconds=settings.breaker_open_seconds)
result = resilient("map_route", lambda: client.route(...), timeout_s=settings.http_timeout_read, attempts=2,
                   breaker=breakers.get("map_route"), fallback=lambda exc: straight_line_estimate())
```

## 4 规则

1. 生产启动必须调用 `assert_production_safe()`；任何开发默认密钥、SQLite、缺 Redis / 字段密钥都拒绝启动。
2. 所有时间 `DateTime(timezone=True)` + `utcnow()`；SQLite 读回 naive，比较前补 UTC。
3. `run_idempotent` 的 `fn` 不 commit；成功由执行器提交，失败回滚并释放键。
4. 发件箱事件与业务写同事务；worker 投递失败按退避表重试，5 次后 failed 并告警；消费者用 `mark_processed` 去重。
5. 只重试 `TransientError`；业务错误（`AppError`）不计入熔断、不重试。
6. 中间件顺序：请求上下文 → 请求体上限 → 限流 → 路由；异常顺序：`AppError` → 校验错误 → 未知异常（5000 + trace_id）。
7. 日志与指标不含 PII；`redact` 对手机号、身份证、Bearer 令牌脱敏。

## 5 测试

`tests/test_core.py`（配置、错误码、JWT、口令、TOTP、服务签名、脱敏、字段加密、限流、游标与排序）、`tests/test_persistence.py`（幂等重放 / 指纹 / 僵尸 / 清理、发件箱退避 / 失败 / 重放 / 去重 / 兼容、队列消费组、审计、调度与锁）、`tests/test_resilience_app.py`（熔断状态机、重试、超时、回退、舱壁、应用信封 / 头 / 413 / 限流 / 5000 / readyz / metrics）。

运行：`cd backend && python -m pytest -q 00-framework`。
