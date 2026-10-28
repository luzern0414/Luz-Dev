# 模块：notification（消息通知）

| 项 | 内容 |
|---|---|
| 状态 | **仅设计，代码未实现**（发件箱与重试机制已在 framework 实现，本模块负责渠道与模板） |
| 依赖 | framework（outbox）、auth-login（用户 openid）、admin-rbac（运营人员通知） |

## 1 职责与边界

| 负责 | 不负责 |
|---|---|
| 消费发件箱事件，生成通知 | 业务事件的产生（各业务模块写入发件箱） |
| 渠道投递：小程序订阅消息、短信、企业微信 | 支付回调 |
| 模板管理与变量校验 | 营销群发的内容审核（运营流程） |
| 投递记录、失败重试、人工重发 | — |

## 2 数据模型（设计）

| 表 | 字段 | 说明 |
|---|---|---|
| `notification_templates` | id, channel, code, template_id, variables, enabled | 渠道模板映射 |
| `notifications` | id, user_id, channel, template_code, payload_json, status(pending/sent/failed/skipped), attempts, last_error, created_at | 每条通知一行，便于审计与重发 |
| `subscribe_consents` | user_id, template_code, count, updated_at | 小程序订阅消息授权次数（一次授权只能发一条） |

## 3 事件与模板（示例）

| 事件（topic） | 通道 | 模板 | 触发条件 |
|---|---|---|---|
| `order.paid` | 订阅消息 | 下单成功 | 支付成功 |
| `order.shipped` | 订阅消息 | 配送提醒 | 骑手取餐 |
| `delivery.delivered` | 订阅消息 | 送达通知 | 送达 |
| `queue.called` | 订阅消息（优先）/ 短信 | 叫号提醒 | 叫号 |
| `member.expiring` | 短信 | 会员到期 | 到期前 7 天 |

## 4 规则

- **订阅消息**：每次用户授权只能发一条；发送前检查 `subscribe_consents.count > 0`，发送后扣减；没有授权次数则跳过并记录 `skipped`。
- **短信**：同一手机号每日上限（默认 5 条）；营销短信必须包含退订方式。
- **失败重试**：指数退避（1 分钟、5 分钟、30 分钟），最多 5 次；超过后进入人工队列。
- **幂等**：`notifications` 以 `(event_id, channel, user_id)` 唯一，防止发件箱重复投递造成重复通知。

## 5 接口

| 方法 | 路径 | 鉴权 | agent | 说明 |
|---|---|---|---|---|
| POST | `/api/v1/app/subscribe-consents` | access | no | 上报用户授权（小程序 `requestSubscribeMessage` 成功后调用） |
| GET | `/api/v1/admin/notifications?status=failed` | admin `notify:read` | no | 失败列表 |
| POST | `/api/v1/admin/notifications/{id}/resend` | admin `notify:resend` | no | 人工重发（审计） |

## 6 并发与一致性

投递 worker 与发件箱 `dispatch_pending` 配合；同一事件可能被投递多次，依赖 `notifications` 的唯一键去重。授权次数扣减使用条件 UPDATE `count > 0`。

## 7 配置

| 变量 | 说明 |
|---|---|
| `WECHAT_SUBSCRIBE_TEMPLATE_IDS` | 各模板的微信模板 ID（JSON） |
| `SMS_PROVIDER` / `SMS_SIGN` | 短信服务商与签名 |

## 8 错误码

通知为内部能力，对外只有管理接口：权限不足 4030；通知不存在 4040；重发非失败状态 4090。

## 9 测试计划（实现时补）

授权次数扣减、重复事件去重、重试退避、超限转人工、退订短信不发送。

## 10 前端对接

小程序在关键操作前调用 `wx.requestSubscribeMessage`（如下单成功页、取号成功页），授权结果上报 `subscribe-consents`。拒绝授权不影响业务流程，页面提供“开启提醒”入口。
