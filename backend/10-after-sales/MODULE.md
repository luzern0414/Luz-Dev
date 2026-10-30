# 模块：10-after-sales（售后：仅退款 / 退货退款）`after_sales`

## 0 官方来源

简写 `DOC/x` = `https://pay.weixin.qq.com/doc/v3/merchant/x`；核对日期均为 2026-10-28。

| 主题 | 官方页面 | 本模块用到的结论 | 核对日期 |
|---|---|---|---|
| 七日无理由退货 | 网络购买商品七日无理由退货暂行办法第六、七、十、十二、十三、十四、十五、十七条（https://www.gov.cn/zhengce/2020-11/03/content_5723721.htm） | 签收次日起 7 日内可申请（`no_reason` 只对 returnable 行）；收到退货之日起 7 日内退款（本模块收货即发起退款）；退款方式与付款方式一致（原路）；券按原方式退回（交 16 策略）；部分退按各行实付计算 | 2026-10-28 |
| 交付时间 | 电子商务法第五十一条（https://www.cac.gov.cn/2018-09/01/c_1123362506_2.htm ，网信办转载） | 签收时间为交付时间：窗口起点 = 订单 fulfilled_at；未签收的订单随时可申请 | 2026-10-28 |
| 预付式消费退款 | 法释〔2025〕4号（https://www.court.gov.cn/zixun/xiangqing/459321.html）第十四、十五条 | 会员卡行的可退金额由 29-member-card 按已兑付扣减计算（`refund_cap`），本模块只取其为上限 | 2026-10-28 |
| 原路退款 | 小程序运营规范 §2.14 第 6 条（https://developers.weixin.qq.com/miniprogram/product/） | 资金原路退回，不得强制退到余额 / 储值卡 / 积分 / 券；原路无法退回时替代方式不得附加条件（人工结清须记录说明） | 2026-10-28 |
| 申请退款 | DOC/4012791903 | 支付后 365 天内；部分退款最多 50 次、同一笔间隔 ≥ 1 分钟、每次新 out_refund_no；失败重试沿用原单号；受理 ≠ 成功（由 09 实现，本模块按 4220 `refund_interval` 延后重提） | 2026-10-28 |
| 退款结果通知 / 异常退款 | DOC/4012791906、DOC/4013071200 | SUCCESS / CLOSED / ABNORMAL；ABNORMAL 需商户发起异常退款（09 后台），之后再成功 | 2026-10-28 |

参考设计（非官方）：芋道售后 APPLY → SELLER_AGREE → BUYER_DELIVERY → WAIT_REFUND → COMPLETE、同一订单项仅一个进行中售后；002Gym 本地补偿只在渠道 SUCCESS 后执行、过期券退回为 expired（R7 §3、§B-2）。

## 1 状态与依赖

| 项 | 内容 |
|---|---|
| 状态 | 标准（可复用）；测试在 `code/tests/`（另有端到端在 `backend/tests/test_integration.py`） |
| 依赖 | 00-framework；08-order（refund_quote / reserve_refund / release_refund / settle_refund / refund_balance / get_owned / admin_get）；09-wechat-pay（create_refund、RefundRecord 只读防重、refund.* 事件）；15-recharge（经 08 冲回余额）；16-coupon（经 08 退券）；29-member-card（经 08 行种类取退卡上限）；01-identity（current_user）；22-admin-rbac（权限、二次验证、门店范围） |
| 被依赖 | 20-notification（消费 after_sale.* 事件）、29-member-card（建议消费 after_sale.completed 终止卡，见 §13） |

## 2 职责与边界

| 负责 | 不负责 |
|---|---|
| 申请：按行数量 / 金额、原因、凭证（media id） | 订单金额分摊与退款额度账本（08） |
| 审核：同意 / 驳回、职责分离、二次验证、自动同意 | 退款渠道协议、out_refund_no、退款查询（09） |
| 退货：买家寄回（快递单号）、商家确认收货 | 物流轨迹（11） |
| 原路退款：微信（09）/ 余额（15，经 08） | 会员卡退卡计算与卡状态（29） |
| 退款结果处理、失败 / 异常人工处理、超时任务 | 补发 / 换货（不包含） |
| 顾客端与后台查询、导出 | 消息推送（20） |

## 3 数据模型

| 表 | 关键字段 | 索引 / 约束 |
|---|---|---|
| `after_sales` | after_sale_no, order_id, order_no, user_id, store_id, type, status, reason_code, reason_text, evidence_json, amount_cents, route(wechat/balance), applied_by(user/staff), applicant_staff_id, reviewed_by, reviewed_at, reject_reason, due_at（自动同意 / 寄回截止 / 下次提交退款）, return_company, return_tracking_no, return_shipped_at, return_received_at, received_by, refund_id, out_refund_no, refund_attempts, refund_error, refunded_at, closed_at, settle_note, version | uq after_sale_no；ix order_id；ix (user_id, status)；ix (status, due_at)；ix (store_id, created_at) |
| `after_sale_lines` | after_sale_id, order_item_id, kind, name, qty, amount_cents, restock, fulfil_ref | ix after_sale_id；ix order_item_id |
| `after_sale_logs` | after_sale_id, from_status, to_status, actor_type, actor_id, note, created_at | ix (after_sale_id, id)；只追加 |

## 4 业务规则

1. 只有已支付且未全退的订单（paid / fulfilling / fulfilled / completed）可申请；他人订单 4040；未支付 4090；0 元订单无可退款项 4220。
2. 窗口：订单未签收（fulfilled_at 为空）随时可申请；已签收的，`no_reason` 签收次日起 `[after_sales] no_reason_days`（≥ 7）日内、其他原因 `[after_sales] window_days`（默认 15）日内，截止到当地 24:00；超出 4220 `window_closed`。
3. `no_reason` 只适用于下单快照 `returnable=true` 的行（法定除外 / 购买时已确认不适用的商品），否则 4220 `not_returnable`。
4. 类型：`refund_only` 任意行（qty 可为 0 表示按金额部分退，如服务瑕疵补偿）；`return_and_refund` 只适用实物（goods）且 qty > 0（否则 4220 `return_not_applicable`）。
5. 金额：每行上限取 08 `refund_quote`（行实付按数量累计公式；退完剩余数量时 = 剩余实付；会员卡取 29 退卡试算为上限）；未传金额默认上限；超过 4220 `exceeds_refundable`（带 max_cents）。
6. 同一订单行同时只能有一个进行中售后（4090 `after_sale_open`）；额度在 08 用条件 UPDATE 占用，并发下合计永远不超过实付。
7. 余额支付的订单可整行或部分退，退款按消费流水部分冲回（原路，`wallet.reverse` 按 `after_sales:{id}:item:{item_id}` 幂等）。
8. 凭证最多 `[after_sales] max_evidence`（默认 9）个 media id；可接 21-media 校验归属（`set_evidence_checker`）。
9. 审核：同意 / 驳回需 `after_sale:approve`；同意还要求近期二次验证（`require_permission(..., fresh=True)` → 4011）；员工代客发起的申请，审批人不能是发起人（4030 `separation_of_duties`）。驳回原因 2–200 字。
10. 同意后：仅退款 → refunding（立即原路退款）；退货退款 → awaiting_return（截止 `[after_sales] return_ship_days`，默认 7 天）。
11. 买家寄回填写快递公司与单号 → return_shipped；商家确认收货 → refunding（收货当时即发起退款，满足“收到退货 7 日内退款”）；也可在收货环节驳回（货物不符）。
12. 回库：退货退款收到货的实物行回库；仅退款且订单仍是 paid（未发货）的实物行回库；其余不回库。
13. 微信原路退款：事务提交后调用 09 `create_refund(business_ref=after_sales:{id})`；提交前用 `wechat_pay.find_active_refund(db, business_ref)` 查有效退款单（崩溃重提不会重复退款）；4220 `refund_interval` / 5xx → 延后重提（due_at，`after_sales.submit_refunds` 任务）；其他规则拒绝（如超 365 天）→ refund_failed（人工）。
14. 退款结果（事件）：`refund.succeeded` → completed：08 计入已退、回库存、16 按券策略退券（默认整单全额退款才退）、全部退完订单 refunded；`refund.failed`（含 CLOSED）→ refund_failed；`refund.abnormal` → refund_abnormal（09 后台异常退款后再 succeeded → completed）。旧的退款尝试结果（refund_id 不符）忽略。
15. 人工处理（`after_sale:refund` + 二次验证）：重试（refund_failed → refunding，09 生成新退款单号）；线下结清（refund_failed / refund_abnormal → completed，说明必填，须与消费者约定）；关闭（refund_failed → closed，释放额度）。
16. 买家可在 pending_review / awaiting_return 撤销；驳回、撤销、关闭都释放 08 额度。
17. 超时：`[after_sales] auto_approve_days` > 0 时，商家逾期未审自动同意（系统身份，不做职责分离检查）；买家逾期未寄回自动关闭。
18. 所有后台写操作写审计；后台门店范围外按 4040；列表与导出共用同一 ListSpec。

## 5 状态机 / 关键流程

| 从 | 到 | 触发 |
|---|---|---|
| pending_review | refunding | 同意（仅退款）/ 自动同意 |
| pending_review | awaiting_return | 同意（退货退款） |
| pending_review | rejected / cancelled | 驳回 / 买家撤销 |
| awaiting_return | return_shipped / cancelled / closed | 买家寄回 / 撤销 / 超时未寄回 |
| return_shipped | refunding / rejected | 商家确认收货 / 收货驳回 |
| refunding | completed / refund_failed / refund_abnormal | refund.succeeded（或余额冲回）/ refund.failed / refund.abnormal |
| refund_failed | refunding / completed / closed | 重试 / 线下结清（或迟到的成功）/ 关闭 |
| refund_abnormal | completed / refund_failed | 异常退款成功 / 失败 |
| completed / rejected / cancelled / closed | — | 终态（非法流转 4090） |

```
apply ─(08 reserve_refund)▶ pending_review ─approve(fresh, SoD)▶ refunding ─submit_refund▶ 09 create_refund
                                         └▶ awaiting_return ─ship▶ return_shipped ─receive▶ refunding
09 refund.succeeded ─▶ completed（08 settle_refund：已退 / 回库 / 退券 / 全退 refunded）
09 refund.failed / abnormal ─▶ refund_failed / refund_abnormal ─▶ 人工：retry / manual-settle / close
```

## 6 接口

| 方法 | 路径 | 鉴权 | 幂等 | 权限码 | agent | 说明 |
|---|---|---|---|---|---|---|
| GET | `/api/v1/app/after-sales/orders/{order_id}/refundable` | 会员（他人 4040） | — | — | read | 每行可退数量 / 金额、截止时间 |
| POST | `/api/v1/app/after-sales` | 会员 | 必须 | — | write_high | 申请 `{order_id, type, reason_code, reason_text?, evidence?, lines:[{item_id, qty, amount_cents?}]}` |
| GET | `/api/v1/app/after-sales` | 会员 | — | — | read | `tab`=all/processing/done，游标，附计数 |
| GET | `/api/v1/app/after-sales/{id}` | 会员（他人 4040） | — | — | read | 详情（行、时间线） |
| POST | `/api/v1/app/after-sales/{id}/cancel` | 会员 | 必须 | — | write_high | 撤销 |
| POST | `/api/v1/app/after-sales/{id}/return-shipment` | 会员 | 必须 | — | no | 寄回 `{company, tracking_no}` |
| GET | `/api/v1/admin/after-sales` | 员工 | — | after_sale:read | read | 列表（见下） |
| GET | `/api/v1/admin/after-sales/export` | 员工 | — | after_sale:export | no | CSV（审计） |
| GET | `/api/v1/admin/after-sales/{id}` | 员工（范围外 4040） | — | after_sale:read | read | 详情（含审核人、退款单、错误） |
| POST | `/api/v1/admin/after-sales` | 员工 | 必须 | after_sale:create | no | 代客发起 |
| POST | `/api/v1/admin/after-sales/{id}/approve` | 员工 + 二次验证 | 必须 | after_sale:approve | no | 同意（职责分离） |
| POST | `/api/v1/admin/after-sales/{id}/reject` | 员工 | 必须 | after_sale:approve | no | 驳回（原因） |
| POST | `/api/v1/admin/after-sales/{id}/receive` | 员工 | 必须 | after_sale:receive | no | 确认收到退货 → 退款 |
| POST | `/api/v1/admin/after-sales/{id}/retry-refund` | 员工 + 二次验证 | 必须 | after_sale:refund | no | 重试退款 |
| POST | `/api/v1/admin/after-sales/{id}/manual-settle` | 员工 + 二次验证 | 必须 | after_sale:refund | no | 线下结清（说明） |
| POST | `/api/v1/admin/after-sales/{id}/close` | 员工 | 必须 | after_sale:refund | no | 关闭失败单（原因） |

后台列表 ListSpec：筛选 `status`、`type`、`reason_code`、`route`、`store_id`、`order_id`、`user_id`、`amount_cents_min/max`、`created_at_from/to`、`refunded_at_from/to`、`refunded_at_null`；关键词：售后单号、订单号、退货快递单号、退款单号；排序 `amount_cents`、`created_at`、`refunded_at`。

## 7 事件

发出（公共字段：after_sale_id, after_sale_no, order_id, user_id, store_id, type, status, amount_cents）：

| 主题 | 额外字段 |
|---|---|
| `after_sale.applied` | reason_code |
| `after_sale.approved` | auto |
| `after_sale.rejected` | reason |
| `after_sale.cancelled` | — |
| `after_sale.closed` | reason（return_timeout / 人工原因） |
| `after_sale.return_shipped` | tracking_no |
| `after_sale.return_received` | — |
| `after_sale.refund_failed` | refund_id?, error |
| `after_sale.refund_abnormal` | refund_id |
| `after_sale.completed` | route, order_full_refund, lines:[{order_item_id, kind, qty, amount_cents, restock, fulfil_ref}] |

消费（`after_sales/events.py` `SUBSCRIPTIONS`，只处理 business_ref 以 `after_sales:` 开头的，`mark_processed("after_sales.refund_result")` 去重）：

| 主题（09） | 用到的字段 | 处理 |
|---|---|---|
| `refund.succeeded` | business_ref, refund_id, out_refund_no | → completed |
| `refund.failed` | business_ref, refund_id, reason | → refund_failed |
| `refund.abnormal` | business_ref, refund_id | → refund_abnormal |

## 8 并发与一致性

- 额度：08 行级 + 单级条件 UPDATE（50 线程同时整行申请只成 1 单；50 线程逐件申请合计 = 占用 ≤ 实付）。
- 状态：`AFTER_SALE_FLOW.advance` 条件 UPDATE；释放额度与状态变化同事务。
- 渠道调用前提交事务；提交退款前按 business_ref 采纳已有有效退款单（防崩溃重复退款）；渠道结果只经 09 事件进入。
- 本地补偿（已退计数、回库、退券、订单 refunded）只在渠道 SUCCESS 之后执行，且由售后单状态机保证只执行一次。
- 任务：`after_sales.submit_refunds`（60s）、`after_sales.close_unshipped`（3600s）、`after_sales.auto_approve`（3600s），`jobs.JOBS`，逐单提交、可重跑。

## 9 配置

运行参数写在 `backend/config/runtime/*.toml`，字段说明（类型、范围、能否热加载、是否下发客户端）在 `00-framework/code/luzback/runtime_config.py`；模块经 `luzback.module_config.ModuleConfig` 读取，热加载后下一次读取即生效。环境变量只放秘密与部署参数；迁移前的环境变量名仍设置着时，启动会打 `config.unknown_env` 警告并指出新键。测试用 `set_config(...)` 或 `luzback.testing.override_runtime(...)` 注入运行参数（写进一份测试用运行配置，测试结束撤销）。

### 9.1 环境变量（秘密与部署参数）

无：本模块没有秘密与部署参数。

### 9.2 运行配置键

| 键 | 默认 | 范围 | 热加载 | 下发客户端 | 说明（含单位） |
|---|---|---|---|---|---|
| `[after_sales] no_reason_days` | 7 | 7–30 | 是 | 否 | 七日无理由退货期（天，不少于 7：网络购买商品七日无理由退货暂行办法第十条）（原 `AFTER_SALE_NO_REASON_DAYS`） |
| `[after_sales] window_days` | 15 | 1–365 | 是 | 否 | 质量等其他原因的申请窗口（天）（原 `AFTER_SALE_WINDOW_DAYS`） |
| `[after_sales] auto_approve_days` | 0 | 0–30 | 是 | 否 | 商家多少天未处理自动同意（天，0 = 关闭）（原 `AFTER_SALE_AUTO_APPROVE_DAYS`） |
| `[after_sales] return_ship_days` | 7 | 1–30 | 是 | 否 | 同意退货后买家寄回的期限（天）（原 `AFTER_SALE_RETURN_SHIP_DAYS`） |
| `[after_sales] max_evidence` | 9 | 0–20 | 是 | 是 | 申请最多上传几张凭证图（原 `AFTER_SALE_MAX_EVIDENCE`） |
| `[after_sales] refund_retry_seconds` | 60 | 10–3600 | 是 | 否 | 退款失败后多久重试（秒）（原 `AFTER_SALE_REFUND_RETRY_SECONDS`） |
| `[after_sales] job_batch` | 200 | 1–10000 | 是 | 否 | 售后定时任务每批几条（原 `AFTER_SALE_JOB_BATCH`） |
| `[coupon] return_policy` | "full_refund" | never / full_refund / any_refund | 是 | 否 | 退款后是否退券：never 不退、full_refund 整单全额退款才退、any_refund 任何退款都退（原 `COUPON_RETURN_POLICY`） |

退款后是否退券由 16-coupon 的 `[coupon] return_policy` 决定（上表最后一行）。

## 10 错误码

| 场景 | code | HTTP |
|---|---|---|
| 参数错误（类型、原因、凭证数量、qty=0 无金额、驳回原因、单号格式）、缺幂等键 | 4001 | 400 |
| 未登录 | 4010 | 401 |
| 需要二次验证 | 4011 | 401 |
| 无权限、职责分离 | 4030 | 403 |
| 订单 / 行 / 售后不存在、他人、门店范围外 | 4040 | 404 |
| 状态不允许、同一行已有进行中售后、额度并发变化 | 4090 | 409 |
| 窗口已过、不适用无理由、不能退货、超过可退、无可退支付 | 4220 | 422 |
| 幂等键不同请求体 | 4093 | 422 |

## 11 测试清单（`10-after-sales/code/tests/`）

| 文件 | 覆盖 |
|---|---|
| `test_after_sales_service.py` | 可退视图；金额累计公式与超额 4220；同一行进行中 4090；各类拒绝（他人、行不存在、数量、原因、凭证、未支付）；七日无理由窗口（签收次日起、当地 24:00）与质量窗口；退货只适用实物；仅退款全流程（提交、防重复、回调、订单 refunded、回库、退券、事件、重复回调幂等）；发货后部分退不回库；退货退款全流程；驳回 / 撤销 / 超时未寄回释放额度与审计；退款关闭 → 重试新单号成功；异常 → 成功；线下结清；关闭失败单；1 分钟间隔延后重提；崩溃后采纳已有退款；不可重试错误转人工；余额原路冲回与部分退拒绝；会员卡上限；职责分离；自动同意；状态机全边；事件订阅幂等与旧尝试忽略；任务接线 |
| `test_after_sales_http.py` | 4010 / 4001 / 4093 / 信封、可退视图、申请重放、列表页签与计数、详情 4040、寄回校验、撤销；后台 4010 / 4030、二次验证 4011、同意后即时退款、筛选 / 关键词 / 区间 / 未知参数、导出与审计、驳回校验、门店范围；代客发起职责分离、退货退款经 HTTP 收货退款、人工动作状态校验、权限码登记 |
| `test_after_sales_concurrency.py` | 文件 SQLite + 50 线程：整行申请只成一单；逐件申请合计不超过实付 |

## 12 前端对接

| 页面 | 接口 | 行为 |
|---|---|---|
| 申请售后 | `GET …/orders/{id}/refundable` → `POST /app/after-sales` | 按行选数量，金额默认上限；七日无理由选项只对 returnable 行显示并展示截止时间；上传凭证先走 21-media 拿 media id |
| 售后详情 | `GET /app/after-sales/{id}` | awaiting_return 显示寄回地址与填写单号；due_at 显示截止 |
| 我的售后 | `GET /app/after-sales?tab=` | 页签角标用 counts |
| 后台审核 | `/admin/after-sales…` | 同意前触发二次验证；失败 / 异常单显示错误与人工动作；异常退款在 09 后台操作 |

## 13 定制点

配置：§9（运行参数在 `[after_sales]`）。

扩展钩子：`set_refund_gateway(RefundGateway)`（替换退款渠道）、`set_evidence_checker(fn)`（凭证归属校验）、`set_config(...)`；订单侧的 `LineKind.refund_cap` / `on_refunded`（行种类自定义可退上限与退款后处理，见 08 §13）。

会员卡行退款完成后由 29-member-card 消费 `after_sale.completed`（lines 中 `fulfil_ref=member_card:{id}`）终止该卡。

不包含：换货 / 补发、退货运费计算与扣除（七日办法第十八条，需项目约定）、仲裁与平台介入、自动确认收到退货、退款转余额 / 券（运营规范禁止强制，本模块不提供）。
