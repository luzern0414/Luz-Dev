"""微信 errcode → 类型化异常。

所有异常都是 `AppError` 子类，可直接冒泡到接口层；`errcode` / `errmsg` 保留在属性与 data 里，
面向用户的 message 不包含上游原文。错误码来源见 MODULE.md §0（各接口页的错误码表与全局错误码表）。
"""
from __future__ import annotations

from luzback import AppError, ErrorCode


class WechatError(AppError):
    """微信接口返回非 0 errcode。"""

    default_code = ErrorCode.UPSTREAM_FAILED
    default_message = "wechat service error"

    def __init__(self, errcode: int, errmsg: str = "", message: str | None = None, retry_after: int | None = None):
        super().__init__(self.default_code, message or self.default_message, data={"errcode": errcode},
                         retry_after=retry_after)
        self.errcode = errcode
        self.errmsg = errmsg


class AccessTokenInvalid(WechatError):
    """40001 / 40014 / 42001：access_token 无效或过期，应失效缓存后重试一次。"""


class InvalidCode(WechatError):
    """40029 code 无效；40163 code 已被使用。前端需重新 wx.login / 重新点按钮获取。"""

    default_code = ErrorCode.UNAUTHORIZED
    default_message = "login code invalid or expired, please retry"


class CodeBlocked(WechatError):
    """40226：高风险等级用户，小程序登录被拦截。"""

    default_code = ErrorCode.FORBIDDEN
    default_message = "login blocked by wechat risk control"


class RateLimited(WechatError):
    """45011 分钟频率、45009 日频率、44991 内容安全分钟频率。"""

    default_code = ErrorCode.RATE_LIMITED
    default_message = "wechat api rate limited, please retry later"

    def __init__(self, errcode: int, errmsg: str = "", message: str | None = None, retry_after: int | None = 60):
        super().__init__(errcode, errmsg, message, retry_after=retry_after)


class SubscriptionRefused(WechatError):
    """43101：用户未订阅 / 拒收 / 一次性订阅次数已用完。"""

    default_code = ErrorCode.BUSINESS_RULE
    default_message = "user has not subscribed to this message"


class TemplateInvalid(WechatError):
    """40037 模板 id 无效；47003 data 参数不符合规则；20001 等模板错误。"""

    default_code = ErrorCode.BUSINESS_RULE
    default_message = "subscribe message template or data invalid"


class SensitiveContent(WechatError):
    """45168：订阅消息命中敏感词。"""

    default_code = ErrorCode.BUSINESS_RULE
    default_message = "message content not allowed"


class UserNotVisited(WechatError):
    """61010：用户近两小时未访问小程序（内容安全接口要求）。"""

    default_code = ErrorCode.BUSINESS_RULE
    default_message = "user has not visited the mini program recently"


class OpenidMismatch(WechatError):
    """40003 openid 无效；43104 openid 与 appid 不匹配。"""

    default_code = ErrorCode.BAD_REQUEST
    default_message = "openid invalid for this app"


class SessionKeyInvalid(WechatError):
    """87007 session_key 不存在或过期；87009 签名无效。"""

    default_code = ErrorCode.UNAUTHORIZED
    default_message = "wechat session expired, please login again"


class InvalidArgument(WechatError):
    """40097 / 40129 / 40169 / 41030 / 85096 / 40004 / 47001 / 44002 / 40013：参数错误。"""

    default_code = ErrorCode.BAD_REQUEST
    default_message = "invalid wechat api argument"


class ConcurrentSend(WechatError):
    """43108：并发下发消息给同一个用户（可稍后重试）。"""

    default_code = ErrorCode.RATE_LIMITED
    default_message = "concurrent message to the same user, retry later"


class CapabilityBanned(WechatError):
    """43107 订阅消息能力被封禁；50004 禁止使用 token 接口；50007 账号已冻结。"""


class ConfigurationError(WechatError):
    """40125 secret 不合法 / 40164 IP 不在白名单 / 40243 AppSecret 已冻结 / 41004 缺少 secret / 89503 等需管理员确认。"""

    default_message = "wechat integration misconfigured"


_MAP: dict[int, type[WechatError]] = {
    40001: AccessTokenInvalid, 40014: AccessTokenInvalid, 42001: AccessTokenInvalid,
    40029: InvalidCode, 40163: InvalidCode,
    40226: CodeBlocked,
    45009: RateLimited, 45011: RateLimited, 44991: RateLimited, 87020: RateLimited,
    43101: SubscriptionRefused,
    40037: TemplateInvalid, 47003: TemplateInvalid, 20001: TemplateInvalid,
    45168: SensitiveContent,
    61010: UserNotVisited,
    40003: OpenidMismatch, 43104: OpenidMismatch,
    87007: SessionKeyInvalid, 87009: SessionKeyInvalid, 87008: InvalidArgument,
    40097: InvalidArgument, 40129: InvalidArgument, 40169: InvalidArgument, 41030: InvalidArgument,
    85096: InvalidArgument, 40004: InvalidArgument, 47001: InvalidArgument, 44002: InvalidArgument,
    40013: InvalidArgument, 40002: InvalidArgument, 43002: InvalidArgument, 41002: InvalidArgument,
    43108: ConcurrentSend,
    43107: CapabilityBanned, 50004: CapabilityBanned, 50007: CapabilityBanned,
    40125: ConfigurationError, 40164: ConfigurationError, 40243: ConfigurationError, 41004: ConfigurationError,
    89503: ConfigurationError, 89506: ConfigurationError, 89507: ConfigurationError,
}

TOKEN_ERRCODES = frozenset({40001, 40014, 42001})
SYSTEM_BUSY = -1


def error_for(errcode: int, errmsg: str = "") -> WechatError:
    return _MAP.get(errcode, WechatError)(errcode, errmsg)
