"""wechat_mp：小程序服务端接口的唯一共享客户端（登录、手机号、订阅消息、内容安全、小程序码、消息推送）。

其他模块只从这里导入：
- 客户端：MiniProgramClient（协议）、HttpMiniProgramClient、get_client / set_client、FakeMiniProgram / FakeWechatServer
- 模块级：send_subscribe_message(...)（20-notification）、access_token() / invalidate_access_token(bad)（09-wechat-pay 发货信息等）
- 生产检查：production_problems()，导入时登记 luzback.register_production_check("wechat_mp", ...)
- 结果：Code2SessionResult、PhoneInfo、SecCheckResult、MediaCheckEvent
- 异常：WechatError 及子类（InvalidCode、RateLimited、SubscriptionRefused …）
- access_token：AccessTokenCache、MemoryTokenStore、DbTokenStore
- 消息推送：PushCrypto、verify_url_signature、msg_signature、parse_body、normalize_list、parse_media_check_event、
  register_push_handler、dispatch
"""
from luzback import register_production_check

from .client import (
    Code2SessionResult, HttpMiniProgramClient, HttpResponse, MiniProgramClient, PhoneInfo, SecCheckResult, access_token,
    breakers, get_client, invalidate_access_token, is_configured, production_problems, send_subscribe_message, session_signature,
    set_client, validate_scene, validate_subscribe_data,
)
from .config import WechatMpConfig, get_config, reload_config
from .errors import (
    AccessTokenInvalid, CapabilityBanned, CodeBlocked, ConcurrentSend, ConfigurationError, InvalidArgument, InvalidCode,
    OpenidMismatch, RateLimited, SensitiveContent, SessionKeyInvalid, SubscriptionRefused, TemplateInvalid,
    UserNotVisited, WechatError, error_for,
)
from .fake import FakeMiniProgram, FakeWechatServer
from .models import MpAccessToken, MpPushEvent
from .push import (
    MediaCheckEvent, PushCrypto, dedup_key, dispatch, msg_signature, normalize_list, parse_body, parse_media_check_event,
    register_push_handler, unregister_push_handler, url_signature, verify_url_signature,
)
from .token import AccessTokenCache, DbTokenStore, MemoryTokenStore, TokenEntry, TokenStore

__all__ = [
    "Code2SessionResult", "HttpMiniProgramClient", "HttpResponse", "MiniProgramClient", "PhoneInfo", "SecCheckResult",
    "access_token", "breakers", "get_client", "invalidate_access_token", "is_configured", "production_problems", "send_subscribe_message",
    "session_signature", "set_client", "validate_scene", "validate_subscribe_data",
    "WechatMpConfig", "get_config", "reload_config",
    "AccessTokenInvalid", "CapabilityBanned", "CodeBlocked", "ConcurrentSend", "ConfigurationError", "InvalidArgument",
    "InvalidCode", "OpenidMismatch", "RateLimited", "SensitiveContent", "SessionKeyInvalid", "SubscriptionRefused",
    "TemplateInvalid", "UserNotVisited", "WechatError", "error_for",
    "FakeMiniProgram", "FakeWechatServer", "MpAccessToken", "MpPushEvent",
    "MediaCheckEvent", "PushCrypto", "dedup_key", "dispatch", "msg_signature", "normalize_list", "parse_body",
    "parse_media_check_event", "register_push_handler", "unregister_push_handler", "url_signature", "verify_url_signature",
    "AccessTokenCache", "DbTokenStore", "MemoryTokenStore", "TokenEntry", "TokenStore",
]

register_production_check("wechat_mp", production_problems)
