# 模块：00-framework（基础框架）`luzback`

| 项 | 内容 |
|---|---|
| 状态 | 已实现，测试 7 组（核心、列表、持久化、弹性与应用、运行配置、模块配置、日志与链路）|
| 代码 | `code/luzback/`，测试 `tests/` |
| 依赖 | fastapi、sqlalchemy 2、pydantic 2；可选 cryptography（字段加密、支付 v3）、argon2-cffi（口令）、redis（限流 / 锁 / 队列）、watchfiles（配置目录监听，缺省时轮询）|

## 1 职责与边界

| 负责 | 不负责 |
|---|---|
| 部署参数与秘密（环境变量）、生产安全断言、启动时扫描环境变量 | 任何业务规则 |
| 运行配置：字段说明、加载与校验、版本、覆盖规则、热加载与目录监听、请求内快照 | 运行配置的接口与加载记录（23-app-settings） |
| 结构化日志：统一字段、脱敏、队列与丢弃计数、事件名登记；W3C 链路传播 | 日志采集与留存（运维） |
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
| `config.py` | 部署参数与秘密（环境变量：存储地址、连接池、令牌与字段密钥、微信、网段、运维令牌、模块装配、配置目录）；运行参数已迁到 `config/runtime/*.toml` | `get_settings()`, `reload_settings()`, `Settings.assert_production_safe()`（框架项 + 模块登记的检查）, `register_production_check(name, fn)`, `service_secret(name)`, `env_warnings()`（已迁走与不认识的变量）, `register_env_names(...)` |
| `runtime_config.py` | 运行参数的字段说明（唯一来源）：分组、类型、默认值、范围、单位；元数据 reloadable、client_visible、risk、owner、scope、platforms、client_since；覆盖规则模型 | `RuntimeConfig`, `RuntimeFile`, `Override`, `setting(...)`, `registry()`, `version_tuple()` |
| `runtime_loader.py` | 读 base + 环境文件、合并、整份校验成不可变快照、版本摘要、覆盖规则与用户分桶、加载串行加锁、热加载失败保留旧快照、待重启的键、监听者、目录监听（watchfiles / 轮询，防抖 1.5 秒）、进程启动 | `config_store()`, `ConfigStore.load / reload / status / add_listener`, `current_config()`, `config_for(store_id=, user_ref=)`, `use_snapshot()`, `ConfigWatcher`, `start_process()`, `watcher_for()` |
| `config_check.py` | 命令行：按字段说明加载每个环境文件并打印版本（CI、发布前） | `python -m luzback.config_check --env all` |
| `module_config.py` | 模块配置的统一读法：部署参数与钩子（环境变量 / 代码）+ 运行配置里该模块分组的同名运行参数（按快照缓存、热加载生效、共用分组）；`set` 的运行参数只给测试（转成测试用运行配置） | `ModuleConfig(group, factory, shared=)`、`.get()` / `.set()` / `.reload()` / `.reset()`、`runtime_default(group, key)` |
| `errors.py` | 错误码（SPEC §4）与 `AppError` | `ErrorCode`, `AppError(code, message, data, retry_after)`, `not_found()`, `conflict()`, `rule()` |
| `db.py` | 引擎、会话、`Base`（约束命名）、`BigIdMixin`、`TimestampMixin`、`SoftDeleteMixin`、`transaction()`、`utcnow()`、`supports_skip_locked()` | `init_engine(url, pool_size, statement_timeout_ms)`, `get_db()` |
| `responses.py` | 信封与分页包装 | `ok()`, `cursor_page()`, `offset_page()` |
| `security.py` | JWT（iss / jti / kind / leeway）、口令哈希与策略、TOTP、服务签名与 nonce 去重、脱敏、安全头、请求 / trace id | `create_token`, `decode_token`, `hash_password`, `verify_password`, `check_password_policy`（NIST 800-63B-4：长度 15 / MFA 时 8、上限 128、黑名单与上下文，无组成规则）, `totp_*`, `service_signature`, `verify_service_signature`, `NonceStore`, `mask_phone` |
| `crypto.py` | AES-256-GCM 字段加密与哈希查询；微信 APIv3 解密 | `FieldCipher(key_b64).encrypt/decrypt/lookup_hash`, `aes_gcm_decrypt` |
| `ratelimit.py` | 令牌桶、固定窗口、Redis 令牌桶、规则；阈值取运行配置的限流器（热加载生效） | `TokenBucketLimiter`, `FixedWindowLimiter`, `RedisTokenBucket`, `RateLimitRule`, `require_rate_limit`, `token_bucket(fn)`, `fixed_window(fn)`, `ConfiguredLimiter` |
| `logging.py` | 一行一条 JSON（ts UTC 毫秒 Z、service、env、version、instance、config_version、trace_id、span_id、request_id、event、outcome、error.*）；字段名黑名单 + 正则脱敏；控制字符转义、字段截断 512、堆栈 2000；QueueHandler + QueueListener，有上限，满时丢 DEBUG / INFO 并计数；安全事件不受级别影响；日志自身异常不外抛 | `configure_logging`, `log_event`, `bind_log_fields`, `redact`, `sanitize`, `is_sensitive_key`, `set_levels`, `flush_logging`, `dropped_total`, `trace_id_var`, `span_id_var` |
| `log_events.py` | 事件名登记：小写“领域.动作”；安全事件 `security.` + OWASP 词汇 | `HTTP_REQUEST`, `CONFIG_RELOAD_REJECTED`, `JOB_FINISHED`, `SECURITY_AUTHZ_FAIL` …, `EVENTS`, `is_security` |
| `tracing.py` | W3C traceparent 解析与生成、兼容旧 X-Trace-Id、对下游与消息写链路头、消费端接上、定时任务新建 trace | `parse_traceparent`, `resolve_incoming`, `outgoing_headers`, `inject`, `httpx_request_hook`, `trace_scope`, `new_trace`, `continue_trace` |
| `idempotency.py` | 幂等执行（作用域含操作者、指纹、僵尸接管、清理） | `run_idempotent(db, scope, key, fn, actor, fingerprint, require_key)`, `validate_key`, `purge_idempotency` |
| `outbox.py` | 发件箱事件（带 traceparent 与 trace_id）、退避投递（SKIP LOCKED；接上生产者的链路，每个事件取一次配置快照）、消费去重、失败重放、worker | `enqueue_event`, `dispatch_pending`, `mark_processed`, `requeue_failed`, `OutboxWorker` |
| `mq.py` | 消息抽象：内存与 Redis Streams；消息头带 traceparent，消费时接上 | `InMemoryBroker`, `RedisStreamsBroker`, `outbox_to_broker` |
| `resilience.py` | 瞬时错误、熔断器与注册表、重试、超时（在调用方上下文副本里执行，链路与配置快照不丢）、舱壁、组合调用 | `CircuitBreaker`, `BreakerRegistry`, `retry_call`, `call_with_timeout`, `Bulkhead`, `resilient(name, fn, timeout_s, attempts, breaker, bulkhead, fallback)` |
| `audit.py` | 审计表与写入 | `AuditLog`, `write_audit`, `diff` |
| `pagination.py` | 签名游标、页码、排序白名单 | `encode_cursor`, `decode_cursor`, `parse_limit`, `parse_page`, `parse_sort` |
| `scheduler.py` | 任务、注册表、锁、运行记录、调度循环；每次运行新建 trace、带 job 与 run_id、取一次配置快照、写 job.finished | `Job`, `JobRegistry`, `InMemoryLock`, `RedisLock`, `run_job`, `Scheduler` |
| `contracts.py` | 跨模块数据结构 | `Quote`, `QuoteLine`, `allocate`, `RangeResult`, `PaymentInfo`, `RefundInfo` |
| `app.py` | 应用工厂与系统端点；`bind_identity(request, user_ref, store_id)` 供鉴权依赖写请求上下文；请求中间件：traceparent / X-Request-Id / X-Trace-Id、每个请求一份配置快照、请求体上限、维护模式、限流（命中写安全事件）、未处理异常记一次、每个请求一条 `http.request` 汇总行、响应头 traceparent；/metrics 含日志丢弃与配置版本 | `create_app(routers, settings, rate_rules, breakers, readiness_checks, services, config)`, `idempotency_key`, `client_ip`, `is_internal_request`, `ops_access`, `Metrics` |
| `testing.py` | 内存会话、测试应用（可传 rate_rules / breakers / config；每个应用独立 `app.state.metrics`，测试直接读计数，/metrics 端点只对内网或令牌开放）、可控时钟、测试用运行配置与运行参数覆盖（每个测试结束由 backend/conftest.py 撤销） | `memory_session`, `make_test_app`, `FakeClock`, `make_config_store(dir, env, values)`, `dump_toml`, `override_runtime(...)`, `reset_runtime_overrides()` |

## 3 用法

```python
from luzback import create_app, get_settings
from luzback.ratelimit import RateLimitRule, fixed_window, token_bucket
from luzback.runtime_loader import start_process, watcher_for
from luzback.app import client_ip

settings = get_settings()
store = start_process(settings)          # 日志 + 运行配置（校验失败逐条打印并以 2 退出）
settings.assert_production_safe()
rules = [  # 阈值取运行配置 [rate_limit]，热加载后立即生效
    RateLimitRule("ip", token_bucket(lambda cfg: cfg.rate_limit.ip_per_s), key_fn=client_ip),
    RateLimitRule("login", fixed_window(lambda cfg: cfg.rate_limit.login_per_min), path_prefix="/api/v1/app/auth/login",
                  methods=("POST",), key_fn=client_ip, retry_after=60),
]
app = create_app(routers=[...], settings=settings, rate_rules=rules, services=[watcher_for(store)], config=store)
```

读运行参数（请求、任务开始时已取好快照，处理中途热加载不影响）：

```python
from luzback import config_for, current_config
timeout_ms = current_config().db.statement_timeout_ms
if config_for(store_id=order.store_id, user_ref=str(user_id)).features.maintenance_mode: ...
```

写日志（事件名先在 `log_events.py` 登记；结构化字段不拼进消息；秘密按字段名自动打码）：

```python
from luzback import log_events as ev
from luzback.logging import log_event
log_event(log, ev.JOB_FINISHED, outcome="success", duration_ms=12)
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
cfg = current_config()
breakers = BreakerRegistry(window_seconds=cfg.breaker.window_s, min_failures=cfg.breaker.min_failures,
                           fail_rate=cfg.breaker.fail_rate, open_seconds=cfg.breaker.open_s)
result = resilient("map_route", lambda: client.route(...), timeout_s=3, attempts=2,
                   breaker=breakers.get("map_route"), fallback=lambda exc: straight_line_estimate())
```

## 4 规则

1. 生产启动必须调用 `assert_production_safe()`；任何开发默认密钥、SQLite、缺 Redis / 字段密钥都拒绝启动。
2. 所有时间 `DateTime(timezone=True)` + `utcnow()`；SQLite 读回 naive，比较前补 UTC。
3. `run_idempotent` 的 `fn` 不 commit；成功由执行器提交，失败回滚并释放键。
4. 发件箱事件与业务写同事务；worker 投递失败按退避表重试，5 次后 failed 并告警；消费者用 `mark_processed` 去重。
5. 只重试 `TransientError`；业务错误（`AppError`）不计入熔断、不重试。
6. 中间件顺序：请求上下文（链路、配置快照）→ 请求体上限 → 维护模式 → 限流 → 路由；异常顺序：`AppError` → 校验错误（写 `security.input_validation_fail`）→ 未知异常（在请求上下文里记一次带堆栈，返回 5000 + trace_id，不回显内部信息）。
7. 日志与指标不含 PII：字段名黑名单（password、token、secret、authorization、cookie、sign、cvv、card_no、api_key、session_key、sms_code……）整值打码，`redact` 再对手机号、证件号、Bearer、JWT 与 `password=…` 一类键值兜底。
8. 五类设置分开（`references/config-logging.md` §1）：秘密与部署参数在环境变量（改了重启）；运行参数在 `config/runtime/*.toml`（热加载，不在后台展示）；经营与运营内容、前台文案与展示在数据库（后台维护；`admin_visible` 为否的文案与展示由开发经种子维护）。环境变量不覆盖运行参数。
9. 运行配置整份校验成快照再替换；启动失败退出、热加载失败保留旧快照并告警；请求、任务、事件投递开始时各取一次快照。
10. 每个请求一条 `http.request` 汇总行（路由模板、状态、耗时、结果）；探测接口成功不记；事件名只用 `log_events.py` 的常量（测试扫描）。
11. 链路：入口沿用合法 traceparent（新 span）或旧 X-Trace-Id，否则新建；响应头返回 X-Request-Id、X-Trace-Id、traceparent；发件箱事件与消息带链路，消费端接上；定时任务每次新建 trace；调用内部下游的 HTTP 用 `httpx_request_hook` / `inject` 带上。

## 5 测试

`tests/test_core.py`（配置、错误码、JWT、口令、TOTP、服务签名、脱敏、字段加密、限流、游标与排序）、`tests/test_listing.py`、`tests/test_persistence.py`（幂等重放 / 指纹 / 僵尸 / 清理、发件箱退避 / 失败 / 重放 / 去重 / 兼容、队列消费组、审计、调度与锁）、`tests/test_resilience_app.py`（熔断状态机、重试、超时、回退、舱壁、应用信封 / 头 / 413 / 限流 / 5000 / readyz / metrics）、`tests/test_runtime_config.py`（字段说明与 base.toml 一致、三个环境文件可加载、坏类型 / 超范围 / 拼错键 / 拼错表 / 跨字段、秘密打码、启动失败退出码、版本摘要、热加载被拒保留旧值、待重启、进行中的请求用旧值、覆盖规则与分桶、目录监听、环境变量扫描、并发重载）、`tests/test_module_config.py`（运行参数只来自运行配置、热加载后模块读到新值、进行中的请求用旧值、测试覆盖与撤销、生产不能用测试覆盖、旧环境变量名报去处、模块分组的跨字段校验）、`tests/test_logging_tracing.py`（字段齐全、防伪造、脱敏、控制字符、截断、队列满丢弃、日志异常不外抛、生产 raiseExceptions、安全事件不受级别影响、事件名登记、traceparent 解析与传播、汇总行、发件箱与消息、定时任务、超时线程上下文）。

运行：`cd backend && python -m pytest -q 00-framework`。
