# 智能体项目清单模板（AGENT.md）

复制为 `agent/requirements/<项目名>.md`。每项标注来源"用户 / 默认"；未提及的默认关。规范依据：智能体底座 `../../references/agent-foundation.md`（默认值见 §16）与门店业务助手应用规范 `../backend/SPEC.md`。填好后生成项目的 `config/intents.yaml`、`config/tools.yaml`。

## 1 基本

| 项 | 值 | 来源 |
|---|---|---|
| 项目名 / 店名 / 行业 | | |
| 启用端 | 小程序 ☐ 后台 ☐ H5 ☐ | |
| 助手名称与头像 | "{店名}助手" / 店 logo | 默认 |
| 入口 | 首页浮窗 ☐ 我的 ☐ 订单详情 ☐ 商品详情 ☐ 后台抽屉 ☐ | |
| 首次说明文案 | "回答由 AI 生成，下单前请核对金额与地址" | 默认 |

## 2 意图（勾选启用；未勾选归 out_of_scope）

| 意图 | 启用 | 备注（槽位 / 文案差异） |
|---|---|---|
| greet_chitchat | ☑ | |
| product_search / product_compare | ☐ | |
| cart_add | ☐ | 需 `cart` 模块 |
| order_create | ☐ | 需 `orders/preview` |
| order_query | ☐ | |
| order_modify | ☐ | 取消 / 改地址 / 催单各自勾选 |
| after_sales | ☐ | 草稿 → 转人工 |
| member_query / coupon_claim / recharge | ☐ | |
| store_info | ☐ | |
| queue_booking | ☐ | 需 `queue` 模块 |
| policy_faq / usage_help | ☐ | 需知识库 |
| admin_report / admin_draft（后台） | ☐ | |
| handoff | ☑ | 客服入口：客服会话页 / 电话 |

## 3 工具（按 `backend/API.md` 状态勾选；"设计 / 待建"接口的工具注册为 disabled）

| 工具 | 风险 | 启用 | 接口状态 |
|---|---|---|---|
| search_products / get_product | read | ☐ | |
| cart_add | low_write | ☐ | |
| price_preview / create_order | read / high | ☐ | |
| list_orders / get_order / get_delivery | read | ☐ | |
| cancel_order / update_order_address / urge_order | high / high / low | ☐ | |
| list_addresses / delivery_check | read | ☐ | |
| get_member / get_points / list_coupons / claim_coupon | read / low | ☐ | |
| get_store | read | ☐ | |
| queue_create / queue_current / queue_cancel | high / read / high | ☐ | |
| create_after_sales_draft | high | ☐ | |
| kb_search | read | ☐ | |
| admin_* | read / high | ☐ | |

## 4 知识库

| 项 | 值 |
|---|---|
| 文档来源 | 退换规则 ☐ 配送规则 ☐ 发票 ☐ 会员权益 ☐ 积分规则 ☐ 过敏原 ☐ 花材养护 ☐ 活动说明 ☐ 系统用法 ☐ |
| 嵌入模型 / 向量库 | |
| top-k / 最低分 | 6 / 0.35（默认） |
| 评测集条数 | ≥ 30 |

## 5 模型与预算

| 项 | 值 |
|---|---|
| 主模型 / 备模型 | `provider:model` / `provider:model` |
| 仅私有部署模型 | ☐ |
| 每轮 token 上限 / 每用户每日轮次 / 租户日预算 | 8000 / 200 / ¥ |
| 超时（模型 / 首 token） | 20s / 8s |

## 6 记忆

| 项 | 值 |
|---|---|
| 长期记忆 | 开 ☐ 关 ☐ |
| 允许类型 | 忌口 ☐ 过敏 ☐ 辣度 ☐ 常用称呼 ☐ 常点品类 ☐ 花材偏好 ☐ 送达偏好 ☐ |
| 用户可见 / 可删除入口 | 设置 → 助手记忆（默认） |

## 7 降级与文案

| 级别 | 文案（可改） |
|---|---|
| L1 无知识库 | 规则说明暂时查不到，你可以在「我的 → 帮助」查看，或联系门店。 |
| L3 无模型 | 助手现在有点忙，先给你直接查：[最近订单] [营业时间] [转人工] |
| L4 只读 | 现在不能帮你直接下单，点这里去结算页。 |
| L5 停用 | 助手维护中，预计 HH:MM 恢复。 |

## 8 快捷回复（首屏 3 个）

1. 
2. 
3. 

## 9 待确认项

| 编号 | 问题 | 建议默认 |
|---|---|---|
| 1 | | |

## 10 验收（按底座 §17 检查清单逐项核对，并复制 `../backend/SPEC.md` §18 应用验收；界面侧按 Luz-System `references/agent-design.html` 的质量门禁与 `references/handoff.md` 勾选）
