"""wechat_mp 配置：全部来自环境变量，代码里没有真实密钥。

AppID / AppSecret 与框架共用 `WECHAT_APPID` / `WECHAT_SECRET`；消息推送的 Token 与 EncodingAESKey 只在
小程序后台「开发 - 开发管理 - 消息推送配置」生成，部署时注入。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache

DEV_APPID = "wx-dev-appid"
DEV_SECRET = "wx-dev-secret"
API_BASE = "https://api.weixin.qq.com"


def _env(name: str, default: str) -> str:
    return os.getenv(name, default)


def _float(name: str, default: float) -> float:
    return float(os.getenv(name, str(default)))


def _int(name: str, default: int) -> int:
    return int(os.getenv(name, str(default)))


def _bool(name: str, default: bool) -> bool:
    return os.getenv(name, "true" if default else "false").lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class WechatMpConfig:
    appid: str = field(default_factory=lambda: _env("WECHAT_APPID", DEV_APPID))
    secret: str = field(default_factory=lambda: _env("WECHAT_SECRET", DEV_SECRET))
    api_base: str = field(default_factory=lambda: _env("WECHAT_MP_API_BASE", API_BASE))
    # 超时（秒）：读类接口 3 秒（SPEC §9）；小程序码返回图片，给 5 秒
    timeout_s: float = field(default_factory=lambda: _float("WECHAT_MP_TIMEOUT", 3.0))
    qrcode_timeout_s: float = field(default_factory=lambda: _float("WECHAT_MP_QRCODE_TIMEOUT", 5.0))
    # access_token 提前刷新的余量：平台保证新旧 token 5 分钟内都可用
    token_refresh_margin_s: int = field(default_factory=lambda: _int("WECHAT_MP_TOKEN_MARGIN", 300))
    # force_refresh 两次之间至少 30 秒（官方限制，且每天 20 次）
    force_refresh_min_interval_s: int = field(default_factory=lambda: _int("WECHAT_MP_FORCE_REFRESH_INTERVAL", 30))
    # 订阅消息默认跳转版本：developer | trial | formal
    miniprogram_state: str = field(default_factory=lambda: _env("WECHAT_MP_STATE", "formal"))
    lang: str = field(default_factory=lambda: _env("WECHAT_MP_LANG", "zh_CN"))
    # 小程序码默认版本：release | trial | develop
    env_version: str = field(default_factory=lambda: _env("WECHAT_MP_ENV_VERSION", "release"))
    # 消息推送
    push_token: str = field(default_factory=lambda: _env("WECHAT_MP_PUSH_TOKEN", ""))
    push_aes_key: str = field(default_factory=lambda: _env("WECHAT_MP_PUSH_AES_KEY", ""))  # 43 位 EncodingAESKey
    push_require_aes: bool = field(default_factory=lambda: _bool("WECHAT_MP_PUSH_REQUIRE_AES", True))
    push_max_skew_s: int = field(default_factory=lambda: _int("WECHAT_MP_PUSH_MAX_SKEW", 0))  # 0 = 不校验时间戳

    @property
    def is_dev_placeholder(self) -> bool:
        return self.appid == DEV_APPID or self.secret == DEV_SECRET

    def validate(self) -> None:
        if self.miniprogram_state not in {"developer", "trial", "formal"}:
            raise ValueError("WECHAT_MP_STATE must be developer|trial|formal")
        if self.env_version not in {"release", "trial", "develop"}:
            raise ValueError("WECHAT_MP_ENV_VERSION must be release|trial|develop")
        if self.push_aes_key and len(self.push_aes_key) != 43:
            raise ValueError("WECHAT_MP_PUSH_AES_KEY must be 43 characters")


@lru_cache(maxsize=1)
def get_config() -> WechatMpConfig:
    config = WechatMpConfig()
    config.validate()
    return config


def reload_config() -> WechatMpConfig:
    get_config.cache_clear()
    return get_config()
