"""wechat_mp 配置：秘密与部署参数（环境变量，代码里没有真实密钥）+ 运行参数（config/runtime 的 `[wechat_mp]`）。

AppID / AppSecret 与框架共用 `WECHAT_APPID` / `WECHAT_SECRET`；消息推送的 Token 与 EncodingAESKey 只在
小程序后台「开发 - 开发管理 - 消息推送配置」生成，部署时注入；接口地址 WECHAT_MP_API_BASE。
运行参数（超时、access_token 刷新、订阅消息与小程序码默认版本、推送安全模式与时间偏差）的类型、范围与默认值登记在
`luzback/runtime_config.py` 的 WechatMpSettings。超时、刷新与默认版本在服务端客户端创建时读取（重启生效），
推送相关的两个开关每次请求读取（热加载）。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import partial

from luzback.module_config import ModuleConfig, runtime_default

DEV_APPID = "wx-dev-appid"
DEV_SECRET = "wx-dev-secret"
API_BASE = "https://api.weixin.qq.com"

_rt = partial(runtime_default, "wechat_mp")


def _env(name: str, default: str) -> str:
    return os.getenv(name, default)


@dataclass(frozen=True)
class WechatMpConfig:
    # ---- 秘密与部署参数（环境变量）----
    appid: str = field(default_factory=lambda: _env("WECHAT_APPID", DEV_APPID))
    secret: str = field(default_factory=lambda: _env("WECHAT_SECRET", DEV_SECRET))
    api_base: str = field(default_factory=lambda: _env("WECHAT_MP_API_BASE", API_BASE))
    push_token: str = field(default_factory=lambda: _env("WECHAT_MP_PUSH_TOKEN", ""))
    push_aes_key: str = field(default_factory=lambda: _env("WECHAT_MP_PUSH_AES_KEY", ""))  # 43 位 EncodingAESKey
    # ---- 运行参数（[wechat_mp]）----
    # 超时（秒）：读类接口 3 秒（SPEC §9）；小程序码返回图片，给 5 秒
    timeout_s: float = field(default_factory=_rt("timeout_s"))
    qrcode_timeout_s: float = field(default_factory=_rt("qrcode_timeout_s"))
    # access_token 提前刷新的余量：平台保证新旧 token 5 分钟内都可用；force_refresh 两次之间至少 30 秒（官方限制，且每天 20 次）
    token_refresh_margin_s: int = field(default_factory=_rt("token_refresh_margin_s"))
    force_refresh_min_interval_s: int = field(default_factory=_rt("force_refresh_min_interval_s"))
    # 订阅消息默认跳转版本：developer | trial | formal；小程序码默认版本：release | trial | develop
    miniprogram_state: str = field(default_factory=_rt("miniprogram_state"))
    lang: str = field(default_factory=_rt("lang"))
    env_version: str = field(default_factory=_rt("env_version"))
    # 消息推送
    push_require_aes: bool = field(default_factory=_rt("push_require_aes"))
    push_max_skew_s: int = field(default_factory=_rt("push_max_skew_s"))  # 0 = 不校验时间戳

    @property
    def is_dev_placeholder(self) -> bool:
        return self.appid == DEV_APPID or self.secret == DEV_SECRET

    def validate(self) -> None:
        """秘密的格式（运行参数的取值范围由字段说明在加载时校验）。"""
        if self.push_aes_key and len(self.push_aes_key) != 43:
            raise ValueError("WECHAT_MP_PUSH_AES_KEY must be 43 characters")


_module = ModuleConfig("wechat_mp", WechatMpConfig)


def get_config() -> WechatMpConfig:
    config = _module.get()
    config.validate()
    return config


def reload_config() -> WechatMpConfig:
    """重新读秘密与部署参数（环境变量）；运行参数随运行配置加载。"""
    _module.reload()
    return get_config()
