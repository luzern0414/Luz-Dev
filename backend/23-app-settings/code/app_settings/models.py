"""app_settings 数据模型：运行配置加载记录、文案库、展示设置、内容修改历史、经营设置。

- 数组与 JSON 值按全项目惯例存成 Text（`*_json`），SQLite 与 PostgreSQL 一致；详设里的 TEXT[] / JSONB 对应这里的 `*_json`。
- 内容版本 = content_revisions 的最大 id（每次修改、恢复与种子同步都追加一行）。
- display_settings：展示设置，store_id 为空 = 全局；全局与门店各自唯一（两个部分唯一索引，等价于 UNIQUE(store_id, key)
  NULLS NOT DISTINCT）。
- business_settings：经营设置（各业务模块在 luzback.business 登记的键），同样全局一行、允许门店覆盖的键每店一行；
  修改写审计，不进内容版本（小程序不下发）。
- 文案与展示设置按 version 条件更新（并发修改后保存的一方返回冲突）。
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Index, Integer, SmallInteger, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from luzback import Base, BigId, BigIdMixin, TimestampMixin, utcnow

TARGET_TEXT = "text"
TARGET_DISPLAY = "display"
SOURCE_ADMIN = "admin"
SOURCE_MIGRATION = "migration"
SOURCE_RESTORE = "restore"


class ConfigLoad(Base, BigIdMixin):
    """每个实例每次加载运行配置的结果（成功与失败都写）：核对多实例版本一致、追查“何时生效”。"""

    __tablename__ = "config_loads"
    __table_args__ = (
        Index("ix_config_loads_instance_created", "instance_id", "created_at"),
        Index("ix_config_loads_version", "config_version"),
    )

    instance_id: Mapped[str] = mapped_column(String(64))
    config_version: Mapped[str] = mapped_column(String(16), default="")  # 加载后生效的版本；被拒时为仍在用的旧版本
    source_commit: Mapped[str | None] = mapped_column(String(40), nullable=True)
    trigger: Mapped[str] = mapped_column(String(16))          # startup | file_change | reload_api | signal
    result: Mapped[str] = mapped_column(String(16))           # loaded | unchanged | rejected
    changed_keys_json: Mapped[str | None] = mapped_column(Text, nullable=True)   # 只记键名，不记值
    restart_keys_json: Mapped[str | None] = mapped_column(Text, nullable=True)   # 已变化但要重启才生效的键
    error_summary: Mapped[str | None] = mapped_column(Text, nullable=True)       # 键、原因、允许范围（秘密已打码）
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class UiText(Base, BigIdMixin, TimestampMixin):
    """小程序每处可见文字：键 `页面.位置`；text 为空表示用默认文案（allow_empty 的键存空串表示该位置不显示）。"""

    __tablename__ = "ui_texts"
    __table_args__ = (Index("ix_ui_texts_page_sort", "page", "sort_order"),)

    key: Mapped[str] = mapped_column(String(80), unique=True)
    page: Mapped[str] = mapped_column(String(48))
    slot_label: Mapped[str] = mapped_column(String(60))
    text: Mapped[str | None] = mapped_column(Text, nullable=True)
    default_text: Mapped[str] = mapped_column(Text)
    max_length: Mapped[int] = mapped_column(SmallInteger)
    variables_json: Mapped[str] = mapped_column(Text, default="[]")
    allow_empty: Mapped[bool] = mapped_column(Boolean, default=False)
    admin_visible: Mapped[bool] = mapped_column(Boolean, default=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    version: Mapped[int] = mapped_column(Integer, default=1)
    updated_by: Mapped[int | None] = mapped_column(BigId, nullable=True)


_GLOBAL = text("store_id IS NULL")
_STORE = text("store_id IS NOT NULL")


class DisplaySetting(Base, BigIdMixin, TimestampMixin):
    """展示设置的全局值（store_id 为空）与门店覆盖；键在 display.py 登记，没有行时用登记的默认值。"""

    __tablename__ = "display_settings"
    __table_args__ = (
        Index("uq_display_settings_global_key", "key", unique=True, sqlite_where=_GLOBAL, postgresql_where=_GLOBAL),
        Index("uq_display_settings_store_key", "store_id", "key", unique=True, sqlite_where=_STORE,
              postgresql_where=_STORE),
    )

    store_id: Mapped[int | None] = mapped_column(BigId, nullable=True)
    key: Mapped[str] = mapped_column(String(64))
    value_json: Mapped[str] = mapped_column(Text)
    admin_visible: Mapped[bool] = mapped_column(Boolean, default=True)  # 写入时取登记的值，便于直接查库时区分隐藏键
    version: Mapped[int] = mapped_column(Integer, default=1)
    updated_by: Mapped[int | None] = mapped_column(BigId, nullable=True)


class ContentRevision(Base, BigIdMixin):
    """文案与展示设置每次修改的前后值；id 即内容版本。只追加，不修改不删除。"""

    __tablename__ = "content_revisions"
    __table_args__ = (Index("ix_content_revisions_target", "target_type", "target_key", "created_at"),)

    target_type: Mapped[str] = mapped_column(String(16))      # text | display
    target_key: Mapped[str] = mapped_column(String(80))
    store_id: Mapped[int | None] = mapped_column(BigId, nullable=True)
    # 值按 JSON 存：文案的 null 表示用默认文案；展示设置的列为空表示这一级没有值（继承上一级或登记的默认值）
    old_value_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    new_value_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    changed_by: Mapped[int | None] = mapped_column(BigId, nullable=True)     # 种子同步时为空
    source: Mapped[str] = mapped_column(String(16), default=SOURCE_ADMIN)    # admin | migration | restore
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class BusinessSettingValue(Base, BigIdMixin, TimestampMixin):
    """经营设置的全局值（store_id 为空）与门店覆盖；键与类型在 luzback.business 登记，没有行时用登记的默认值。"""

    __tablename__ = "business_settings"
    __table_args__ = (
        Index("uq_business_settings_global_key", "key", unique=True, sqlite_where=_GLOBAL, postgresql_where=_GLOBAL),
        Index("uq_business_settings_store_key", "store_id", "key", unique=True, sqlite_where=_STORE,
              postgresql_where=_STORE),
    )

    store_id: Mapped[int | None] = mapped_column(BigId, nullable=True)
    key: Mapped[str] = mapped_column(String(64))
    value_json: Mapped[str] = mapped_column(Text)
    version: Mapped[int] = mapped_column(Integer, default=1)
    updated_by: Mapped[int | None] = mapped_column(BigId, nullable=True)
