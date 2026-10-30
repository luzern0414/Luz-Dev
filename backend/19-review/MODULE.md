# 模块：19-review（评价）`review`

| 项 | 内容 |
|---|---|
| 状态 | 核心已实现（本人已完成订单、每单一评、评分与长度校验、敏感词转审核）；审核后台与评价回复为设计 |
| 代码 | `review/code/review/`、测试 `review/code/tests/` |
| 依赖 | 00-framework、08-order（读取订单归属与状态） |

## 1 职责与边界

| 负责 | 不负责 |
|---|---|
| 提交评价（评分、文字、图片） | 订单状态流转（order） |
| 每笔订单只能评价一次 | 内容安全的最终裁决（人工审核，设计） |
| 敏感内容转待审核 | 商家回复的业务规则（设计） |
| 评价列表与评分汇总（设计） | 积分奖励（membership，订单完成后另行入账） |

## 2 数据模型

| 表 | 字段 | 约束 | 说明 |
|---|---|---|---|
| `reviews` | id, order_id, user_id, rating(1–5), content(≤500), status(pending/visible/hidden), created_at | `order_id` 唯一 | 一单一评 |
| `review_images`（设计） | id, review_id, url, sort | — | 图片经对象存储，只存地址 |

## 3 接口

| 方法 | 路径 | 鉴权 | 幂等 | agent | 说明 |
|---|---|---|---|---|---|
| POST | `/api/v1/app/orders/{id}/review` | access（归属） | 必须 | no | body: `{rating, content, image_urls?}` |
| GET | `/api/v1/app/skus/{id}/reviews` | 可选 | — | read | 评价列表（只返回 visible） |
| GET | `/api/v1/app/skus/{id}/review-summary` | 可选 | — | read | 评分均值与数量（设计） |
| GET | `/api/v1/admin/reviews?status=pending` | admin `review:read` | — | no | 待审核列表（设计） |
| POST | `/api/v1/admin/reviews/{id}/moderate` | admin `review:moderate` | 必须 | no | 通过 / 隐藏（审计，设计） |

## 4 关键规则

| 规则 | 说明 |
|---|---|
| 评价资格 | 订单属于当前用户，且状态为 completed |
| 时效 | 订单完成后 30 天内可评价（设计） |
| 一单一评 | `order_id` 唯一约束 |
| 评分 | 整数 1–5 |
| 内容 | 不超过 500 字；图片最多 9 张 |
| 敏感词 | 命中词库 → `status = pending`，不对外展示；未命中 → `visible` |
| 匿名 | 展示昵称脱敏（设计） |

## 5 状态机

`pending → visible`（审核通过）；`visible → hidden`（违规下架）；`pending → hidden`（审核拒绝）。

## 6 并发与一致性

| 场景 | 机制 |
|---|---|
| 重复提交评价 | 幂等键 + `order_id` 唯一约束（IntegrityError → 4090） |
| 评价与订单状态同时变化 | 评价只读订单状态；订单在完成后不会再变回未完成 |

## 7 配置

目前代码不读配置（评分范围、正文长度、敏感词是代码常量）。实现评价期限与可维护词库时：评价期限（天）作为运行参数写进运行配置 `[review]` 分组；敏感词库属于经营内容，存数据库由后台维护，或接平台内容安全接口。

## 8 错误码

| 场景 | code | HTTP |
|---|---|---|
| 评分越界、内容过长 | 4001 | 400 |
| 订单不存在或不属于用户 | 4040 | 404 |
| 订单未完成 | 4220 | 422 |
| 已评价 | 4090 | 409 |

## 9 测试用例（已实现）

| 用例 | 断言 |
|---|---|
| 本人已完成订单评价 | visible |
| 他人订单 | 4030 |
| 未完成订单 | 4220 |
| 重复评价 | 4090 |
| 评分 0 或 6 | 4001 |
| 敏感词 | pending |

## 10 可复用代码

`review/code/review/__init__.py`：`Review`、`create_review`、敏感词检查函数 `_moderate`（替换为内容安全服务即可）。

## 11 前端对接

| 页面 | 接口 | 行为 |
|---|---|---|
| 订单详情（已完成） | — | 显示“去评价”按钮；已评价显示星级只读 |
| 评价页 | POST review | 未选星级禁用提交；pending 结果提示“评价已提交，审核通过后展示” |
| 商品详情 | reviews | 展示 visible 评价，最多 3 条，“查看全部”进入列表页 |
