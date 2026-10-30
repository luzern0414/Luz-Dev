"""展示键登记：小程序数据展示方式（模块顺序与显示隐藏、展示开关、标签名称、数量上限）的类型、范围与默认值。

值存在 display_settings（全局一行、门店覆盖各一行）；没有行时用这里的默认值。admin_visible=False 的键存后端、照常下发小程序，
但不在后台展示、后台接口拒绝修改——由开发改这里的默认值并经代码评审（技术性展示配置）。
public=False 的键只给服务端用，不进 /api/v1/app/content。

项目新增展示键：在模块导入时 `register_display_key(DisplayKey(...))`，同名覆盖；类型只有下面几种，值按类型与范围校验。
"""
from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Any, Literal

Kind = Literal["bool", "int", "text", "choice", "order"]


@dataclass(frozen=True)
class DisplayKey:
    key: str                                  # 分组.名称，如 product.show_sales
    label: str                                # 给运营看的名称
    page: str                                 # 所在小程序页面，后台按它分组
    kind: Kind
    default: Any
    description: str = ""                     # 位置说明与影响
    admin_visible: bool = True
    public: bool = True                       # 是否下发小程序
    store_scope: bool = True                  # 能否按门店覆盖
    minimum: int | None = None                # int
    maximum: int | None = None                # int
    max_length: int | None = None             # text
    choices: tuple[str, ...] = ()             # choice：单选；order：从中选若干并排序（没选的即隐藏）
    sort_order: int = 0

    def validate(self, value: Any) -> Any:
        """返回规范化后的值；不合法抛 ValueError（说明允许范围）。默认值在登记时也走这里。"""
        if self.kind == "bool":
            if not isinstance(value, bool):
                raise ValueError("must be true or false")
            return value
        if self.kind == "int":
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValueError("must be an integer")
            if (self.minimum is not None and value < self.minimum) or (self.maximum is not None and value > self.maximum):
                raise ValueError(f"must be between {self.minimum} and {self.maximum}")
            return value
        if self.kind == "text":
            if not isinstance(value, str):
                raise ValueError("must be text")
            value = value.strip()
            if not value:
                raise ValueError("must not be empty")
            if self.max_length is not None and len(value) > self.max_length:
                raise ValueError(f"must be at most {self.max_length} characters")
            return value
        if self.kind == "choice":
            if value not in self.choices:
                raise ValueError("must be one of: " + ", ".join(self.choices))
            return value
        if self.kind == "order":
            if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
                raise ValueError("must be a list of module names")
            unknown = [v for v in value if v not in self.choices]
            if unknown:
                raise ValueError("unknown items: " + ", ".join(unknown) + "; allowed: " + ", ".join(self.choices))
            if len(set(value)) != len(value):
                raise ValueError("items must not repeat")
            return list(value)
        raise ValueError(f"unknown kind {self.kind}")

    def describe(self) -> dict[str, Any]:
        out: dict[str, Any] = {"key": self.key, "label": self.label, "page": self.page, "kind": self.kind,
                               "description": self.description, "default": self.default, "store_scope": self.store_scope}
        if self.kind == "int":
            out.update(minimum=self.minimum, maximum=self.maximum)
        if self.kind == "text":
            out["max_length"] = self.max_length
        if self.kind in ("choice", "order"):
            out["choices"] = list(self.choices)
        return out


_lock = threading.Lock()
DISPLAY_KEYS: dict[str, DisplayKey] = {}


def register_display_key(item: DisplayKey) -> DisplayKey:
    item.validate(item.default)  # 默认值也要合法
    if len(item.key) > 64:
        raise ValueError(f"display key too long: {item.key}")
    with _lock:
        DISPLAY_KEYS[item.key] = item
    return item


def get_display_key(key: str) -> DisplayKey | None:
    return DISPLAY_KEYS.get(key)


def display_keys() -> list[DisplayKey]:
    with _lock:
        return sorted(DISPLAY_KEYS.values(), key=lambda k: (k.page, k.sort_order, k.key))


HOME_MODULES = ("banner", "notice", "categories", "recommend", "coupons", "new_products")

for _item in (
    DisplayKey("home.modules", "首页模块顺序与显示", "home", "order", list(HOME_MODULES[:5]), choices=HOME_MODULES,
               description="首页从上到下显示哪些模块、按什么顺序；没选的模块不显示", sort_order=10),
    DisplayKey("home.recommend_limit", "首页推荐商品数量", "home", "int", 6, minimum=1, maximum=30,
               description="首页“推荐”模块最多展示几件商品", sort_order=20),
    DisplayKey("product.show_sales", "显示销量", "product", "bool", True,
               description="商品卡片与详情页是否显示“已售 N”", sort_order=10),
    DisplayKey("product.show_stock", "显示剩余库存", "product", "bool", False,
               description="库存低于提醒阈值时是否显示“仅剩 N 件”", sort_order=20),
    DisplayKey("product.low_stock_threshold", "库存提醒阈值", "product", "int", 10, minimum=1, maximum=999,
               description="剩余库存不超过这个数时显示“仅剩 N 件”（需打开“显示剩余库存”）", sort_order=30),
    DisplayKey("product.show_reviews", "显示评价", "product", "bool", True,
               description="商品详情页是否显示评价区与好评率", sort_order=40),
    DisplayKey("category.hot_label", "热销标签名称", "menu", "text", "热销", max_length=6,
               description="点菜页热销分类与商品角标上的文字", sort_order=10),
    # 技术性展示配置：存后端并下发，不在后台展示，由开发改默认值
    DisplayKey("list.page_size", "列表每页条数", "common", "int", 20, minimum=10, maximum=50, admin_visible=False,
               store_scope=False, description="小程序列表每次加载的条数（与接口分页上限一致）"),
    DisplayKey("product.image_ratio", "商品图比例", "product", "choice", "1:1", choices=("1:1", "4:3", "3:4"),
               admin_visible=False, store_scope=False, description="商品卡片图片裁切比例，随设计稿调整"),
):
    register_display_key(_item)
