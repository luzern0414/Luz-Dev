"""日志事件名登记（references/config-logging.md §3.4，OBS-001、OBS-008）。

- 事件名用小写“领域.动作”；结果放 outcome（success / failure / unknown），不另造 _success / _fail 两个名字。
- 新增事件名在这里登记并经代码评审；日志调用处只用这里的常量，不临时起名（tests 会扫描 log_event 的字面量）。
- 安全事件用 `security.` 前缀加 OWASP Logging Vocabulary 的名称：日志自动带 category=security，
  单独成流并告警；日志队列满时也不丢。

    from luzback.log_events import ORDER_CREATED
    log_event(log, ORDER_CREATED, order_no=order.order_no, amount_cents=order.total_cents)
"""
from __future__ import annotations

# ---------- 请求与应用 ----------
HTTP_REQUEST = "http.request"                      # 每个请求结束时的汇总行（路由模板、状态、耗时、结果）
APP_STARTED = "app.started"
APP_MODULE_MOUNTED = "app.module_mounted"
APP_ERROR = "app.error"                            # 5xxx 的 AppError（上游失败、熔断、维护）
APP_UNHANDLED_ERROR = "app.unhandled_error"        # 未处理异常：只在全局处理器记一次并带堆栈

# ---------- 日志自身 ----------
LOG_FORMAT_FAILED = "log.format_failed"

# ---------- 运行配置（config-logging.md §2）----------
CONFIG_LOADED = "config.loaded"                    # 启动加载成功
CONFIG_RELOADED = "config.reloaded"                # 热加载成功并替换快照
CONFIG_UNCHANGED = "config.unchanged"              # 内容与当前版本相同
CONFIG_RELOAD_REJECTED = "config.reload_rejected"  # 热加载校验失败，保留旧快照（告警）
CONFIG_STARTUP_REJECTED = "config.startup_rejected"  # 启动校验失败，进程退出（CRITICAL）
CONFIG_RESTART_REQUIRED = "config.restart_required"  # 不能热加载的键变化，待重启生效
CONFIG_UNKNOWN_ENV = "config.unknown_env"          # 不认识或已迁走的环境变量
CONFIG_WATCH_STARTED = "config.watch_started"
CONFIG_WATCH_FAILED = "config.watch_failed"
CONFIG_LISTENER_FAILED = "config.listener_failed"
CONFIG_LOAD_RECORD_FAILED = "config.load_record_failed"

# ---------- 任务、发件箱与事件 ----------
JOB_FINISHED = "job.finished"
SCHEDULER_STARTED = "scheduler.started"
WORKER_STARTED = "worker.started"
OUTBOX_DISPATCH_FAILED = "outbox.dispatch_failed"
OUTBOX_DEAD_LETTERED = "outbox.dead_lettered"
EVENT_SUBSCRIBER_FAILED = "event.subscriber_failed"

# ---------- 文案与展示（23-app-settings）----------
CONTENT_TEXT_UPDATED = "content.text_updated"
CONTENT_DISPLAY_UPDATED = "content.display_updated"
CONTENT_SEED_SYNCED = "content.seed_synced"

# ---------- 安全事件（OWASP Logging Vocabulary）----------
SECURITY_AUTHN_LOGIN_SUCCESS = "security.authn_login_success"
SECURITY_AUTHN_LOGIN_FAIL = "security.authn_login_fail"
SECURITY_AUTHN_LOGIN_LOCK = "security.authn_login_lock"
SECURITY_AUTHN_TOKEN_REUSE = "security.authn_token_reuse"
SECURITY_SESSION_USE_AFTER_EXPIRE = "security.session_use_after_expire"
SECURITY_AUTHZ_FAIL = "security.authz_fail"
SECURITY_INPUT_VALIDATION_FAIL = "security.input_validation_fail"
SECURITY_EXCESS_RATE_LIMIT_EXCEEDED = "security.excess_rate_limit_exceeded"

SECURITY_PREFIX = "security."

EVENTS: frozenset[str] = frozenset(
    value for name, value in dict(globals()).items()
    if name.isupper() and isinstance(value, str) and "." in value and not value.endswith(".")
)


def is_registered(event: str | None) -> bool:
    return bool(event) and event in EVENTS


def is_security(event: str | None) -> bool:
    return bool(event) and str(event).startswith(SECURITY_PREFIX)
