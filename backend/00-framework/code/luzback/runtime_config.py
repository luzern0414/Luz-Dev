"""运行配置的字段说明（references/config-logging.md §2.2，CONF-001、OPS-005）：类型、默认值、范围与元数据的唯一来源。

配置文件在 `backend/config/runtime/`：`base.toml` 写全部键与默认值，`dev.toml`、`staging.toml`、`prod.toml` 只写差异；
加载、校验、热加载与覆盖规则见 `runtime_loader.py`。这里登记的键与 `base.toml` 必须一一对应（测试检查缺键、多键）。

每个键用 `setting(...)` 登记，元数据放在 `Field(json_schema_extra=...)`：

| 元数据 | 含义 |
|---|---|
| reloadable | 能否热加载；否 = 变化时不应用，列为“待重启生效”（端口、并发、连接池、在启动时建好的对象） |
| client_visible | 是否经 `GET /api/v1/app/config` 下发客户端（默认否；秘密与内部地址一律不下发） |
| risk | low / high（支付、上传上限、维护模式、限流）：high 的改动要第二人评审、先按门店或用户灰度 |
| owner | 负责模块 |
| scope | 允许在环境文件的 `[overrides.<名称>]` 里按哪些维度写覆盖：store、channel、client_version、user_bucket |
| platforms、client_since | 下发给哪些端（miniprogram、app、h5）、从哪个客户端版本起认识这个键 |
| legacy_env | 迁移前读取的环境变量名；启动时仍设置着会打警告并指出这里的键 |

- 单位写进键名：`_ms`、`_s`、`_min`、`_kb`、`_mb`、`_per_s`、`_per_min`。
- 默认值也要通过校验（validate_default）；新功能开关默认关；文件里出现未登记的键一律报错（extra="forbid"）。
- 秘密、部署参数（地址、端口、进程数、连接池、商户号、密钥路径、回调地址）与经营内容（营业时间、起送价、文案）
  不在这里，见 §1 五类设置。
- 框架分组（logging、http、db、rate_limit、breaker、auth、upload、client、features）之后是各业务模块的分组，
  键名与模块配置数据类的属性同名，模块通过 luzback.module_config.ModuleConfig 读取。
"""
from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from typing import Annotated, Any, Literal, get_args, get_origin

from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic.fields import FieldInfo

LogLevel = Literal["debug", "info", "warning", "error"]
Platform = Literal["miniprogram", "app", "h5"]
PLATFORMS: tuple[str, ...] = get_args(Platform)
SCOPES: tuple[str, ...] = ("store", "channel", "client_version", "user_bucket")
SemVer = Annotated[str, Field(pattern=r"^\d{1,4}\.\d{1,4}\.\d{1,4}$")]
LoggerName = Annotated[str, Field(pattern=r"^[A-Za-z_][A-Za-z0-9_.]{0,99}$")]
OverrideName = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]{0,47}$")]


def setting(default: Any, description: str, *, reloadable: bool = True, client_visible: bool = False,
            risk: Literal["low", "high"] = "low", owner: str = "framework", scope: tuple[str, ...] = (),
            platforms: tuple[str, ...] = PLATFORMS, client_since: str = "0.0.0",
            legacy_env: str | tuple[str, ...] = (), **constraints: Any) -> Any:
    """登记一个运行参数：默认值、说明（用途与单位）、范围（ge / le / pattern）与元数据。"""
    unknown = set(scope) - set(SCOPES)
    if unknown:
        raise ValueError(f"unknown override scope {sorted(unknown)}")
    legacy = [legacy_env] if isinstance(legacy_env, str) else list(legacy_env)
    extra: dict[str, Any] = {"reloadable": reloadable, "client_visible": client_visible, "risk": risk, "owner": owner,
                             "scope": ["global", *scope], "legacy_env": legacy}
    if client_visible:
        extra["platforms"] = list(platforms)
        extra["client_since"] = client_since
    return Field(default, description=description, json_schema_extra=extra, **constraints)


class _Group(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)


class LoggingSettings(_Group):
    level: LogLevel = setting("info", description="根日志级别；生产默认 info，排障时临时调低，结束后还原")
    modules: dict[LoggerName, LogLevel] = setting({}, description="按 logger 名覆盖级别，例如 order = debug；排障结束后删除")
    queue_capacity: int = setting(10_000, ge=1_000, le=1_000_000, reloadable=False,
                                  description="日志队列容量（条）；满时丢 debug / info 并计 luz_log_dropped_total，warning 以上不丢")


class HttpSettings(_Group):
    max_body_kb: int = setting(256, ge=1, le=10_240, risk="high",
                               description="请求体上限（KB）；超过返回 413。文件走预签名直传，不受此限")


class DbSettings(_Group):
    statement_timeout_ms: int = setting(5_000, ge=100, le=600_000, reloadable=False,
                                        description="单条 SQL 语句超时（毫秒，仅 PostgreSQL）；建立连接时设置")


class RateLimitSettings(_Group):
    ip_per_s: float = setting(20.0, ge=1, le=10_000, risk="high",
                              description="每个 IP 访问 /api/ 的令牌补充速率（次/秒）；突发容量为 2 倍")
    user_write_per_s: float = setting(3.0, ge=0.1, le=1_000, risk="high",
                                      description="每个会员写操作（/api/v1/app/ 的 POST、PUT、PATCH、DELETE）速率（次/秒）；突发 2 倍")
    login_per_min: int = setting(5, ge=1, le=1_000, risk="high", owner="identity",
                                 description="登录接口每个 IP 每分钟次数（会员登录与后台登录各自计数）")
    admin_mfa_per_min: int = setting(10, ge=1, le=1_000, risk="high", owner="admin_rbac",
                                     description="后台第二因素校验每个 IP 每分钟次数")
    admin_reauth_per_min: int = setting(10, ge=1, le=1_000, risk="high", owner="admin_rbac",
                                        description="后台敏感操作重新认证每个 IP 每分钟次数")
    sms_send_per_min: int = setting(5, ge=1, le=1_000, risk="high", owner="identity",
                                    description="发送短信验证码接口每个 IP 每分钟次数（按手机号与每日上限见 [identity]）")
    refresh_per_min: int = setting(30, ge=1, le=1_000, risk="high", owner="identity",
                                   description="刷新令牌接口每个 IP 每分钟次数")
    lottery_claim_per_min: int = setting(5, ge=1, le=1_000, risk="high", owner="lottery",
                                         description="每个会员每分钟领奖次数")

    @model_validator(mode="after")
    def _user_not_above_ip(self) -> "RateLimitSettings":
        # 会员的请求同时计入 IP 令牌桶；会员写速率高于 IP 速率通常是把“每分钟”的数写进了“每秒”的键
        if self.user_write_per_s > self.ip_per_s:
            raise ValueError("rate_limit.user_write_per_s must not exceed rate_limit.ip_per_s")
        return self


class BreakerSettings(_Group):
    window_s: int = setting(60, ge=5, le=3_600, reloadable=False, description="熔断统计窗口（秒）；熔断器创建时读取")
    min_failures: int = setting(10, ge=1, le=10_000, reloadable=False, description="窗口内至少多少次失败才考虑熔断")
    fail_rate: float = setting(0.5, gt=0, le=1, reloadable=False, description="窗口内失败率达到多少熔断（0–1）")
    open_s: int = setting(30, ge=1, le=3_600, reloadable=False, description="熔断打开多久后放探测请求（秒）")


class AuthSettings(_Group):
    fresh_window_s: int = setting(900, ge=60, le=3_600, risk="high", owner="identity",
                                  description="资金与敏感操作要求最近认证的时间窗（秒）")
    service_signature_skew_s: int = setting(300, ge=30, le=900, risk="high", owner="identity",
                                            description="服务间签名允许的时钟偏差（秒）")


class UploadSettings(_Group):
    doc_max_mb: int = setting(5, ge=1, le=100, risk="high", owner="media", client_visible=True,
                              scope=("channel", "client_version"),
                              description="文档类文件上传上限（MB）；客户端显示并预检查，服务端按同一个值拒收；照片不设上限、后台压缩")


class ClientSettings(_Group):
    config_refresh_s: int = setting(300, ge=60, le=86_400, owner="app_settings", client_visible=True,
                                    scope=("channel", "client_version"),
                                    description="客户端回到前台后重新拉取配置的最短间隔（秒）")
    min_version_miniprogram: SemVer = setting("0.0.0", owner="app_settings", client_visible=True,
                                              platforms=("miniprogram",),
                                              description="小程序最低可用版本（x.y.z）；低于它提示重启更新")
    min_version_app: SemVer = setting("0.0.0", owner="app_settings", client_visible=True, platforms=("app",),
                                      description="App 最低可用版本（x.y.z）；低于它引导去应用商店更新")


class FeatureSettings(_Group):
    maintenance_mode: bool = setting(False, risk="high", owner="framework", client_visible=True,
                                     scope=("store", "channel", "client_version", "user_bucket"),
                                     description="维护模式：顾客端写接口返回 5030，客户端显示维护页；先按门店或渠道灰度")


class MembershipSettings(_Group):
    points_per_yuan: int = setting(1, ge=0, le=100, owner="membership", legacy_env="POINTS_PER_YUAN",
                                   description="订单完成后每 1 元返多少积分（再乘等级倍率）；商户要在后台自己改时改由会员模块的后台设置提供")
    points_valid_days: int = setting(365, "积分入账批次有效期（天，0 = 不过期）", ge=0, le=3_650, risk="high",
                                     owner="membership", legacy_env="POINTS_VALID_DAYS")


# ======================================================================== 模块分组
# 键名与模块配置数据类的属性同名（单位后缀用完整写法 _seconds / _minutes / _days / _cents），
# 模块用 luzback.module_config.ModuleConfig 把同名属性合并进它的配置对象；legacy_env 是迁移前的环境变量名，
# 启动时仍设置着会打警告并指出这里的键。

PositiveInts = Annotated[tuple[Annotated[int, Field(ge=1)], ...], Field(min_length=1, max_length=20)]
HourMinute = Annotated[str, Field(pattern=r"^([01]\d|2[0-3]):[0-5]\d$")]
MpState = Literal["developer", "trial", "formal"]
MpLang = Literal["zh_CN", "en_US", "zh_HK", "zh_TW"]


class IdentitySettings(_Group):
    access_ttl_seconds: int = setting(7_200, "会员 access 令牌有效期（秒）；新签发的令牌按新值", ge=60, le=86_400,
                                      risk="high", owner="identity", legacy_env="IDENTITY_ACCESS_TTL")
    jwt_leeway_seconds: int = setting(30, "校验令牌时允许的时钟偏差（秒，RFC 7519：最多几分钟）", ge=0, le=300,
                                      risk="high", owner="identity", legacy_env="IDENTITY_JWT_LEEWAY")
    refresh_idle_days: int = setting(14, "刷新令牌空闲超时（天），不大于绝对期限", ge=1, le=30, risk="high",
                                     owner="identity", legacy_env="IDENTITY_REFRESH_IDLE_DAYS")
    refresh_absolute_days: int = setting(30, "刷新令牌绝对期限（天，NIST AAL1 不超过 30）", ge=1, le=30, risk="high",
                                         owner="identity", legacy_env="IDENTITY_REFRESH_ABSOLUTE_DAYS")
    refresh_reuse_grace_seconds: int = setting(10, "刷新令牌轮换后旧令牌的宽限（秒），并发刷新不误判为重放", ge=0, le=60,
                                               owner="identity", legacy_env="IDENTITY_REFRESH_REUSE_GRACE")
    session_check: bool = setting(True, "每个请求校验服务端会话（登出、停用、注销即时生效）", risk="high", owner="identity",
                                  legacy_env="IDENTITY_SESSION_CHECK")
    sms_enabled: bool = setting(True, "短信验证码登录与绑定开关；关闭后客户端隐藏短信入口", client_visible=True,
                                owner="identity", legacy_env="IDENTITY_SMS_ENABLED")
    sms_ttl_seconds: int = setting(300, "短信验证码有效期（秒，不超过 10 分钟）", ge=60, le=600, owner="identity",
                                   legacy_env="IDENTITY_SMS_TTL")
    sms_max_attempts: int = setting(5, "同一验证码最多校验几次", ge=1, le=10, owner="identity",
                                    legacy_env="IDENTITY_SMS_MAX_ATTEMPTS")
    sms_resend_seconds: int = setting(60, "同一手机号重发间隔（秒）；客户端倒计时用同一个值", ge=10, le=600,
                                      client_visible=True, owner="identity", legacy_env="IDENTITY_SMS_RESEND_SECONDS")
    sms_daily_per_phone: int = setting(10, "每个手机号每天最多发几条验证码", ge=1, le=100, risk="high", owner="identity",
                                       legacy_env=("IDENTITY_SMS_DAILY_PER_PHONE", "SMS_DAILY_LIMIT"))
    sms_hourly_per_ip: int = setting(20, "每个 IP 每小时最多发几条验证码", ge=1, le=10_000, risk="high", owner="identity",
                                     legacy_env="IDENTITY_SMS_HOURLY_PER_IP")
    sms_daily_per_ip: int = setting(50, "每个 IP 每天最多发几条验证码", ge=1, le=100_000, risk="high", owner="identity",
                                    legacy_env="IDENTITY_SMS_DAILY_PER_IP")
    content_check: bool = setting(True, "昵称与头像走内容安全检测", owner="identity", legacy_env="IDENTITY_CONTENT_CHECK")
    content_check_fail_open: bool = setting(False, "检测服务不可用时是否放行（默认不放行）", risk="high", owner="identity",
                                            legacy_env="IDENTITY_CONTENT_CHECK_FAIL_OPEN")
    media_check_timeout_minutes: int = setting(60, "头像异步检测多久没有结果按失败处理（分钟）", ge=5, le=1_440,
                                               owner="identity", legacy_env="IDENTITY_MEDIA_CHECK_TIMEOUT_MINUTES")
    deletion_cooling_days: int = setting(15, "注销冷静期（天，个人信息须在 15 个工作日内删除）", ge=0, le=15, risk="high",
                                         owner="identity", legacy_env="IDENTITY_DELETION_COOLING_DAYS")
    sms_retention_days: int = setting(7, "短信验证码记录保留（天）", ge=1, le=365, owner="identity",
                                      legacy_env="IDENTITY_SMS_RETENTION_DAYS")
    session_retention_days: int = setting(180, "已失效会话保留（天）", ge=30, le=3_650, owner="identity",
                                          legacy_env="IDENTITY_SESSION_RETENTION_DAYS")

    @model_validator(mode="after")
    def _refresh_window(self) -> "IdentitySettings":
        if self.refresh_idle_days > self.refresh_absolute_days:
            raise ValueError("identity.refresh_idle_days must not exceed identity.refresh_absolute_days")
        return self


class OrderSettings(_Group):
    pay_timeout_minutes: int = setting(15, "待支付自动关闭时间（分钟；微信 time_expire 至少 1 分钟）", ge=1, le=1_440,
                                       risk="high", owner="order", legacy_env="ORDER_PAY_TIMEOUT_MINUTES")
    quote_ttl_seconds: int = setting(600, "报价有效期（秒）", ge=30, le=3_600, owner="order", legacy_env="ORDER_QUOTE_TTL")
    auto_complete_days: int = setting(7, "履约后自动完成（天）", ge=1, le=30, owner="order",
                                      legacy_env="ORDER_AUTO_COMPLETE_DAYS")
    max_lines: int = setting(50, "一单最多几种商品", ge=1, le=200, client_visible=True, owner="order",
                             legacy_env="ORDER_MAX_LINES")
    max_qty_per_line: int = setting(99, "每种商品最多买几件", ge=1, le=9_999, client_visible=True, owner="order",
                                    legacy_env="ORDER_MAX_QTY_PER_LINE")
    terms_required: bool = setting(True, "下单必须同意购买条款", risk="high", owner="order",
                                   legacy_env="ORDER_TERMS_REQUIRED")
    terms_version: str = setting("purchase-v1", "当前购买条款版本（与 27-legal 发布的版本一致）", min_length=1, max_length=32,
                                 risk="high", owner="order", legacy_env="ORDER_TERMS_VERSION")
    close_batch: int = setting(100, "超时关单任务每批几单", ge=1, le=10_000, owner="order", legacy_env="ORDER_CLOSE_BATCH")
    complete_batch: int = setting(200, "自动完成任务每批几单", ge=1, le=10_000, owner="order",
                                  legacy_env="ORDER_COMPLETE_BATCH")
    balance_payment_enabled: bool = setting(True, "允许用储值余额支付订单", client_visible=True, owner="order",
                                            legacy_env="ORDER_BALANCE_PAYMENT")
    shipping_upload_enabled: bool = setting(True, "发货后向微信上传发货信息", owner="order",
                                            legacy_env="ORDER_SHIPPING_UPLOAD")


class WechatPaySettings(_Group):
    timeout_prepay_s: float = setting(8.0, "下单接口超时（秒，不重试）", ge=1, le=30, risk="high", owner="wechat_pay",
                                      legacy_env="WECHAT_PAY_TIMEOUT_PREPAY")
    timeout_query_s: float = setting(5.0, "查单与查退款超时（秒）", ge=1, le=30, owner="wechat_pay",
                                     legacy_env="WECHAT_PAY_TIMEOUT_QUERY")
    timeout_close_s: float = setting(5.0, "关单超时（秒）", ge=1, le=30, owner="wechat_pay", legacy_env="WECHAT_PAY_TIMEOUT_CLOSE")
    timeout_refund_s: float = setting(10.0, "退款申请超时（秒，不重试，由任务重试）", ge=1, le=60, owner="wechat_pay",
                                      legacy_env="WECHAT_PAY_TIMEOUT_REFUND")
    timeout_bill_s: float = setting(30.0, "下载账单超时（秒）；HTTP 客户端创建时读取", ge=5, le=300, reloadable=False,
                                    owner="wechat_pay", legacy_env="WECHAT_PAY_TIMEOUT_BILL")
    query_attempts: int = setting(3, "查单最多尝试几次", ge=1, le=10, owner="wechat_pay", legacy_env="WECHAT_PAY_QUERY_ATTEMPTS")
    close_attempts: int = setting(2, "关单最多尝试几次", ge=1, le=10, owner="wechat_pay", legacy_env="WECHAT_PAY_CLOSE_ATTEMPTS")
    bulkhead: int = setting(30, "同时调用微信支付的并发上限（个）；首次调用时创建", ge=1, le=1_000, reloadable=False,
                            owner="wechat_pay", legacy_env="WECHAT_PAY_BULKHEAD")
    timestamp_skew_s: int = setting(300, "应答与回调签名的时间偏差上限（秒）；客户端创建时读取", ge=60, le=900,
                                    reloadable=False, risk="high", owner="wechat_pay", legacy_env="WECHAT_PAY_TIMESTAMP_SKEW")
    default_expire_minutes: int = setting(15, "支付单默认有效期（分钟），业务没给截止时间时用", ge=1, le=1_440, risk="high",
                                          owner="wechat_pay", legacy_env="WECHAT_PAY_EXPIRE_MINUTES")
    prepay_reuse_seconds: int = setting(6_900, "prepay_id 复用期（秒，官方 2 小时有效，留 5 分钟余量）", ge=0, le=7_200,
                                        owner="wechat_pay", legacy_env="WECHAT_PAY_PREPAY_REUSE_SECONDS")
    app_sync_min_interval_s: int = setting(3, "客户端轮询支付结果时同一单主动查单的最短间隔（秒）", ge=1, le=60,
                                           owner="wechat_pay", legacy_env="WECHAT_PAY_APP_SYNC_INTERVAL")
    query_schedule_s: PositiveInts = setting((5, 30, 60, 180, 300, 600, 1_800), "下单后主动查单的时间点（秒，官方方案一）",
                                             owner="wechat_pay", legacy_env="WECHAT_PAY_QUERY_SCHEDULE")
    auto_refund_orphans: bool = setting(True, "业务已关闭仍到账的支付自动退款", risk="high", owner="wechat_pay",
                                        legacy_env="WECHAT_PAY_AUTO_REFUND_ORPHANS")
    refund_max_times: int = setting(50, "一笔支付最多部分退款几次（官方上限 50）", ge=1, le=50, owner="wechat_pay",
                                    legacy_env="WECHAT_PAY_REFUND_MAX_TIMES")
    refund_min_interval_s: int = setting(60, "同一笔支付两次退款申请的最短间隔（秒，官方要求 1 分钟；0 = 不限）", ge=0, le=3_600,
                                         owner="wechat_pay", legacy_env="WECHAT_PAY_REFUND_MIN_INTERVAL")
    refund_window_days: int = setting(365, "支付后多少天内可以原路退款（天，官方 1 年）", ge=1, le=365, risk="high",
                                      owner="wechat_pay", legacy_env="WECHAT_PAY_REFUND_WINDOW_DAYS")
    refund_submit_max_attempts: int = setting(5, "退款申请最多尝试几次，之后转人工", ge=1, le=20, owner="wechat_pay",
                                              legacy_env="WECHAT_PAY_REFUND_MAX_ATTEMPTS")
    refund_query_fast_s: int = setting(60, "退款申请后快速查询的间隔（秒）", ge=10, le=600, owner="wechat_pay",
                                       legacy_env="WECHAT_PAY_REFUND_QUERY_FAST")
    refund_query_fast_window_s: int = setting(300, "快速查询持续多久（秒），之后按退避间隔", ge=60, le=3_600,
                                              owner="wechat_pay", legacy_env="WECHAT_PAY_REFUND_QUERY_FAST_WINDOW")
    refund_query_backoff_min: PositiveInts = setting((5, 10, 20, 30), "退款结果查询的退避间隔（分钟）", owner="wechat_pay",
                                                     legacy_env="WECHAT_PAY_REFUND_QUERY_BACKOFF")
    bill_ready_hour: int = setting(10, "每天几点后下载前一天的账单（时，0–23，北京时间）", ge=0, le=23, owner="wechat_pay",
                                   legacy_env="WECHAT_PAY_BILL_READY_HOUR")
    bill_retry_minutes: int = setting(30, "账单未就绪时多久后重试（分钟）", ge=5, le=720, owner="wechat_pay",
                                      legacy_env="WECHAT_PAY_BILL_RETRY_MINUTES")
    bill_max_days: int = setting(90, "最多下载多少天前的账单（天，官方 3 个月）", ge=1, le=90, owner="wechat_pay",
                                 legacy_env="WECHAT_PAY_BILL_MAX_DAYS")
    shipping_enabled: bool = setting(True, "小程序发货信息管理（上传发货信息）开关", owner="wechat_pay",
                                     legacy_env="WECHAT_PAY_SHIPPING_ENABLED")
    shipping_max_attempts: int = setting(8, "发货信息上传最多尝试几次", ge=1, le=50, owner="wechat_pay",
                                         legacy_env="WECHAT_PAY_SHIPPING_MAX_ATTEMPTS")


class AfterSalesSettings(_Group):
    no_reason_days: int = setting(7, "七日无理由退货期（天，不少于 7：网络购买商品七日无理由退货暂行办法第十条）", ge=7, le=30,
                                  risk="high", owner="after_sales", legacy_env="AFTER_SALE_NO_REASON_DAYS")
    window_days: int = setting(15, "质量等其他原因的申请窗口（天）", ge=1, le=365, owner="after_sales",
                               legacy_env="AFTER_SALE_WINDOW_DAYS")
    auto_approve_days: int = setting(0, "商家多少天未处理自动同意（天，0 = 关闭）", ge=0, le=30, risk="high",
                                     owner="after_sales", legacy_env="AFTER_SALE_AUTO_APPROVE_DAYS")
    return_ship_days: int = setting(7, "同意退货后买家寄回的期限（天）", ge=1, le=30, owner="after_sales",
                                    legacy_env="AFTER_SALE_RETURN_SHIP_DAYS")
    max_evidence: int = setting(9, "申请最多上传几张凭证图", ge=0, le=20, client_visible=True, owner="after_sales",
                                legacy_env="AFTER_SALE_MAX_EVIDENCE")
    refund_retry_seconds: int = setting(60, "退款失败后多久重试（秒）", ge=10, le=3_600, owner="after_sales",
                                        legacy_env="AFTER_SALE_REFUND_RETRY_SECONDS")
    job_batch: int = setting(200, "售后定时任务每批几条", ge=1, le=10_000, owner="after_sales",
                             legacy_env="AFTER_SALE_JOB_BATCH")


class PrepaidSettings(_Group):
    """储值（15-recharge）与会员卡（29-member-card）共用的预付卡参数。"""

    policy: Literal["conservative", "shanghai_fitness_2025", "shanghai_fitness_draft_2026", "beijing_2022",
                    "national_single_use_card", "unlimited"] = setting(
        "conservative", "预付卡监管口径预设（单次、累计上限与冷静期等）；连锁跨城市可按门店另行注入", risk="high",
        owner="wallet", legacy_env="LUZ_PREPAID_POLICY")
    lpr_1y_bp: int = setting(300, "合同成立时一年期 LPR（万分比），退款补偿利息按它计算；按公布值更新", ge=0, le=2_000,
                             risk="high", owner="wallet", legacy_env="LUZ_LPR_1Y_BP")
    stop_prepaid: bool = setting(False, "停止收取预付款（经营风险时）：储值与会员卡都停售，客户端隐藏入口", risk="high",
                                 client_visible=True, owner="wallet", legacy_env="LUZ_STOP_PREPAID")


class WalletSettings(_Group):
    split_policy: Literal["proportional", "principal_first"] = setting(
        "proportional", "消费时本金与赠送金的扣减顺序", risk="high", owner="wallet", legacy_env="WALLET_SPLIT_POLICY")
    pay_timeout_minutes: int = setting(15, "充值待支付自动关闭（分钟）", ge=1, le=1_440, owner="wallet",
                                       legacy_env="WALLET_PAY_TIMEOUT_MINUTES")
    refund_requires_approval: bool = setting(True, "储值退款需要后台审批", risk="high", owner="wallet",
                                             legacy_env="WALLET_REFUND_REQUIRES_APPROVAL")
    customer_refund_fee_bp: int = setting(0, "顾客原因退款的手续费（万分比）", ge=0, le=10_000, risk="high", owner="wallet",
                                          legacy_env="WALLET_CUSTOMER_REFUND_FEE_BP")
    adjust_max_cents: int = setting(50_000, "后台单次手工调整余额上限（分）", ge=0, le=10_000_000, risk="high",
                                    owner="wallet", legacy_env="WALLET_ADJUST_MAX_CENTS")
    custom_amount_enabled: bool = setting(False, "允许顾客输入自定义充值金额", client_visible=True, owner="wallet",
                                          legacy_env="WALLET_CUSTOM_AMOUNT")
    custom_amount_min_cents: int = setting(100, "自定义充值最低金额（分）", ge=1, le=10_000_000, client_visible=True,
                                           owner="wallet", legacy_env="WALLET_CUSTOM_AMOUNT_MIN_CENTS")
    original_refund_max_days: int = setting(365, "支付后多少天内可以原路退回（天）", ge=1, le=365, risk="high",
                                            owner="wallet", legacy_env="WALLET_ORIGINAL_REFUND_MAX_DAYS")


class MemberCardSettings(_Group):
    pay_timeout_minutes: int = setting(15, "购卡待支付自动关闭（分钟）", ge=1, le=1_440, owner="member_card",
                                       legacy_env="MEMBER_CARD_PAY_TIMEOUT_MINUTES")
    app_freeze_enabled: bool = setting(True, "顾客可在小程序自助停卡", client_visible=True, owner="member_card",
                                       legacy_env="MEMBER_CARD_APP_FREEZE")
    app_transfer_enabled: bool = setting(True, "顾客可在小程序自助转让", client_visible=True, owner="member_card",
                                         legacy_env="MEMBER_CARD_APP_TRANSFER")
    refund_requires_approval: bool = setting(True, "会员卡退款需要后台审批", risk="high", owner="member_card",
                                             legacy_env="MEMBER_CARD_REFUND_REQUIRES_APPROVAL")
    reminder_days: PositiveInts = setting((7, 3, 1), "到期前几天提醒（天，可多个）", owner="member_card",
                                          legacy_env="MEMBER_CARD_REMINDER_DAYS")
    original_refund_max_days: int = setting(365, "支付后多少天内可以原路退回（天）", ge=1, le=365, risk="high",
                                            owner="member_card", legacy_env="MEMBER_CARD_ORIGINAL_REFUND_MAX_DAYS")
    adjust_max_sessions: int = setting(50, "后台单次手工调整次数上限（次）", ge=1, le=10_000, risk="high",
                                       owner="member_card", legacy_env="MEMBER_CARD_ADJUST_MAX_SESSIONS")
    adjust_max_days: int = setting(366, "后台单次手工调整有效期上限（天）", ge=1, le=3_650, risk="high",
                                   owner="member_card", legacy_env="MEMBER_CARD_ADJUST_MAX_DAYS")
    replace_fee_cents: int = setting(0, "补卡工本费（分）", ge=0, le=100_000, risk="high", owner="member_card",
                                     legacy_env="MEMBER_CARD_REPLACE_FEE_CENTS")

    @model_validator(mode="after")
    def _sorted_reminders(self) -> "MemberCardSettings":
        ordered = tuple(sorted(set(self.reminder_days), reverse=True))
        if ordered != self.reminder_days:
            raise ValueError("member_card.reminder_days must be distinct and in descending order, e.g. [7, 3, 1]")
        return self


class CouponSettings(_Group):
    return_policy: Literal["never", "full_refund", "any_refund"] = setting(
        "full_refund", "退款后是否退券：never 不退、full_refund 整单全额退款才退、any_refund 任何退款都退", risk="high",
        owner="coupon", legacy_env="COUPON_RETURN_POLICY")
    return_window_hours: int = setting(0, "退券只在下单后多少小时内（小时，0 = 只看券的有效期）", ge=0, le=8_760,
                                       owner="coupon", legacy_env="COUPON_RETURN_WINDOW_HOURS")
    code_length: int = setting(12, "兑换码长度（位）", ge=8, le=32, owner="coupon", legacy_env="COUPON_CODE_LENGTH")
    code_max_failures: int = setting(5, "兑换码在窗口内输错几次后锁定", ge=1, le=100, risk="high", owner="coupon",
                                     legacy_env="COUPON_CODE_MAX_FAILURES")
    code_window_seconds: int = setting(600, "兑换码输错计数窗口（秒）", ge=60, le=86_400, owner="coupon",
                                       legacy_env="COUPON_CODE_WINDOW_SECONDS")
    code_batch_max: int = setting(10_000, "一批最多生成多少个兑换码", ge=1, le=100_000, owner="coupon",
                                  legacy_env="COUPON_CODE_BATCH_MAX")
    issue_batch_max: int = setting(10_000, "一次批量发券最多多少人", ge=1, le=100_000, owner="coupon",
                                   legacy_env="COUPON_ISSUE_BATCH_MAX")
    expire_batch_size: int = setting(500, "过期任务每批几张", ge=1, le=10_000, owner="coupon", legacy_env="COUPON_EXPIRE_BATCH")
    remind_before_hours: int = setting(48, "到期前多少小时提醒（小时）", ge=1, le=720, owner="coupon",
                                       legacy_env="COUPON_REMIND_BEFORE_HOURS")
    issue_job_batch: int = setting(200, "批量发券任务每批几人", ge=1, le=10_000, owner="coupon",
                                   legacy_env="COUPON_ISSUE_JOB_BATCH")
    max_coupons_per_order: int = setting(3, "一单最多叠加几张券", ge=1, le=10, client_visible=True, owner="coupon",
                                         legacy_env="COUPON_MAX_PER_ORDER")


class LotterySettings(_Group):
    prize_value_cap_cents: int = setting(5_000_000, "单个奖品价值上限（分，法定不超过 5 万元）", gt=0, le=5_000_000,
                                         risk="high", owner="lottery", legacy_env="LUZ_LOTTERY_PRIZE_CAP_CENTS")
    publicity_threshold_cents: int = setting(50_000, "奖品价值超过多少须公示兑奖情况（分，法定不超过 500 元）", gt=0,
                                             le=50_000, risk="high", owner="lottery", legacy_env="LUZ_LOTTERY_PUBLICITY_CENTS")
    retention_days: int = setting(730, "抽奖资料保存（天，法定不少于 2 年）", ge=730, le=3_650, risk="high", owner="lottery",
                                  legacy_env="LUZ_LOTTERY_RETENTION_DAYS")
    member_draws_per_minute: int = setting(10, "每个会员每分钟最多抽几次（次/分钟，0 = 不限）；网关与服务内共用", ge=0,
                                           le=1_000, risk="high", owner="lottery", legacy_env="LUZ_LOTTERY_MEMBER_PER_MIN")
    ip_draws_per_minute: int = setting(60, "每个 IP 每分钟最多抽几次（次/分钟，0 = 不限）", ge=0, le=10_000, risk="high",
                                       owner="lottery", legacy_env="LUZ_LOTTERY_IP_PER_MIN")
    delivery_ack_timeout_seconds: int = setting(600, "发奖事件发出后多久没有回执就重发（秒）", ge=60, le=86_400,
                                                owner="lottery")
    delivery_max_attempts: int = setting(5, "发奖最多尝试几次，之后等待人工重试", ge=1, le=20, owner="lottery")
    delivery_backoff_seconds: PositiveInts = setting((600, 1_800, 7_200, 21_600, 43_200), "发奖重试的退避间隔（秒）",
                                                     owner="lottery")
    default_claim_days: int = setting(7, "实物奖填写收货信息的期限（天）", ge=1, le=90, owner="lottery")
    max_prizes: int = setting(12, "一个活动最多几个奖项", ge=1, le=50, owner="lottery")
    max_admin_grant: int = setting(100, "后台单次发放抽奖次数上限（次）", ge=1, le=10_000, risk="high", owner="lottery")
    max_simulate: int = setting(1_000_000, "概率模拟最多抽几次", ge=1, le=10_000_000, owner="lottery")
    purge_batch: int = setting(500, "到期清理任务每批几条", ge=1, le=10_000, owner="lottery")


class NotificationSettings(_Group):
    quiet_start: HourMinute = setting("22:00", "静默时段开始（时:分，门店时区）；非紧急消息延后到结束", owner="notification",
                                      legacy_env="NOTIFY_QUIET_START")
    quiet_end: HourMinute = setting("08:00", "静默时段结束（时:分，门店时区）", owner="notification",
                                    legacy_env="NOTIFY_QUIET_END")
    quiet_enabled: bool = setting(True, "启用静默时段", owner="notification", legacy_env="NOTIFY_QUIET_ENABLED")
    retry_backoff_seconds: PositiveInts = setting((60, 300, 1_800, 7_200), "投递失败的重试间隔（秒）", owner="notification",
                                                  legacy_env="NOTIFY_RETRY_BACKOFF")
    max_attempts: int = setting(5, "每条消息最多投递几次", ge=1, le=20, owner="notification", legacy_env="NOTIFY_MAX_ATTEMPTS")
    send_timeout_seconds: float = setting(3.0, "单次发送超时（秒）", ge=0.5, le=30, owner="notification",
                                          legacy_env="NOTIFY_SEND_TIMEOUT")
    sending_stuck_seconds: int = setting(300, "发送中超过多久视为卡住并重新投递（秒）", ge=30, le=3_600,
                                         owner="notification", legacy_env="NOTIFY_SENDING_STUCK_SECONDS")
    batch_size: int = setting(100, "投递任务每批几条", ge=1, le=1_000, owner="notification", legacy_env="NOTIFY_BATCH_SIZE")
    miniprogram_state: MpState = setting("formal", "订阅消息跳转的小程序版本", owner="notification",
                                         legacy_env="NOTIFY_MINIPROGRAM_STATE")
    lang: MpLang = setting("zh_CN", "订阅消息语言", owner="notification", legacy_env="NOTIFY_LANG")
    quota_source: Literal["frontend", "push"] = setting(
        "frontend", "订阅额度按什么计数：frontend 前端上报的授权结果、push 微信推送的授权事件（只能选一个，否则额度翻倍）",
        risk="high", owner="notification", legacy_env="NOTIFY_QUOTA_SOURCE")
    quota_cap_per_template: int = setting(50, "每个模板本地累计额度上限（次）", ge=1, le=1_000, owner="notification",
                                          legacy_env="NOTIFY_QUOTA_CAP")
    on_43101: Literal["reset", "keep"] = setting(
        "reset", "微信返回 43101（用户没有额度）时：reset 清零本地额度、keep 只退回本次占用", owner="notification",
        legacy_env="NOTIFY_ON_43101")
    subscribe_daily_cap: int = setting(10_000_000, "订阅消息每天下发总量（条，官方：开通支付 3000 万、未开通 1000 万）",
                                       ge=1, le=30_000_000, owner="notification", legacy_env="NOTIFY_SUBSCRIBE_DAILY_CAP")
    sms_per_minute: int = setting(1, "同一手机号每分钟最多几条短信", ge=1, le=60, risk="high", owner="notification",
                                  legacy_env="NOTIFY_SMS_PER_MINUTE")
    sms_per_hour: int = setting(5, "同一手机号每小时最多几条短信", ge=1, le=1_000, risk="high", owner="notification",
                                legacy_env="NOTIFY_SMS_PER_HOUR")
    sms_per_day: int = setting(10, "同一手机号每天最多几条短信", ge=1, le=10_000, risk="high", owner="notification",
                               legacy_env="NOTIFY_SMS_PER_DAY")
    sms_daily_cap: int = setting(100_000, "全店每天短信预算（条）", ge=1, le=10_000_000, risk="high", owner="notification",
                                 legacy_env="NOTIFY_SMS_DAILY_CAP")
    member_nonurgent_per_day: int = setting(5, "每个会员每天最多收几条非紧急通知（订阅消息与短信合计）", ge=0, le=100,
                                            owner="notification", legacy_env="NOTIFY_MEMBER_NONURGENT_PER_DAY")
    inbox_retention_days: int = setting(180, "站内信保留（天）", ge=1, le=3_650, owner="notification",
                                        legacy_env="NOTIFY_INBOX_RETENTION_DAYS")
    delivery_retention_days: int = setting(180, "投递记录保留（天）", ge=1, le=3_650, owner="notification",
                                           legacy_env="NOTIFY_DELIVERY_RETENTION_DAYS")

    @model_validator(mode="after")
    def _sms_tiers(self) -> "NotificationSettings":
        if self.sms_per_minute > self.sms_per_day:  # 每分钟比每天还多，通常是单位写错
            raise ValueError("notification.sms_per_minute must not exceed notification.sms_per_day")
        return self


DEFAULT_MFA_PERMISSIONS = ("staff:write", "role:write", "after_sale:approve", "after_sale:refund", "payment:refund",
                           "report:export", "wallet:adjust", "wallet:refund", "member_card:adjust", "member_card:refund",
                           "member:reveal")


class AdminRbacSettings(_Group):
    session_idle_seconds: int = setting(900, "后台会话空闲超时（秒，OWASP 15–30 分钟）", ge=60, le=86_400, risk="high",
                                        owner="admin_rbac", legacy_env="ADMIN_SESSION_IDLE_SECONDS")
    session_absolute_seconds: int = setting(28_800, "后台会话绝对期限（秒，OWASP 4–8 小时）", ge=300, le=604_800,
                                            risk="high", owner="admin_rbac", legacy_env="ADMIN_SESSION_ABSOLUTE_SECONDS")
    session_touch_seconds: int = setting(60, "会话活跃时间最多多久写一次库（秒）", ge=1, le=3_600, owner="admin_rbac",
                                         legacy_env="ADMIN_SESSION_TOUCH_SECONDS")
    session_bind_user_agent: bool = setting(True, "会话绑定浏览器标识，换了即失效", risk="high", owner="admin_rbac",
                                            legacy_env="ADMIN_SESSION_BIND_UA")
    max_sessions_per_staff: int = setting(5, "每个员工最多同时几个会话", ge=1, le=50, owner="admin_rbac",
                                          legacy_env="ADMIN_MAX_SESSIONS")
    fresh_auth_seconds: int = setting(300, "资金与敏感操作要求最近认证的时间窗（秒）", ge=60, le=3_600, risk="high",
                                      owner="admin_rbac", legacy_env="ADMIN_FRESH_AUTH_SECONDS")
    mfa_challenge_seconds: int = setting(300, "口令通过后完成第二因素的期限（秒）", ge=60, le=1_800, owner="admin_rbac",
                                         legacy_env="ADMIN_MFA_CHALLENGE_SECONDS")
    login_max_failures: int = setting(5, "窗口内连续登录失败几次锁定账号（NIST 上限 100）", ge=1, le=100, risk="high",
                                      owner="admin_rbac", legacy_env=("ADMIN_LOGIN_MAX_FAILURES", "LUZ_LOGIN_MAX_FAILURES"))
    login_failure_window_seconds: int = setting(900, "登录失败计数窗口（秒）", ge=60, le=86_400, owner="admin_rbac",
                                                legacy_env="ADMIN_LOGIN_FAILURE_WINDOW_SECONDS")
    login_lock_seconds: int = setting(900, "首次锁定时长（秒），再次锁定逐次加倍到上限", ge=1, le=86_400, risk="high",
                                      owner="admin_rbac", legacy_env=("ADMIN_LOGIN_LOCK_SECONDS", "LUZ_LOGIN_LOCK_SECONDS"))
    login_lock_max_seconds: int = setting(86_400, "锁定时长上限（秒），也是锁定级别的衰减时间", ge=60, le=604_800,
                                          owner="admin_rbac", legacy_env="ADMIN_LOGIN_LOCK_MAX_SECONDS")
    captcha_after_failures: int = setting(3, "连续失败几次后要求人机验证（0 = 不要求）", ge=0, le=100, owner="admin_rbac",
                                          legacy_env="ADMIN_CAPTCHA_AFTER_FAILURES")
    password_min_length_mfa: int = setting(8, "已绑定第二因素时口令最短长度（位，NIST 不少于 8）", ge=8, le=64,
                                           risk="high", owner="admin_rbac", legacy_env="ADMIN_PASSWORD_MIN_MFA")
    password_min_length_no_mfa: int = setting(15, "只用口令时最短长度（位，NIST 不少于 15）", ge=8, le=128, risk="high",
                                              owner="admin_rbac", legacy_env="ADMIN_PASSWORD_MIN_NO_MFA")
    password_max_length: int = setting(128, "口令最长长度（位，至少允许 64）", ge=64, le=1_024, owner="admin_rbac",
                                       legacy_env="ADMIN_PASSWORD_MAX")
    password_blocklist_extra: tuple[Annotated[str, Field(min_length=1, max_length=128)], ...] = setting(
        (), "额外禁用的口令（如店名、品牌名）", max_length=500, owner="admin_rbac", legacy_env="ADMIN_PASSWORD_BLOCKLIST")
    password_breach_fail_closed: bool = setting(False, "泄露口令检查服务不可用时拒绝设置口令", risk="high",
                                                owner="admin_rbac", legacy_env="ADMIN_PASSWORD_BREACH_FAIL_CLOSED")
    argon2_memory_kib: int = setting(19_456, "Argon2id 内存参数（KiB，OWASP 最低 19 MiB）", ge=7_168, le=1_048_576,
                                     owner="admin_rbac", legacy_env="ADMIN_ARGON2_MEMORY_KIB")
    argon2_time_cost: int = setting(2, "Argon2id 迭代次数", ge=1, le=10, owner="admin_rbac",
                                    legacy_env="ADMIN_ARGON2_TIME_COST")
    argon2_parallelism: int = setting(1, "Argon2id 并行度", ge=1, le=16, owner="admin_rbac",
                                      legacy_env="ADMIN_ARGON2_PARALLELISM")
    temp_password_ttl_seconds: int = setting(259_200, "临时口令有效期（秒）", ge=3_600, le=604_800, risk="high",
                                             owner="admin_rbac", legacy_env="ADMIN_TEMP_PASSWORD_TTL_SECONDS")
    totp_drift_steps: int = setting(1, "TOTP 前后各允许几个 30 秒步长的漂移", ge=0, le=2, risk="high", owner="admin_rbac",
                                    legacy_env="ADMIN_TOTP_DRIFT_STEPS")
    recovery_code_count: int = setting(10, "每次生成几个恢复码", ge=5, le=20, owner="admin_rbac",
                                       legacy_env="ADMIN_RECOVERY_CODE_COUNT")
    mfa_required_all: bool = setting(False, "全部员工必须绑定第二因素", risk="high", owner="admin_rbac",
                                     legacy_env="ADMIN_MFA_REQUIRED_ALL")
    mfa_required_permissions: tuple[Annotated[str, Field(pattern=r"^[a-z_]+:([a-z_]+|\*)$")], ...] = setting(
        DEFAULT_MFA_PERMISSIONS, "持有这些权限码（含 module:* 覆盖）的员工必须绑定第二因素", max_length=200, risk="high",
        owner="admin_rbac", legacy_env="ADMIN_MFA_REQUIRED_PERMISSIONS")
    login_log_retention_days: int = setting(186, "登录日志保留（天，网络安全法不少于 6 个月，6 个月最长 184 天）", ge=184, le=3_650,
                                            owner="admin_rbac", legacy_env="ADMIN_LOGIN_LOG_RETENTION_DAYS")
    export_max_rows: int = setting(50_000, "后台单次导出最多多少行", ge=1, le=1_000_000, owner="admin_rbac",
                                   legacy_env="ADMIN_EXPORT_MAX_ROWS")

    @model_validator(mode="after")
    def _consistent(self) -> "AdminRbacSettings":
        if self.session_idle_seconds > self.session_absolute_seconds:
            raise ValueError("admin_rbac.session_idle_seconds must not exceed session_absolute_seconds")
        if self.login_lock_seconds > self.login_lock_max_seconds:
            raise ValueError("admin_rbac.login_lock_seconds must not exceed login_lock_max_seconds")
        if not self.password_min_length_mfa <= self.password_min_length_no_mfa <= self.password_max_length:
            raise ValueError("admin_rbac.password_min_length_mfa <= password_min_length_no_mfa <= password_max_length")
        return self


class WechatMpSettings(_Group):
    timeout_s: float = setting(3.0, "服务端接口超时（秒，读类接口）；客户端创建时读取", ge=0.5, le=30, reloadable=False, owner="wechat_mp",
                               legacy_env="WECHAT_MP_TIMEOUT")
    qrcode_timeout_s: float = setting(5.0, "生成小程序码超时（秒）；客户端创建时读取", ge=1, le=60, reloadable=False, owner="wechat_mp",
                                      legacy_env="WECHAT_MP_QRCODE_TIMEOUT")
    token_refresh_margin_s: int = setting(300, "access_token 到期前多久刷新（秒；新旧 token 5 分钟内都可用）；客户端创建时读取", ge=60,
                                          le=1_800, reloadable=False, owner="wechat_mp", legacy_env="WECHAT_MP_TOKEN_MARGIN")
    force_refresh_min_interval_s: int = setting(30, "两次强制刷新 access_token 的最短间隔（秒，官方至少 30 秒）；客户端创建时读取", ge=30,
                                                le=3_600, reloadable=False, owner="wechat_mp", legacy_env="WECHAT_MP_FORCE_REFRESH_INTERVAL")
    miniprogram_state: MpState = setting("formal", "订阅消息默认跳转的小程序版本；客户端创建时读取", reloadable=False, owner="wechat_mp",
                                         legacy_env="WECHAT_MP_STATE")
    lang: MpLang = setting("zh_CN", "订阅消息默认语言；客户端创建时读取", reloadable=False, owner="wechat_mp", legacy_env="WECHAT_MP_LANG")
    env_version: Literal["release", "trial", "develop"] = setting("release", "小程序码默认打开的版本；客户端创建时读取", reloadable=False, owner="wechat_mp",
                                                                 legacy_env="WECHAT_MP_ENV_VERSION")
    push_require_aes: bool = setting(True, "消息推送只接受安全模式（AES 加密）", risk="high", owner="wechat_mp",
                                     legacy_env="WECHAT_MP_PUSH_REQUIRE_AES")
    push_max_skew_s: int = setting(0, "消息推送时间戳允许的偏差（秒，0 = 不校验）", ge=0, le=3_600, owner="wechat_mp",
                                   legacy_env="WECHAT_MP_PUSH_MAX_SKEW")


class RuntimeConfig(BaseModel):
    """一份已校验的运行参数（不含覆盖规则）。快照对象不可变，读取方不要修改其中的字典。"""

    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)

    logging: LoggingSettings = LoggingSettings()
    http: HttpSettings = HttpSettings()
    db: DbSettings = DbSettings()
    rate_limit: RateLimitSettings = RateLimitSettings()
    breaker: BreakerSettings = BreakerSettings()
    auth: AuthSettings = AuthSettings()
    upload: UploadSettings = UploadSettings()
    client: ClientSettings = ClientSettings()
    features: FeatureSettings = FeatureSettings()
    membership: MembershipSettings = MembershipSettings()
    identity: IdentitySettings = IdentitySettings()
    order: OrderSettings = OrderSettings()
    wechat_pay: WechatPaySettings = WechatPaySettings()
    after_sales: AfterSalesSettings = AfterSalesSettings()
    prepaid: PrepaidSettings = PrepaidSettings()
    wallet: WalletSettings = WalletSettings()
    member_card: MemberCardSettings = MemberCardSettings()
    coupon: CouponSettings = CouponSettings()
    lottery: LotterySettings = LotterySettings()
    notification: NotificationSettings = NotificationSettings()
    admin_rbac: AdminRbacSettings = AdminRbacSettings()
    wechat_mp: WechatMpSettings = WechatMpSettings()


class Override(BaseModel):
    """环境文件里的一条覆盖规则：`[overrides.<名称>]` 写命中条件，`[overrides.<名称>.set]` 写 "分组.键" = 值。

    条件之间是“且”；没写的条件不限制。percent 按用户内部编号分桶（同一用户在同一条规则里总落在同一桶）。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    stores: tuple[Annotated[int, Field(ge=1)], ...] = Field(default=(), max_length=1_000)
    channels: tuple[Platform, ...] = ()
    client_version_min: SemVer | None = None
    client_version_max: SemVer | None = None
    percent: int | None = Field(default=None, ge=1, le=100)
    values: dict[str, Any] = Field(alias="set", min_length=1)

    @model_validator(mode="after")
    def _has_condition(self) -> "Override":
        if not self.dimensions():
            raise ValueError("override needs at least one condition (stores, channels, client_version_min/max, percent); "
                             "a value for everyone belongs in the environment file body")
        low, high = self.client_version_min, self.client_version_max
        if low and high and version_tuple(low) > version_tuple(high):
            raise ValueError("client_version_min must not be greater than client_version_max")
        return self

    def dimensions(self) -> set[str]:
        dims: set[str] = set()
        if self.stores:
            dims.add("store")
        if self.channels:
            dims.add("channel")
        if self.client_version_min or self.client_version_max:
            dims.add("client_version")
        if self.percent is not None:
            dims.add("user_bucket")
        return dims


class RuntimeFile(RuntimeConfig):
    """配置文件合并后的整体：运行参数 + 覆盖规则。"""

    overrides: dict[OverrideName, Override] = {}


def version_tuple(value: str | None) -> tuple[int, int, int] | None:
    """"1.2.3"（可带 -beta 等后缀）→ (1, 2, 3)；不合法返回 None。"""
    if not value:
        return None
    head = str(value).strip().split("-", 1)[0].split("+", 1)[0]
    parts = head.split(".")
    if len(parts) != 3 or not all(p.isdigit() for p in parts):
        return None
    return int(parts[0]), int(parts[1]), int(parts[2])


# ---------- 键的元数据 ----------

@dataclass(frozen=True)
class KeyInfo:
    path: str                      # "group.key"
    group: str
    name: str
    description: str
    default: Any
    reloadable: bool
    client_visible: bool
    risk: str
    owner: str
    scope: tuple[str, ...]
    platforms: tuple[str, ...]
    client_since: str
    allowed: str
    legacy_env: tuple[str, ...] = ()


def _allowed(info: FieldInfo) -> str:
    annotation = info.annotation
    origin = get_origin(annotation)
    if origin is Literal:
        return "one of: " + ", ".join(str(a) for a in get_args(annotation))
    if annotation is bool:
        return "true or false"
    if origin is dict:
        return "table of logger name = level"
    if origin is tuple:
        inner = get_args(annotation)[0] if get_args(annotation) else Any
        inner = get_args(inner)[0] if get_origin(inner) is Annotated else inner
        return "list of " + {int: "positive integers", str: "text"}.get(inner, "values")
    kind = {int: "integer", float: "number", str: "text"}.get(annotation, getattr(annotation, "__name__", "value"))
    bounds: list[str] = []
    for item in info.metadata:
        for attr, symbol in (("ge", ">="), ("gt", ">"), ("le", "<="), ("lt", "<")):
            value = getattr(item, attr, None)
            if value is not None:
                bounds.append(f"{symbol} {value}")
        pattern = getattr(item, "pattern", None)
        if pattern:
            bounds.append("version x.y.z" if r"\d{1,4}\." in pattern else f"pattern {pattern}")
    return f"{kind} {', '.join(bounds)}".strip()


@lru_cache(maxsize=1)
def registry() -> dict[str, KeyInfo]:
    """全部登记的键：{"group.key": KeyInfo}（按 RuntimeConfig 的分组顺序）。"""
    keys: dict[str, KeyInfo] = {}
    for group, group_field in RuntimeConfig.model_fields.items():
        model = group_field.annotation
        for name, info in model.model_fields.items():
            extra = info.json_schema_extra if isinstance(info.json_schema_extra, dict) else {}
            if not extra:
                raise TypeError(f"{group}.{name} must be declared with setting(...)")
            keys[f"{group}.{name}"] = KeyInfo(
                path=f"{group}.{name}", group=group, name=name, description=info.description or "",
                default=info.default, reloadable=bool(extra["reloadable"]), client_visible=bool(extra["client_visible"]),
                risk=str(extra["risk"]), owner=str(extra["owner"]), scope=tuple(extra["scope"]),
                platforms=tuple(extra.get("platforms", ())), client_since=str(extra.get("client_since", "0.0.0")),
                allowed=_allowed(info), legacy_env=tuple(extra.get("legacy_env", ())),
            )
    return keys


@lru_cache(maxsize=1)
def legacy_env_index() -> dict[str, str]:
    """迁移前的环境变量名 → 运行配置键（"group.key"）。启动扫描与测试工具用。"""
    return {name: info.path for info in registry().values() for name in info.legacy_env}


def group_keys(group: str) -> list[str]:
    return [info.name for info in registry().values() if info.group == group]
