"""identity：会员账号（微信登录 / 自动注册 / 协议同意、令牌与会话、短信验证码、手机号、资料、注销、后台管理）。

其他模块只从这里导入（依赖注入与路由在 identity.api：current_user、current_claims、require_permission …）：
- 模型与状态：User、UserSession、RefreshToken、SmsCode、ContentCheck、STATUS_*
- 查询：find_user_by_phone(db, phone) -> User | None（phone_hash 等值查询）
- 用例：login_with_code、login_with_sms、refresh_tokens、logout、issue_tokens、decode_access、current_user_id、
  require_fresh、send_sms_code、bind_phone_with_sms、bind_phone_from_wechat、attach_phone、profile、update_profile、
  request_deletion、cancel_deletion、anonymize_member、session_key_of、handle_media_check_event
- 扩展点：register_deletion_checker / Blocker、SmsSender / FakeSmsSender、AgreementGateway / FakeAgreements
- 后台：PERMISSIONS、list_members、member_detail、reveal_phone、disable_member、enable_member、force_logout、freeze
- 事件：member.registered、member.phone_bound、member.disabled、member.deleted
"""
from .admin import (
    PERMISSIONS, disable_member, enable_member, force_logout, freeze, list_members, member_detail, reveal_phone,
)
from .config import IdentityConfig, get_config, production_problems, reload_config
from .models import (
    STATUS_ACTIVE, STATUS_DEACTIVATED, STATUS_DELETED, STATUS_DELETING, STATUS_DISABLED, STATUS_FROZEN, STATUSES,
    ContentCheck, RefreshToken, SmsCode, User, UserSession,
)
from .ports import (
    AgreementAccept, AgreementGateway, Blocker, FakeAgreements, FakeSmsSender, NoAgreements, SmsSender,
    register_deletion_checker, unregister_deletion_checker,
)
from .service import (
    anonymize_member, attach_phone, bind_phone_from_wechat, bind_phone_with_sms, cancel_deletion, current_user_id,
    decode_access, deletion_status, find_user_by_phone, handle_media_check_event, issue_tokens, login_with_code, login_with_sms, logout,
    profile, refresh_tokens, request_deletion, require_fresh, revoke_sessions, send_sms_code, session_key_of,
    update_profile,
)

from luzback import register_production_check as _register_production_check

_register_production_check("identity", production_problems)

EVENTS = ("member.registered", "member.phone_bound", "member.disabled", "member.deleted")

try:  # 头像异步检测结果经 28-wechat-mp 消息推送回到这里
    from wechat_mp import register_push_handler as _register_push_handler

    _register_push_handler("wxa_media_check", handle_media_check_event)
except ImportError:  # pragma: no cover - 未启用 wechat_mp
    pass

__all__ = [
    "PERMISSIONS", "disable_member", "enable_member", "force_logout", "freeze", "list_members", "member_detail", "reveal_phone",
    "IdentityConfig", "get_config", "production_problems", "reload_config",
    "STATUS_ACTIVE", "STATUS_DEACTIVATED", "STATUS_DELETED", "STATUS_DELETING", "STATUS_DISABLED", "STATUS_FROZEN", "STATUSES",
    "ContentCheck", "RefreshToken", "SmsCode", "User", "UserSession",
    "AgreementAccept", "AgreementGateway", "Blocker", "FakeAgreements", "FakeSmsSender", "NoAgreements", "SmsSender",
    "register_deletion_checker", "unregister_deletion_checker",
    "anonymize_member", "attach_phone", "bind_phone_from_wechat", "bind_phone_with_sms", "cancel_deletion",
    "current_user_id", "decode_access", "deletion_status", "find_user_by_phone", "handle_media_check_event", "issue_tokens", "login_with_code",
    "login_with_sms", "logout", "profile", "refresh_tokens", "request_deletion", "require_fresh", "revoke_sessions",
    "send_sms_code", "session_key_of", "update_profile", "EVENTS",
]
