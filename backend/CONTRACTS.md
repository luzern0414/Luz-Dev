# 模块间契约（CONTRACTS）

每个模块只能通过本文列出的函数被其他模块调用；签名固定，实现者不得改名或改参数顺序。调用方在函数内部延迟导入（`from catalog import get_sellable_sku`）避免循环导入；测试中若被依赖模块尚未实现，用 `monkeypatch` 替换该函数。共享数据结构在 `luzback/contracts.py`（`Quote`、`QuoteLine`、`allocate`、`RangeResult`、`PaymentInfo`、`RefundInfo`）。

依赖分层：L1 只依赖框架与 identity；L2 依赖 L1；L3 依赖 L1 + L2。同层之间不互相调用（事件除外）。

## L1

| 模块 | 导出 | 说明 |
|---|---|---|
| identity | `identity.api.current_user()`, `current_claims()`, `service_call()`; `identity.User`, `identity.profile(db, user_id, reveal_phone=False)`, `identity.freeze(db, user_id, frozen, staff_id, reason)` | 依赖与用户资料 |
| store | `get_store(db, store_id) -> Store`（4040）; `is_open(store, at) -> bool`; `assert_accepting(db, store_id, channel, at) -> Store`（STORE_CLOSED 4221 / 渠道关闭 4220）; `fee_params(store) -> dict(min_order_cents, delivery_fee_cents, free_delivery_threshold_cents, delivery_radius_m, cutoff_minutes, prep_minutes, fee_tiers)`; `nearby(db, lng, lat, limit)` | 门店 |
| geo | `distance_m(lng1, lat1, lng2, lat2) -> int`; `check_range(db, store, lng, lat) -> RangeResult`; `delivery_fee(store, distance_m, subtotal_cents) -> int`; `reverse_geocode(db, lng, lat) -> dict`; `geocode(db, address) -> dict` | 坐标 GCJ-02 |
| address | `get_address(db, user_id, address_id) -> Address`（他人 4040）; `snapshot(address) -> dict`（含脱敏手机） | — |
| catalog | `get_sellable_sku(db, store_id, product_id, sku_id, channel, at) -> QuoteLine`（不可售抛 4040 / 4091 / 4221）; `expand_set_meal(db, store_id, set_meal_id, selections, channel, at) -> list[QuoteLine]`; `bump_version(db, store_id)`; `Product`, `Category`, `SetMeal` | `Sku` 模型在 inventory |
| inventory | `Sku`; `deduct(db, sku_id, qty, ref_type, ref_id)`; `restore(db, sku_id, qty, ref_type, ref_id)`（按 ref 去重）; `lock(db, sku_id, qty, ref)`, `unlock(...)`, `confirm(...)`; `adjust(db, sku_id, delta, reason, operator_id)`; `set_sold_out(db, sku_id, flag)` | 条件 UPDATE |
| cart | `selected_lines(db, user_id, store_id) -> list[dict(product_id, sku_id, set_meal_id, selections, qty)]`; `clear_after_order(db, user_id, order_id)` | — |
| media | `assert_ready(db, media_ids, owner_type, owner_id)`; `url_for(media_id, width=None)` | — |
| app_settings | `get_setting(db, store_id, group, key)`; `get_group(db, store_id, group) -> dict` | 默认回退 `config.py` |
| admin_rbac | `admin_rbac.api.current_staff() -> Staff`; `require_permission(code)`; `store_scope(staff) -> list[int] | None`; `require_fresh()` | 后台依赖 |
| notification | `handle_event(db, topic, payload) -> str`（消费者入口，返回 sent/skipped/failed）; `send_sms(db, phone, text, kind)` | 消费发件箱 |
| agent_support | `chunk_markdown(text, title_path) -> list[str]`; `publish_doc(db, doc_id, staff_id)` | — |

## L2

| 模块 | 导出 | 说明 |
|---|---|---|
| membership | `member_discount_bp(db, user_id) -> int`（无会员 10000）; `apply_member_discount(db, quote) -> None`（填 member_alloc）; `redeem_points(db, user_id, points, ref_key) -> int cents`（4224）; `revert_points(db, user_id, ref_key)`; `earn_points_for_order(db, user_id, order_id, paid_cents)`; `clawback_for_refund(db, user_id, order_id, refund_cents, paid_cents)`; `add_growth(db, user_id, cents)`; `gift_points(db, user_id, points, ref_key, note)`; `on_membership_paid(db, payment: PaymentInfo)` | 注册到 wechat_pay `membership` |
| recharge | `consume(db, user_id, cents, ref_key)`（4224）; `revert(db, user_id, cents, ref_key)`; `adjust(db, user_id, delta_cents, reason, operator_id)`; `on_recharge_paid(db, payment)`; `on_recharge_refunded(db, refund)` | 注册到 wechat_pay `recharge` |
| coupon | `applicable_coupons(db, user_id, quote) -> list[dict]`; `apply_coupon(db, quote, user_coupon_id) -> None`（填 coupon_alloc / coupon_cents；不可用抛 4220）; `lock(db, user_coupon_id, user_id, order_id)`; `mark_used(db, order_id)`; `revert(db, order_id, at)`; `issue(db, template_id, user_id, ref_key, source) -> UserCoupon|None`; `issue_new_user(db, user_id)` | — |
| campaign | `apply(db, quote) -> None`（填 campaign_alloc / campaign_discount_cents / participations / campaign_priced）; `commit(db, order_id, quote)`; `revert(db, order_id)` | 先于券执行 |
| lottery | `draw(db, activity_id, user_id, idempotency_key, address_id=None) -> dict` | 依赖 membership / coupon / recharge |
| wechat_pay | `create_payment(db, business_type, business_id, user_id, store_id, amount_cents, expires_at, description) -> dict(payment_id, out_trade_no, pay_params)`; `register_on_paid(business_type, fn(db, PaymentInfo))`; `register_on_refunded(business_type, fn(db, RefundInfo))`; `create_refund(db, payment_id, amount_cents, reason, business_ref) -> RefundInfo`; `query_and_reconcile(db, out_trade_no) -> dict`; `close_payment(db, payment_id)`; `handle_pay_notify(db, headers, body) -> str`; `handle_refund_notify(db, headers, body) -> str`; `paid_total(db, business_type, business_id) -> int` | 回调同事务调用注册函数 |
| queueing | `resolve_table(db, qr_token) -> Table`（4040）; `occupy_table(db, table_id, order_id)`; `release_table(db, table_id)`; `zone_status(db, store_id)` | 桌台供 order / reservation |
| reservation | `on_reservation_paid(db, payment)`; `on_reservation_refunded(db, refund)`; `mark_seated(db, reservation_id, table_id, staff_id)` | 注册到 wechat_pay `reservation` |
| delivery | `create_task(db, order, address_snapshot, distance_m, fee_cents)`; `cancel_task(db, order_id)`; `task_for_order(db, order_id) -> dict|None` | 由 order 在 ready / cancel 时调用 |

## L3

| 模块 | 导出 | 说明 |
|---|---|---|
| order | `preview(db, user_id, request) -> dict`; `create(db, user_id, request, idempotency_key) -> dict`; `transition(db, order_id, to_status, actor_type, actor_id, reason=None)`; `mark_paid(db, order_id)`（on_paid 注册 `order`）; `get_order(db, user_id, order_id)`; `Order`, `OrderItem` | 调用 L1/L2 全部 |
| after_sales | `auto_full_refund(db, order_id, reason, actor)`; `on_refund_result(db, refund: RefundInfo)`（注册 `after_sales` 到 wechat_pay 退款回调） | — |
| review | `open_for_order(db, order_id)`（completed 时调用） | — |
| report | `materialize_today(db, store_id)`; `summary(db, store_ids, range)` | 只读其他模块表 |
| jobs | `build_registry() -> JobRegistry`; `build_worker(broker) -> OutboxWorker` | 进程入口 |

## 事件主题（发件箱 topic，payload 只放 id 与必要字段）

`user.registered`、`order.*`（created paid accepted preparing ready delivering delivered picked_up served completed cancelled refunding refunded urged overdue）、`payment.*`（succeeded failed closed refunded refund_failed）、`after_sales.*`、`delivery.*`、`queue.*`、`reservation.*`、`member.*`、`points.*`、`recharge.*`、`coupon.*`、`campaign.*`、`lottery.*`、`review.created`、`review.negative`、`stock.low`、`kb.doc.updated`、`store.updated`、`product.updated`。

## 回调注册（wechat_pay）

```python
# 模块导入时注册一次（在各自 __init__.py 末尾）
from wechat_pay import register_on_paid, register_on_refunded
register_on_paid("order", mark_paid_from_payment)
register_on_paid("recharge", on_recharge_paid)
register_on_paid("membership", on_membership_paid)
register_on_paid("reservation", on_reservation_paid)
register_on_refunded("after_sales", on_refund_result)
```

未注册的 `business_type` 回调 → 支付记 success + `needs_manual=true` + 告警日志，不抛错。
