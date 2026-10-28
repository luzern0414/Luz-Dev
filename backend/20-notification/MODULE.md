# 模块：notification（通知中心）

包名 `notification`，代码 `20-notification/code/notification/`，测试 `20-notification/code/tests/`（119 个用例）。

## 0 官方来源

| 主题 | 官方页面 | 本模块用到的结论 | 核对日期 |
|---|---|---|---|
| 订阅消息发送 | https://developers.weixin.qq.com/miniprogram/dev/server/API/mp-message-management/subscribe-message/api_sendmessage.html | 参数 touser / template_id / page / data / miniprogram_state(developer\|trial\|formal) / lang；关键词类型表：thing ≤20 字、number ≤32 位数字可小数、letter ≤32 字母、symbol ≤5 符号、character_string ≤32 数字字母符号、time / date（年月日与 24 小时制，可用 ~ 连成区间）、amount（1 个币种符号 + ≤10 位数字，可带小数、结尾可带“元”）、phone_number ≤17、car_number ≤8（首尾可为汉字）、name ≤10 汉字或 ≤20 字母符号、phrase ≤5 汉字、enum 只能取枚举值；符号不能带换行等控制字符；错误码 40003 / 40037 / 43101 / 43107 / 43108 / 45168 / 47003 | 2026-10-28 |
| 订阅消息能力 | https://developers.weixin.qq.com/miniprogram/dev/framework/open-ability/subscribe-message.html | 一次性订阅：用户每 accept 一次可下发一条；推送事件 subscribe_msg_popup_event / subscribe_msg_change_event（目前只推 reject）/ subscribe_msg_sent_event（ErrorCode≠0 为异步失败）；JSON 下 List 一项时是对象、多项时是数组；下发总量：开通支付 3000 万 / 日，否则 1000 万 / 日 | 2026-10-28 |
| wx.requestSubscribeMessage | https://developers.weixin.qq.com/miniprogram/dev/api/open-api/subscribe-message/wx.requestSubscribeMessage.html | 一次最多 5 个模板；一次性与长期模板不能混用（20002）；结果 accept / reject / ban / filter，开启语音或提醒时为 acceptWithAudio / acceptWithAlert；只能在点击或支付回调后调起 | 2026-10-28 |
| 全局错误码 | https://developers.weixin.qq.com/doc/oplatform/developers/errCode/README.html | -1 系统繁忙（可重试）；40001 / 40014 / 42001 令牌失效（客户端刷新后重试）；45009 日调用量超限；45011 频率超限；43101 用户拒收（曾订阅则表示取消订阅） | 2026-10-28 |
| 消息推送 | https://developers.weixin.qq.com/miniprogram/dev/framework/server-ability/message-push.html | 验签、解密、回包由 28-wechat-mp 处理；本模块只接收解密后的事件 dict | 2026-10-28 |
| 运营规范 2.5.15 / 2.5.21 | https://developers.weixin.qq.com/miniprogram/product/index.html | 禁止诱导订阅（奖励、强制）、禁止用订阅消息骚扰或营销 → 本模块不提供群发营销订阅消息 | 2026-10-28 |
| 腾讯云短信频控 | https://cloud.tencent.com/document/product/382/37809 、https://cloud.tencent.com/document/product/382/13303 | 同号同内容 30 秒 1 条；个人认证同号 1 条 / 30 秒、5 条 / 小时、10 条 / 天；超限码 LimitExceeded.PhoneNumber*；运营商非营销短信 10 条 / 天 | 2026-10-28 |
| 阿里云短信频控 | https://help.aliyun.com/zh/document_detail/2881383.html | 验证码同号 1 条 / 分钟、5 条 / 小时、10 条 / 天；通知短信同签名同模板同号 50 条 / 天；分钟与小时窗口按 UTC+8 自然窗口；超限码 isv.BUSINESS_LIMIT_CONTROL | 2026-10-28 |
| 弹性参数 | `backend/SPEC.md` §9 | 订阅消息 / 短信超时 3s，任务重试 1m / 5m / 30m，只重试可重试错误 | 2026-10-28 |

未核实（官方原文未写，按保守方式实现）：同一模板多次 accept 是否累计次数 → 本地累计但设上限 `NOTIFY_QUOTA_CAP`（默认 50），以 43101 校正；长期订阅的开放类目 → 长期模板由后台显式标记 `long_term`。

## 1 状态与依赖

| 项 | 内容 |
|---|---|
| 状态 | **标准**（模板注册、订阅额度、发件箱投递、重试与永久失败分类、静默时段、频控、短信兜底、站内信、后台日志 / 统计 / 导出 / 重发、事件消费、任务） |
| 硬依赖 | 00-framework（db、errors、idempotency、outbox、audit、listing、export、resilience、scheduler、statemachine） |
| 软依赖（守卫导入，缺失可运行） | 28-wechat-mp（`send_subscribe_message`、`register_push_handler`）、01-identity（`User.openid / phone_enc`）、22-admin-rbac（`load_active_staff`、`grants_of`、`TOKEN_KIND_ACCESS`） |

## 2 职责与边界

| 负责 | 不负责 |
|---|---|
| 场景 → 渠道模板映射（订阅消息 / 短信 / 站内信），关键词类型校验与安全截断 | 微信 access_token、消息推送验签解密（28-wechat-mp） |
| 订阅额度：前端上报与推送事件记账、发送前占用、失败退回、43101 校正 | 业务事件的产生（各业务模块） |
| `notify()` 入发件箱 → 受理成投递行 → worker 发送（重试、退避、永久失败、日上限、静默时段、频控） | 短信云厂商 SDK（项目注入 `SmsSender`） |
| 关键场景订阅失败后短信兜底；员工告警（短信 + 站内信） | 营销群发、内容安全审核 |
| 站内信收件箱（会员与员工）、后台日志 / 统计 / 导出 / 重发 / 模板 CRUD | 企业微信、打印机渠道（见 §13 不包含） |

## 3 数据模型

| 表 | 关键字段 | 约束 / 索引 |
|---|---|---|
| `notification_scenes` | scene, name, category(transaction\|reminder\|security\|marketing\|staff_alert\|system), audience(member\|staff), urgent, sms_mode(off\|fallback\|always), enabled, created_by, updated_by | scene 唯一 |
| `notification_templates` | scene, channel(subscribe\|sms\|inapp), template_ref（微信模板 id / 短信模板 code）, keywords_json（关键词映射或短信参数）, title_tpl, body_tpl, page, long_term, enabled, version | (scene, channel) 唯一 |
| `notification_requests` | scene, audience, recipient_id, store_id, dedupe_key, data_json, source_event_id | (scene, audience, recipient_id, dedupe_key) 唯一 = 去重 |
| `notification_deliveries` | request_id, scene, channel, audience, recipient_id, recipient_ref（无 PII 检索用）, store_id, urgent, template_ref, status, attempts, next_attempt_at, errcode, errmsg, provider_msg_id, quota_reserved, fallback_from_id, sent_at | (request_id, channel) 唯一；(status, next_attempt_at)；(audience, recipient_id)；provider_msg_id |
| `notification_subscriptions` | member_id, template_id, available, long_term, status(active\|rejected\|banned), accepted_total, consumed_total, last_accept_at, last_reject_at | (member_id, template_id) 唯一 |
| `notification_subscribe_events` | dedupe_key, source(frontend\|push), event(report\|popup\|change\|sent), member_id, template_id, status, msg_id, error_code, counted | dedupe_key 唯一（防推送重投） |
| `notification_inbox` | recipient_type, recipient_id, request_id, scene, category, title, body, page, read_at, deleted_at | request_id 唯一；(recipient_type, recipient_id, deleted_at, read_at) |
| `notification_counters` | key, bucket, count | (key, bucket) 唯一；手机号只存 sha256 前缀 |

手机号、openid 不落本模块的表；发送瞬间由收件人解析器取得。

## 4 业务规则

1. **入队**：`notify(db, scene, member_id, data, dedupe_key, store_id=None)` / `notify_staff(db, scene, staff_ids, data, dedupe_key)` 只写发件箱事件 `notification.requested`，与业务写同事务；scene 形如 `order.paid`，dedupe_key 1–128 位 `[A-Za-z0-9_-:.]`，否则 4001。data 中的 datetime / date 保留类型。
2. **去重**：同 (scene, 受众, 收件人, dedupe_key) 只受理一次（先查 + 唯一约束兜底）；发件箱重复投递、业务重复调用都不会重复发送。
3. **渠道策略**：站内信模板启用即写收件箱（总是发，不受静默与频控）；会员且订阅模板启用 → 订阅消息投递；短信：`sms_mode=always` 直接投递，`fallback` 在没有订阅模板时直接投递、否则在订阅消息未送达（无额度、43101、无 openid、格式 / 缺字段、永久失败、重试耗尽、模板停用）后补一条（每请求最多一条），`off` 不发。
4. **关键词格式化**：按 key 前缀（thing1 → thing）取类型。thing 超 20 字截为 19 字 + “…”；name 截到 10（含汉字）/ 20；phrase 只允许汉字并截到 5；letter 截到 32；number、character_string、amount、phone_number、car_number、time、date、symbol、enum 不合规直接判无效（不截断，避免改变编号 / 金额含义）。整数金额按分格式化为 `¥12.34`；datetime 按门店时区格式化为 `2026年10月28日 10:00`。去控制字符、零宽字符，合并空白。无效 → 投递 `skipped / format`，从不把不合规数据发给微信。
5. **模板渲染**：占位符只允许 `{标识符}`（拒绝属性、下标、格式说明）；缺字段 → `skipped / missing_field`（关键词可配 default）。派生字段：`*_cents` → `*_yuan`，datetime → `*_text`。page 渲染失败退回无参数路径。
6. **订阅额度**：一次性模板每个 accept +1（上限 `NOTIFY_QUOTA_CAP`）；reject / 推送 change(reject) → 清零并标 rejected；ban → banned；filter 忽略；只接受已配置的模板 id，一次最多 5 个。额度来源二选一（`NOTIFY_QUOTA_SOURCE`）：同一次授权前端上报与 popup 推送都会到达，同时计数会翻倍。
7. **占用与消耗**：发送前条件 UPDATE `available > 0` 占用 1 次（与投递行标记同事务提交）；发送成功 `consumed_total + 1`；任何未送达结果退回占用。**43101 不算消耗**：退回占用后按 `NOTIFY_ON_43101`（默认 reset）把本地可用次数清零（以微信为准，避免反复 43101），长期模板标 rejected。长期模板不扣次数。
8. **错误分类**（微信）：0 成功；-1、43108、45011、40001 / 40014 / 42001、传输错误（超时、连接、熔断）→ 可重试；45009 → 延后到门店次日 00:00:05；43101 → skipped（无额度）；其他（40003、40037、41030、43107、45168、47003、未知码、发送器异常）→ failed，不重试。
9. **重试**：可重试错误按 `NOTIFY_RETRY_BACKOFF`（60, 300, 1800, 7200 秒）退避，`attempts ≥ NOTIFY_MAX_ATTEMPTS`（5）→ failed（触发短信兜底）。
10. **静默时段**：非紧急场景在门店时区 `22:00–08:00`（可配）内不发，`next_attempt_at` 延后到结束时刻（不计失败次数）；紧急场景（交易、安全、员工告警）照发。门店时区由 `set_store_timezone_resolver` 提供，缺省 `NOTIFY_TIMEZONE`。
11. **每会员频控**：非紧急场景每会员每门店自然日最多 `NOTIFY_MEMBER_NONURGENT_PER_DAY`（5）个请求走外部渠道，超出的订阅 / 短信投递直接 `skipped / member_rate_limited`，站内信照写。
12. **短信频控**（按手机号哈希、门店时区自然窗口，不高于云厂商默认值）：1 条 / 分钟（超出延后到下一分钟）、5 条 / 小时、10 条 / 天（超出 `skipped / sms_rate_limited`）；全店日预算 `NOTIFY_SMS_DAILY_CAP`（超出延后到次日）；厂商频控码（LimitExceeded.PhoneNumber*、isv.BUSINESS_LIMIT_CONTROL 等）→ skipped 不重试；厂商系统错误（InternalError*、isp.SYSTEM_ERROR 等）→ 重试。未送达时退回计数。
13. **订阅消息日上限**：全店按门店日计数，达到 `NOTIFY_SUBSCRIBE_DAILY_CAP` 延后到次日并退回占用。
14. **推送事件**：兼容 List 对象 / 数组与 XML 风格外层（SubscribeMsgPopupEvent 等）；按 (事件, openid 哈希, CreateTime, 模板, MsgID) 去重；sent 事件 ErrorCode≠0 只在投递上记录 `async:<code>`（sent 为终态）。
15. **后台重发**：只允许 failed → pending（尝试次数清零、立即到期），其他状态 4090，不存在 4040，写审计。
16. **站内信**：只能读写自己的消息（他人 4040）；标记已读幂等；全部已读可按类别；删除为软删并视为已读；保留 `NOTIFY_INBOX_RETENTION_DAYS` 天。

## 5 状态机与关键流程

投递状态（`DELIVERY_SM`，条件 UPDATE 推进，非法 4090；sent、skipped 为终态）：

| 从 | 到 | 触发 |
|---|---|---|
| pending | sending | worker 领取（多实例只有一个成功） |
| pending | sent | 站内信受理即写入 |
| pending | skipped | 场景停用 |
| sending | sent | 发送成功 |
| sending | pending | 可重试错误（attempts+1，退避）；静默 / 分钟频控 / 日上限延后（不计次数）；卡死回收 |
| sending | failed | 永久错误；重试耗尽 |
| sending | skipped | 无 openid / 手机号、无额度、43101、格式、缺字段、频控、模板停用 |
| failed | pending | 后台重发 |

发送流程（`process_delivery`）：读投递 → 场景停用则 skipped → 非紧急且在静默时段 → 延后 → 领取 pending→sending 并提交 → 解析收件人 → 渲染与格式校验 → 占用额度（提交）→ 日上限计数 → **事务外**经 `resilient("notify.subscribe", timeout 3s, breaker)` 调用发送器 → 新事务写结果（成功记消耗；失败退回额度与计数、分类、必要时生成短信兜底）。进程在 sending 中崩溃：`notification.reclaim` 把超过 300 秒的 sending 退回 pending，已占用的额度保留不重复扣（至少一次语义）。

## 6 接口

会员端前缀 `/api/v1/app/notifications`（会员 access 令牌）；后台前缀 `/api/v1/admin/notifications`（员工令牌）。写接口全部要求 `Idempotency-Key`，同键重放返回首次结果，同键不同请求体 4093。agent 列：均为 no。

| 方法 | 路径 | 鉴权 / 权限码 | 幂等 | 说明 |
|---|---|---|---|---|
| GET | `/app/notifications/subscribe-plan?scenes=a,b` | 会员 | — | wx.requestSubscribeMessage 前取模板 id（≤5，混用一次性 / 长期 4220） |
| GET | `/app/notifications/subscriptions` | 会员 | — | 各场景模板 id、剩余次数、状态（“开启提醒”入口） |
| POST | `/app/notifications/subscriptions` | 会员 | 是 | 上报授权结果 `{results: {模板id: accept\|reject\|ban\|filter\|acceptWith…}}` |
| GET | `/app/notifications/messages` | 会员 | — | 站内信游标列表；筛选 category、read_at_null、created_at 区间 |
| GET | `/app/notifications/messages/unread-count` | 会员 | — | `{total, by_category}` |
| POST | `/app/notifications/messages/{id}/read` | 会员 | 是 | 标记已读（他人 4040） |
| POST | `/app/notifications/messages/read-all` | 会员 | 是 | `{category?}` |
| DELETE | `/app/notifications/messages/{id}` | 会员 | 是 | 软删 |
| GET | `/admin/notifications/deliveries` | `notification:read` | — | 投递日志：scene、channel、status、errcode（含 errcode_null）、audience、recipient_id、store_id、attempts 区间、created_at / sent_at 区间、keyword（会员号 / 场景 / 消息 id）；排序 created_at、sent_at、attempts、scene |
| GET | `/admin/notifications/deliveries/export` | `notification:export` | — | CSV，同列表筛选，≤31 天、≤5000 行（超出 4225），写审计 |
| POST | `/admin/notifications/deliveries/{id}/resend` | `notification:resend` | 是 | `{reason?}`，failed → pending，写审计 |
| GET | `/admin/notifications/stats` | `notification:read` | — | 发送量、按渠道 × 状态、失败错误码分布、额度不足率；筛选 scene、channel、audience、store_id、created_at（≤92 天） |
| GET | `/admin/notifications/scenes` | `notification:read` | — | 场景列表（含模板），筛选 category、audience、enabled、keyword |
| GET | `/admin/notifications/scenes/{scene}` | `notification:read` | — | 场景详情 |
| POST | `/admin/notifications/scenes` | `notification:template` | 是 | 新建场景（重名 4090） |
| PUT | `/admin/notifications/scenes/{scene}` | `notification:template` | 是 | 修改名称、类别、urgent、sms_mode、enabled |
| DELETE | `/admin/notifications/scenes/{scene}` | `notification:template` | 是 | 删除场景及其模板（历史投递保留） |
| PUT | `/admin/notifications/scenes/{scene}/templates/{channel}` | `notification:template` | 是 | 新建 / 修改渠道模板，保存前校验关键词类型、占位符、page |
| DELETE | `/admin/notifications/scenes/{scene}/templates/{channel}` | `notification:template` | 是 | 删除渠道模板 |
| GET | `/admin/notifications/inbox`、`/inbox/unread-count` | 员工登录 | — | 员工站内信 |
| POST / DELETE | `/admin/notifications/inbox/{id}/read`、`/inbox/read-all`、`/inbox/{id}` | 员工登录 | 是 | 员工站内信已读 / 删除 |

权限码（`notification.auth.PERMISSIONS`）：`notification:read`、`notification:export`、`notification:resend`、`notification:template`；支持 `notification:*` 与 `*`。

## 7 事件

| 方向 | 主题 | 内容 |
|---|---|---|
| 发出 | `notification.requested` | notify / notify_staff 写入；本模块自己消费 |
| 消费 | `payment.succeeded` → `order.paid` | 收件人 `user_id`，去重 `pay:{out_trade_no}` |
| 消费 | `refund.succeeded` → `refund.succeeded` | `user_id`，`refund:{out_refund_no}` |
| 消费 | `member_card.expiring` → `card.expiring` | `user_id`，`card:{card_id}:{expire_date}` |
| 消费 | `booking.reminder` → `booking.reminder` | `user_id`，`booking:{booking_id}:remind` |
| 消费 | `account.security_alert` → `security.alert` | `user_id`，`sec:{event_id}` |
| 消费 | `lottery.prize_won` → `lottery.prize_won` | 仅 `prize_kind == "physical"`；收件人 `member_id`，去重 `prize:{draw_id}`；payload `{draw_id, activity_id, member_id, prize_kind, prize_name, delivery_key, …}`，提醒中奖者填写收货信息领取 |
| 消费 | `device.offline` → `staff.device_offline` | `staff_ids`（员工），`device:{device_id}:{offline_at}` |

映射集中在 `consumers.EVENT_BINDINGS`（`EventBinding.when` 做字段过滤），用 `set_event_binding()` 改主题名 / 字段名；去重模板缺字段时退回 event_id。payload 里 `*_at` / `*_date` 字符串自动转为时间类型。接入（25-jobs 约定）：`notification/events.py` 的 `SUBSCRIPTIONS = {topic: handler(db, payload, event_id)}`，覆盖 `notification.requested` 与上表全部主题；handler 不提交（worker 成功提交、异常回滚并退避重试），幂等靠请求去重键唯一（重投返回 0）；`set_event_binding` 同步增删该表。备用：`dispatch_pending(db, consumers.outbox_handler)` 或 `broker.consume(topic, group, consumers.consume_message)`（自开会话并提交）；推送：`handle_push_event(db, message)`（wechat_mp 提供 `register_push_handler` 时导入即自动登记三类事件）。

## 8 并发与一致性

- 领取投递：`UPDATE … WHERE status='pending'`，rowcount 判定（10 线程同一投递只发 1 次，见测试）。
- 额度占用：`UPDATE … WHERE available > 0 AND status='active'`（50 线程抢 10 次额度恰好 10 次）。
- 频控计数：`UPDATE … WHERE count < limit` + (key, bucket) 唯一约束插入（50 线程上限 7 恰好 7 次）；独立小事务提交，失败退回。
- 去重：请求唯一键、收件箱 request_id 唯一、推送事件 dedupe_key 唯一。
- 事务内不做外部调用；外部调用经 `resilient`（超时 3s、熔断 `notify.subscribe` / `notify.sms`）；卡死回收任务兜底。
- 后台写与审计同事务（run_idempotent 提交）。

定时任务（`notification/jobs.py` 的 `JOBS` 元组，`luzback.scheduler.Job` 实例，由 25-jobs 加载；也可 `register(registry)`）：

| 任务 | 周期 | 说明 |
|---|---|---|
| `notification.dispatch` | 15 秒 | 投递到期 pending |
| `notification.reclaim` | 60 秒 | sending 超时退回 pending |
| `notification.purge` | 每天 | 清理 3 天前的计数、超保留期 / 删除 30 天的站内信 |

## 9 配置

| 键 | 默认 | 说明 |
|---|---|---|
| `NOTIFY_TIMEZONE` | `LUZ_TIMEZONE` 或 Asia/Shanghai | 无门店时区时使用 |
| `NOTIFY_QUIET_ENABLED` / `NOTIFY_QUIET_START` / `NOTIFY_QUIET_END` | true / 22:00 / 08:00 | 非紧急场景静默时段 |
| `NOTIFY_RETRY_BACKOFF` | 60,300,1800,7200 | 秒 |
| `NOTIFY_MAX_ATTEMPTS` | 5 | 达到即 failed |
| `NOTIFY_SEND_TIMEOUT` | 3 | 秒（SPEC §9） |
| `NOTIFY_SENDING_STUCK_SECONDS` | 300 | 卡死回收阈值 |
| `NOTIFY_BATCH_SIZE` | 100 | 每轮投递数 |
| `NOTIFY_MINIPROGRAM_STATE` / `NOTIFY_LANG` | formal / zh_CN | send 参数 |
| `NOTIFY_QUOTA_SOURCE` | frontend | frontend \| push |
| `NOTIFY_QUOTA_CAP` | 50 | 单模板累计上限 |
| `NOTIFY_ON_43101` | reset | reset \| keep |
| `NOTIFY_SUBSCRIBE_DAILY_CAP` | 10000000 | 未开通支付的官方日上限；开通支付可调到 30000000 |
| `NOTIFY_SMS_PER_MINUTE` / `_PER_HOUR` / `_PER_DAY` | 1 / 5 / 10 | 每手机号 |
| `NOTIFY_SMS_DAILY_CAP` | 100000 | 全店日预算 |
| `NOTIFY_MEMBER_NONURGENT_PER_DAY` | 5 | 每会员非紧急请求 |
| `NOTIFY_INBOX_RETENTION_DAYS` | 180 | 站内信保留 |
| `NOTIFY_CURSOR_SECRET` | `LUZ_JWT_SECRET` | 站内信游标签名 |

## 10 错误码

| code | 场景 |
|---|---|
| 4001 | 参数错误：scene / dedupe_key 格式、模板关键词类型、占位符、page、上报超过 5 个、未知筛选参数、导出超过 31 天、缺幂等键 |
| 4010 | 未登录 / 令牌类型不符 |
| 4030 | 缺少 `notification:*` 权限码 |
| 4040 | 场景 / 模板 / 投递不存在；他人站内信 |
| 4090 | 场景重名；重发非 failed 投递；并发修改订阅 |
| 4092 / 4093 | 幂等键处理中 / 被不同请求体复用 |
| 4220 | 一次性与长期模板混在一次授权请求 |
| 4225 | 导出超过 5000 行 |

投递层错误码（`notification_deliveries.errcode`）：微信 errcode 原值（如 `43101`、`47003`）、短信厂商码、`transport`、`exception`、`channel_not_configured`、`no_quota`、`no_openid`、`no_phone`、`format`、`missing_field`、`member_rate_limited`、`sms_rate_limited`、`quiet_hours`、`daily_cap`、`sms_daily_cap`、`sms_minute_limit`、`template_disabled`、`scene_disabled`、`stuck`、`async:<code>`。

## 11 测试清单

| 文件 | 覆盖 |
|---|---|
| `test_notification_rules.py`（51） | 每种关键词类型的合法 / 截断 / 无效；控制字符；占位符注入；映射校验；静默时段跨午夜与门店时区；微信与短信错误码分类；退避；推送 List 对象 / 数组 / XML 外层 / sent 事件 |
| `test_notification_service.py`（60） | 发件箱入队与端到端投递、去重；渠道策略；额度消耗、无额度、43101 不消耗（reset / keep）、长期模板、拒收 / 封禁 / 上限；推送计数模式与重投去重；-1 重试退避、传输错误耗尽后 failed + 短信兜底、异常永久失败、45009 与日上限延后、格式错误不发送；卡死回收不重复扣；静默时段（FakeClock）与门店时区钩子；每会员频控；员工告警短信、短信分钟 / 小时 / 天频控、厂商频控码；事件绑定与覆盖；站内信全流程；重发状态边与审计；列表筛选与统计；CSV 导出；模板 CRUD 与校验；任务注册；events.SUBSCRIPTIONS 约定（不提交、回滚后重放、重投幂等、实物奖品过滤、绑定同步）；默认发送器；wechat_mp 适配器；并发（额度、计数、多 worker 同一投递） |
| `test_notification_api.py`（8） | 会员 / 员工 4010、4030、4001、幂等重放与 4093、4040、信封；订阅上报；站内信；投递日志筛选、导出、重发、统计；场景与模板 CRUD；员工站内信 |

运行：`cd backend && python -X utf8 -m pytest -q 20-notification`。

## 12 前端对接

| 页面 | 接口 | 行为 |
|---|---|---|
| 下单成功 / 预约成功 / 会员卡页（点击“开启提醒”） | `GET subscribe-plan` → `wx.requestSubscribeMessage` → `POST subscriptions` | 只在用户点击或支付回调后调起；拒绝不影响业务；不得用奖励诱导订阅（2.5.21） |
| 我的 → 消息提醒设置 | `GET subscriptions` | 显示各提醒剩余次数，次数为 0 时引导再次开启 |
| 消息中心 | `GET messages`（游标）、`unread-count`、`read`、`read-all`、`DELETE` | 角标用 unread-count；点击消息跳 page |
| 后台：通知日志 | `deliveries`、`export`、`resend`、`stats` | 失败行显示 errcode 含义，按钮“重发”只对 failed 显示 |
| 后台：通知模板 | `scenes`、`templates` | 填写小程序后台申请的模板 id 与关键词（thing1 …）对应字段，保存即校验 |
| 后台顶栏 | `inbox`、`inbox/unread-count` | 员工告警（设备离线、安全告警） |

## 13 定制点

| 类型 | 入口 |
|---|---|
| 配置 | §9 环境变量；运行时 `notification.set_config(**overrides)` |
| 订阅消息发送器 | `set_subscribe_sender(SubscribeMessageSender)`；默认：wechat_mp 可用 → `WechatMpSubscribeSender`，否则生产 `NotConfiguredSender`（failed / channel_not_configured）、开发 `FakeSubscribeSender` |
| 短信发送器 | `set_sms_sender(SmsSender)`：`send(phone, template_code, params) -> SendResult(ok, errcode, errmsg, msg_id, retryable)`；默认同上（生产未配置即失败可见） |
| 收件人 | `set_recipient_resolver(RecipientResolver)`：`member(db, id)`、`staff(db, id)`、`member_id_by_openid(db, openid)`；默认读 identity / admin_rbac |
| 门店时区 | `set_store_timezone_resolver(fn(store_id) -> tz)` |
| 事件映射 | `set_event_binding(topic, [EventBinding(scene, recipient_field, dedupe, audience, store_field, fields)])` |
| 场景与模板 | 后台 CRUD；默认 8 个场景（含 `lottery.prize_won`）；种子 `ensure_default_scenes(db)`（`defaults.DEFAULT_SCENES`，外部渠道模板 id 留空停用） |
| 鉴权 | `set_permission_resolver(fn(db, staff_id, claims))`、`set_member_checker(fn(db, claims))` |
| 时钟 | `set_clock(fn)`（测试用 FakeClock） |

期望 28-wechat-mp 提供：`send_subscribe_message(touser, template_id, data, page=None, miniprogram_state="formal", lang="zh_CN") -> dict{errcode, errmsg, msgid}`（非 0 errcode 也可抛带 `.errcode/.errmsg` 的 `WechatError`；内部完成 access_token 获取与 40001/42001 刷新重试一次；传输错误抛 `TransientError` / `UpstreamError`）；`register_push_handler(event: str, fn(db, message: dict))`（验签解密后按 Event 分发）。

不包含：营销群发与人群圈选；企业微信 / 打印机 / 邮件 / App 推送渠道；新版一次性订阅（服务卡片 setUserNotify）；小程序虚拟支付相关通知；短信验证码（01-identity 负责）；短信退订处理；内容安全审核（msgSecCheck）。
