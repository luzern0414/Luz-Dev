# 模块间契约（CONTRACTS）

其他模块只能调用这里列出的公开接口（各包 `__init__.py` 的 `__all__`；`identity.api` / `admin_rbac.api` 等路由依赖另列）。调用方在函数内延迟导入（`import wallet` 写在函数里）避免循环；被依赖模块未启用时用 `try: import x except ImportError` 退回默认实现。跨模块副作用一律走发件箱事件（同事务 `enqueue_event`），事件字段见 `MODULES.md` 事件总表，契约由 `tests/test_event_contracts.py` 自动核对（订阅者只能读生产者真实发出的字段）。

状态列与 `MODULES.md` 一致（定义见那里）；规划模块尚无可调用接口。

## 框架 `luzback`（00-framework）

| 类别 | 导出 |
|---|---|
| 配置 | `Settings`, `get_settings`, `reload_settings`, `register_production_check(name, fn(settings) -> list[str])`（启动时 `Settings.assert_production_safe()` 统一执行） |
| 数据库 | `Base`, `BigId`, `BigIdMixin`, `TimestampMixin`, `SoftDeleteMixin`, `get_db`, `init_engine`, `transaction`, `utcnow` |
| 错误 | `AppError`, `ErrorCode`, `conflict`, `not_found`, `rule` |
| 写入保障 | `run_idempotent`, `idempotency_key`, `optional_idempotency_key`, `enqueue_event`, `mark_processed`, `OutboxWorker`, `dispatch_pending`, `write_audit`, `StateMachine` |
| 列表 / 导出 | `ListSpec`, `Field`, `escape_like`, `export_csv`, `safe_cell`, `cents_to_yuan`, 游标 / 分页助手 |
| 弹性 | `resilient`, `retry_call`, `BreakerRegistry`, `CircuitBreaker`, `Bulkhead`, `TransientError`, `UpstreamError` |
| 安全 | `create_token`, `decode_token`, `hash_password`, `verify_password`, `mask_phone`；`luzback.security.check_password_policy`（NIST，仅长度 + 黑名单） |
| 测试 | `luzback.testing.memory_session`, `make_test_app`, `FakeClock` |

## 账号与权限

| 模块 | 状态 | 契约 |
|---|---|---|
| identity（01） | 标准 | 依赖：`identity.api.current_user`（会员 id，会话已失效 4010）、`current_claims`、`service_call`（服务间签名）、`rate_limit_rules(settings)`；函数：`find_user_by_phone(db, phone) -> User \| None`（哈希查找）、`decode_access`、`current_user_id`、`profile`、`require_fresh`、`revoke_sessions`、`register_deletion_checker(fn)`；注入：`identity.api.set_mini_program / set_sms_sender / set_agreement_gateway`；`identity.testing.member_tokens` |
| admin_rbac（22） | 标准 | 依赖：`require_permission(*codes, fresh=False) -> Staff`、`require_fresh_auth`、`current_staff`；授权助手：`store_scope(staff)`（None = 全部门店）、`assert_in_scope`、`apply_store_scope`、`assert_not_self`、`assert_permission`、`log_sensitive_read`；权限码：`declare_permissions(module, [(code, 描述)])`、`grants(granted, code)`（`*`、`module:*` 前缀匹配）；部署：`ensure_seed(db)`、`python -m admin_rbac.bootstrap --seed / <username>`；测试：`admin_rbac.testing.staff_headers(db, *codes, store_ids=None)`、`staff_actor` |
| legal（27） | 基础 | `current(db, kind)`, `pending(db, user_id)`, `accept(...)`, `publish(...)`, `purchase_terms_checker(db, user_id, accepted, client_version)`（导入时 `install_purchase_terms_checker()` 接入 `order.set_terms_checker`） |

## 交易与资金

| 模块 | 状态 | 契约 |
|---|---|---|
| inventory（07） | 基础 | `Sku`；`deduct(db, sku_id, qty, ref=None) -> bool`、`restore(db, sku_id, qty, ref=None) -> bool`（同一 ref 只生效一次）、`restocked(db, ref, sku_id)`；预占：`reserve`, `confirm`, `release`, `expired_refs`, `ledger_balance` |
| order（08） | 标准 | `preview`, `create_order`, `pay`, `cancel_order`, `confirm_receipt`, `ship`, `mark_fulfilled`；售后用：`refund_quote`, `reserve_refund`, `release_refund`, `settle_refund`, `refund_balance(db, order_id, [(item_id, amount_cents)], ref)`；扩展：`register_line_kind`（goods / service / member_card）、`set_stock_port / set_coupon_port / set_wallet_port / set_member_card_port / set_payment_gateway / set_promotions_provider / set_terms_checker / set_address_provider / set_buyer_contact_provider` |
| wechat_pay（09） | 标准 | `register_payable(business_type, fn) / register_on_paid / register_on_refunded`；`create_payment`, `pay_business`, `close_payment`, `create_refund(db, payment_id, amount_cents, reason, business_ref, actor)`, `find_active_refund(db, business_ref) -> RefundRecord \| None`, `get_refund_status`, `paid_total`, `query_and_reconcile`, `reconcile_bill`, `request_shipping_upload`；客户端：`WechatPayClient` / `HttpWechatPayClient`、`ShippingClient` / `HttpShippingClient(access_token=None → wechat_mp)`；`production_problems(cfg)` |
| after_sales（10） | 标准 | `apply`, `approve`, `reject`, `receive_return`, `submit_refund`, `retry_refund`, `manual_settle`, `refundable`, `business_ref(case_id) = "after_sales:{id}"`；扩展：`set_refund_gateway`, `set_evidence_checker`；结果靠 `refund.*` 事件 |
| wallet（15） | 标准 | `consume(db, user_id, ref, amount_cents, store_id)`, `reverse(db, user_id, ref, amount_cents=None, reversal_ref=None)`（部分冲回，按 reversal_ref 幂等，累计 ≤ 原消费）, `recharge`, `adjust`, `create_recharge_order`, `quote_refund / request_refund / approve_refund / complete_offline_refund`, `member_outstanding_cents`；扩展：`set_payment_port`, `set_member_resolver`, `set_policy_resolver`, `register_outstanding_provider` |
| member_card（29） | 标准 | `create_product`, `create_order`, `issue_card / issue_gift / issue_manual`, `check_usable`, `consume`, `reverse_use`, `adjust`, `freeze / unfreeze`, `transfer`, `report_lost / cancel_lost / replace_card`, `quote_refund / request_refund / approve_refund / reject_refund / complete_offline_refund`, `outstanding_cents` |

## 会员与营销

| 模块 | 状态 | 契约 |
|---|---|---|
| membership（14） | 标准 | `earn(db, user_id, amount, reason, ref_key, expires_at=None)`, `spend`, `revert_spend`, `clawback`, `gift_points(db, user_id, points, ref, reason="", expires_at=None)`, `balance`, `member_profile(db, user_id)`（16-coupon 人群默认用它） |
| coupon（16） | 标准 | `available_coupons`, `best_coupon`, `price_with_coupons`, `lock`, `unlock`, `redeem`, `return_on_refund`, `issue / try_issue`, `claim`, `redeem_code`, `revoke / revoke_by_source`；扩展：`set_member_profile_provider`, `set_audience_checker`, `set_cart_lines_provider`；入站主题 `coupon.issue_requested` |
| pricing（17） | 基础 | `quote(lines, promotions)`, `allocate`, `refund_amount`；`Promotion(rate_bp / line_indexes …)`；优惠向下取整 |
| lottery（18） | 标准 | `draw`, `grant_chances`, `revoke_grant`, `ack_delivery`, `verify_draw`, `prize_won_payload`；奖品交付靠 `lottery.prize_won` → 发奖模块回执 `lottery.delivery_result` |
| review（19） | 基础 | `create_review` |

## 通知与内容

| 模块 | 状态 | 契约 |
|---|---|---|
| notification（20） | 标准 | `notify(db, scene, member_id, data, dedupe_key, *, store_id=None)`, `notify_staff(...)`, `handle_event`；扩展：`set_subscribe_sender`, `set_sms_sender`, `set_recipient_resolver`, `set_event_binding`（事件 → 场景映射表 `EVENT_BINDINGS`） |
| wechat_mp（28） | 标准 | `get_client() / set_client`（`MiniProgramClient`：code2session、手机号、订阅消息、内容安全、小程序码）, `send_subscribe_message(touser, template_id, data, page=None, miniprogram_state=None, lang=None)`, `access_token()`, `invalidate_access_token(bad)`, `register_push_handler(event, fn(db, message))`, `is_configured()`, `production_problems()`；测试：`FakeMiniProgram`, `FakeWechatServer` |

## 运营与基础设施

| 模块 | 状态 | 契约 |
|---|---|---|
| jobs（25） | 标准 | 约定：`<pkg>/jobs.py` 的 `JOBS`（`luzback.scheduler.Job`）、`<pkg>/events.py` 的 `SUBSCRIPTIONS = {topic: handler(db, payload, event_id)}`（幂等、不提交）；`discover_jobs`, `discover_subscriptions`, `EventRouter`, `make_scheduler`, `make_worker`；`python -m jobs scheduler \| worker \| list` |
| address（05）/ geo（06）/ booking（13） | 基础 | address：`create_address`, `set_default`, `delete_address`；geo：`haversine_m`, `check_delivery_range`；booking：`book`, `cancel`, `check_in`, `mark_no_shows` |
| app_settings（23） | 标准 | 运行配置：`install(store)`（进程入口登记 config_loads 加载记录）、`record_load`；读取运行参数用框架的 `luzback.current_config()` / `config_for(store_id=, user_ref=)`。文案与展示：`resolve_display(db, store_id)`, `public_content(db, store_id)`, `content_version(db)`, `update_text`, `reset_text`, `update_display`, `reset_display`, `restore_revision`；扩展：`register_display_key(DisplayKey(...))`, `set_text_checker(fn)`；种子：`load_seed`, `sync_texts`；`python -m app_settings sync \| check-seed` |
| store（02）/ catalog（03）/ cart（04）/ delivery（11）/ queue（12）/ media（21）/ report（24）/ agent_support（26） | 规划 | 设计见 `MODULES.md`；实现时先在这里登记导出再写代码 |

## 支付业务注册（wechat_pay）

业务模块在导入时登记一次（同一 `business_type` 只能有一个）：

```python
from wechat_pay import register_payable, register_on_paid, register_on_refunded
register_payable("order", resolve_payable)       # fn(db, user_id, business_id) -> Payable（金额只来自服务端）
register_on_paid("order", on_payment_paid)       # 回调同事务（保存点）调用；抛错 → needs_manual + 自动全额退款
```

已登记：`order`（08）、`wallet_recharge`（15）、`member_card`（29）。退款结果另发 `refund.succeeded / refund.failed / refund.abnormal`（`business_ref` 标识发起方：`after_sales:{id}`、`wallet_refund:{refund_no}`、`member_card_refund:{refund_no}`）。
