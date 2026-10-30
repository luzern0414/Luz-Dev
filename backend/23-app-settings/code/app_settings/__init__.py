"""23-app-settings（app_settings）：运行配置的接口与加载记录 + 文案与展示配置。

三类东西分开放（MODULE.md §2）：
- 运行参数：`config/runtime/*.toml`，服务端热加载，不在后台展示。字段说明与加载器在框架（luzback.runtime_config /
  runtime_loader）；本模块提供 `/internal/config`、`/internal/config/reload`、`/api/v1/app/config` 与 config_loads 加载记录。
- 文案与展示：数据库（ui_texts、display_settings），运营在后台实时维护，小程序经 `/api/v1/app/content` 拉取。
- 隐藏的文案与展示键：同样存后端、照常下发，但不在后台展示、后台接口拒绝修改；文案由开发经种子 `seed/texts.toml` 维护，
  展示键的默认值在 display.py 登记。
- 经营设置：退款手续费、储值扣减顺序、退券策略、补卡费这类商户日常调整的经营规则；键由各业务模块用 luzback.business 登记，
  值存 business_settings，运营在后台“经营设置”页改（/api/v1/admin/settings），业务模块用 business_value 读取。

其他模块只从这里导入：
    from app_settings import install, resolve_display, register_display_key, DisplayKey, set_text_checker
进程入口在数据库就绪后调用 `install(store)` 写加载记录；发布时 `python -m app_settings sync` 同步文案种子。
"""
from admin_rbac import declare_permissions

from . import business  # noqa: F401 - 导入即接上 luzback.business 的存储
from .content import (
    content_version, list_display, public_content, reset_display, reset_text, resolve_display, restore_revision,
    set_text_checker, update_display, update_text,
)
from .display import DISPLAY_KEYS, DisplayKey, display_keys, get_display_key, register_display_key
from .models import BusinessSettingValue, ConfigLoad, ContentRevision, DisplaySetting, UiText
from .recorder import install, record_load
from .seed import SEED_PATH, SeedText, load_seed, sync_texts

PERMISSIONS = (("settings:read", "系统设置查看（文案、展示与经营设置）"), ("settings:write", "系统设置修改（文案、展示与经营设置）"))
declare_permissions("app_settings", PERMISSIONS)

__all__ = [
    "PERMISSIONS", "content_version", "list_display", "public_content", "reset_display", "reset_text", "resolve_display",
    "restore_revision", "set_text_checker", "update_display", "update_text",
    "DISPLAY_KEYS", "DisplayKey", "display_keys", "get_display_key", "register_display_key",
    "BusinessSettingValue", "ConfigLoad", "ContentRevision", "DisplaySetting", "UiText", "install", "record_load",
    "SEED_PATH", "SeedText", "load_seed", "sync_texts",
]
