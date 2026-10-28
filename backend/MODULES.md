# 技术模块目录（MODULES）

每个模块的表、规则、状态机、接口、事件与测试要点。实现者按本表写 `MODULE.md` 与代码；结构与铁律见 `MODULE-TEMPLATE.md`；总则见 `SPEC.md`。规则来源：本仓库 SPEC、小程序 / 后台规范、以及对开源项目（litemall、macrozheng、bee-api、fuint、crmeb、yshop、powerhair）的提炼，已剔除它们的反模式（先查后改、信任客户端价格、浮点算钱、超时关单不查支付、守卫符号写反）。

通用约定（不再逐条重复）：所有表含 `id, created_at, updated_at`；业务表含 `store_id`；金额整数分；状态蛇形字符串；写接口要幂等键；顾客只能访问本人资源（他人 4040）；后台接口按权限码；每条规则有测试。

---

## 02-store（门店）`store`

**表**
- `stores`: name, phone_enc, phone_hash, address, lng, lat, geohash(12), province/city/district, logo_url, images_json, intro, timezone(默认 Asia/Shanghai), status(open|closed|paused), delivery_radius_m, min_order_cents, delivery_fee_cents, free_delivery_threshold_cents, cutoff_minutes(截单提前), prep_minutes(预计出餐), dine_in(bool), delivery(bool), pickup(bool), queue_enabled, reservation_enabled, auto_accept, printer_config_json, deleted_at.
- `store_hours`: store_id, weekday(0–6), open_time("10:30"), close_time("21:30"), seq（同日多段）。
- `store_holidays`: store_id, date, closed(bool), open_time?, close_time?（节假日例外）。

**规则**
1. `is_open(store, at)`：节假日表优先 → 当周营业段 → 跨午夜段（close < open 视为次日）；`paused` 一律关闭。
2. 营业状态对外返回 `open_now`, `next_open_at`, `accepting_orders`（= open_now 且 status=open；预约单例外）。
3. 配送范围判断在 06-geo；门店只存参数。
4. 修改门店写审计；连锁 `copy_config(from, to[])` 复制营业时间与履约参数。
5. 附近门店：按 geohash 前缀粗筛 + Haversine 精排，返回距离。

**接口**
| 方法 | 路径 | 鉴权 |
|---|---|---|
| GET | `/api/v1/app/stores` `?lat&lng` | 可选 |
| GET | `/api/v1/app/stores/{id}` | 可选 |
| GET | `/api/v1/app/stores/nearby?lat&lng&limit` | 可选 |
| GET/PUT | `/api/v1/admin/stores/{id}` · `/hours` · `/fulfillment` | `store:*` |
| POST | `/api/v1/admin/stores/{id}/copy-config` | `store:write` |

**事件** `store.updated`。**测试** 跨午夜营业判断、节假日覆盖、paused、附近排序、权限。

---

## 03-catalog（商品目录）`catalog`

**表**
- `categories`: store_id, parent_id(≤2 级), name(≤30), icon_url, seq, status, deleted_at。
- `products`: store_id, category_id, name(≤30), subtitle(≤40), images_json(≤5), description, tags_json(招牌/新品/限量/当日达), price_cents(起), original_price_cents, member_price_cents?, has_specs, channels_json(dine_in/delivery/pickup), sale_time_json(时段), limit_per_order, monthly_sales(近 30 天，定时物化), status(on_sale|off_sale), sold_out_today(bool, 估清), seq, version(int, 缓存失效), deleted_at。
- `spec_groups`: product_id, name(份量/辣度/花材), required(bool), multi(bool), seq。
- `spec_options`: group_id, name, price_delta_cents, seq, status。
- `skus`（07-inventory 持有库存列）: product_id, spec_values_json({"份量":"大"}), price_cents, stock, version, barcode?。
- `set_meals`: store_id, name, images_json, price_cents, original_total_cents(自动算), channels_json, sale_time_json, limit_per_order, status(draft|on_sale|off_sale), seq。
- `set_meal_groups`: set_meal_id, name("主菜任选 1"), pick_count, allow_spec_change, seq。
- `set_meal_items`: group_id, product_id, sku_id?, extra_cents, default_selected。
- `search_hot_words`: store_id, word, weight；`search_logs`: user_id, keyword, hit_count。

**规则**
1. 价格：无规格商品价 = `price_cents`；有规格 = SKU 价（规格组合唯一 SKU）；`products.price_cents` 为最低 SKU 价（保存时回算）。
2. 套餐价 ≤ `original_total_cents`（各组默认项合计）；组内商品下架时套餐发布校验失败（4220）。
3. 估清：`sold_out_today=true` 当日售罄，次日 00:05 任务清零；售罄与下架商品不可加购（4091 / 4040）。
4. 可售校验 `assert_sellable(product|sku, channel, at)`：状态、渠道、时段、估清、库存>0。
5. 列表：游标分页；排序白名单 `default(seq)`, `sales_desc`, `price_asc`, `price_desc`, `new`；筛选 `category_id, keyword, price_min/max, tags`。
6. 变更商品 / 分类 / 套餐 / 估清 → `catalog_version` +1（存于 store 或 app_settings），缓存 key 带版本。
7. 搜索：关键词前缀匹配名称与标签；热词按 7 天点击加权；用户历史 ≤ 10 条。
8. 后台批量上下架、改分类；分类删除需无商品。

**接口** `/api/v1/app/categories`、`/products`（列表/详情/specs）、`/set-meals`、`/search/suggest`、`/search/hot`；`/api/v1/admin/products*`、`/categories*`、`/set-meals*`（见后台模块清单）。

**事件** `product.updated`, `product.sold_out`, `set_meal.published`。**测试** 规格组合 → SKU、最低价回算、套餐校验、可售校验各分支、游标分页与排序、估清恢复、权限。

---

## 04-cart（购物车）`cart`

**表** `carts`: user_id(唯一), store_id, version；`cart_items`: cart_id, product_id, sku_id, set_meal_id?, selections_json(套餐选择), qty(1–99), selected(bool), snapshot_price_cents, snapshot_name, invalid_reason?。

**规则**
1. 同 (sku 或 套餐+选择) 合并数量；总行数 ≤ 50；qty 1–99。
2. 加购时校验可售（03-catalog），记录快照价；读取购物车时重算当前价与可售性，失效项标 `invalid_reason`（下架 / 售罄 / 时段）并置 `selected=false`。
3. 登录合并：客户端本地购物车 `POST /cart/merge` 按规则 1 合并；冲突取较大数量。
4. 跨门店：切换门店清空（返回 4090 让前端确认）。
5. 结算前 `checkout_preview` 由 08-order 的报价接口承担；购物车只给已选行。

**接口** `GET /api/v1/app/cart`、`POST /cart/items`、`PATCH /cart/items/{id}`、`DELETE /cart/items/{id}`、`POST /cart/merge`、`DELETE /cart`、`POST /cart/select-all?selected=`。agent: `cart_add` low_write。

**测试** 合并、上限、失效项标记、跨店、merge 取大值。

---

## 05-address（收货地址）`address`（已实现，补全）

**表** `addresses`: user_id, contact(1–10), phone_enc, phone_hash, province, city, district, detail(≤100), house_no(≤30), lng, lat, tag(home|company|other), is_default, deleted_at。

**规则** 每人 ≤ `address_max_per_user`（4225）；只有一个默认（条件 UPDATE 清旧默认）；首个地址自动默认；删除默认后最新一条补位；手机号加密脱敏；`PUT` 修改；地址返回 `in_range`（调用 06-geo，门店参数）。

**接口** `GET/POST /api/v1/app/addresses`、`PUT/DELETE /{id}`、`POST /{id}/default`。agent: list read，create/delete high。

**测试** 现有 6 项 + PUT + 范围标记 + 幂等创建。

---

## 06-geo（定位与范围）`geo`

**表** `geo_cache`: key_hash(唯一), kind(reverse|geocode|route), result_json, expires_at。

**规则**
1. 距离 Haversine（米）；配送范围：`d ≤ 0.8R` 在范围；`d ≥ 1.2R` 超出（4222）；之间调用路线 API（`resilient("map_route")`），失败按直线 ×1.3 判断。
2. 逆地理 / 地理编码走 `resilient("map_geo", timeout 3s, attempts 2)`，缓存 7 天，单飞；失败 5020，前端手动选址。
3. 坐标系 GCJ-02；经纬度范围校验。
4. 配送费（供 08-order）：`delivery_fee(store, distance_m, subtotal)`：基础费 + 阶梯（店配置 `fee_tiers_json: [{up_to_m, fee_cents}]`）；满 `free_delivery_threshold` 免；超范围 4222。

**接口** `GET /api/v1/app/geo/reverse?lat&lng`、`/geo/geocode?address`、`/api/v1/app/delivery-check?store_id&address_id|lat&lng`。

**测试** 三段判断、阶梯费、免配送、缓存命中、上游失败降级。

---

## 07-inventory（库存）`inventory`（已实现，扩展）

**表** `skus`（见 03）增加 `lock_stock`(预占), `low_stock_threshold`；`stock_logs`: sku_id, delta, kind(deduct|restore|lock|unlock|adjust|import|sold_out), reason, ref_type, ref_id, operator_id, before, after。

**规则**
1. 下单扣减：`UPDATE skus SET stock = stock - q, version = version + 1 WHERE id = ? AND stock >= q AND status = 'on_sale'`，rowcount 0 → 4091；多 SKU 按 id 升序。
2. 预占模式（可选，`settings.inventory.reserve_on_create`）：下单 `lock_stock += q`（条件 `stock - lock_stock >= q`），支付成功 `stock -= q, lock_stock -= q`，取消 `lock_stock -= q`。
3. 回补只在 `pending_payment → cancelled` 与 售后通过（未出餐）各一次（按 stock_logs 的 ref 去重）。
4. 后台调整只允许 ±delta + 原因（4001 若 delta=0）；批量导入 CSV 逐行条件更新；每次写 `stock_logs`。
5. 低库存：`stock ≤ threshold` 时发 `stock.low`（10 分钟任务去重）。
6. 热点 SKU：Redis Lua 预扣（key `stock:{sku}`），DB 为最终裁决；Redis 不可用退化为 DB。

**接口** `GET /api/v1/admin/stock`、`POST /stock/{sku_id}/adjust`、`POST /stock/import`、`GET /stock/{sku_id}/logs`。

**测试** 现有 50 线程超卖 + 预占模式并发 + 回补去重 + 调整日志。

---

## 21-media（媒体上传）`media`

**表** `media_assets`: owner_type(user|staff), owner_id, purpose(avatar|review|product|banner), object_key, url, content_type, size_bytes, width, height, status(pending|ready|rejected), sha256, deleted_at。

**规则**
1. 预签名直传：`POST /media/presign {purpose, content_type, size}` → 校验类型白名单（jpg/png/webp）、大小 ≤ `upload_max_bytes` → 生成 `object_key = {purpose}/{yyyymm}/{uuid}.{ext}` 与预签名 URL（S3 兼容，开发环境返回本地 mock）。
2. 上传完成 `POST /media/{id}/complete`：服务端 HEAD 校验大小与类型（魔数）；剥离 EXIF（任务）；状态 ready。
3. 评价图 ≤ 9 张；头像 1 张；未 `complete` 的 24 小时清理。
4. URL 走 CDN 域名；返回尺寸变体参数（`?w=352`）。

**接口** `POST /api/v1/app/media/presign`、`POST /media/{id}/complete`、`GET /media/{id}`；后台同路径 `/admin/media/*`。

**测试** 白名单、大小、owner 校验、complete 幂等、清理任务。

---

## 08-order（订单）`order`（已实现 v1，升级 v2）

**表**
- `orders`: store_id, user_id, order_no(唯一), channel(dine_in|delivery|pickup), table_id?, parent_order_id?(堂食加菜), status, sub_status?(overdue), items_cents, packaging_cents, delivery_cents, campaign_discount_cents, coupon_cents, member_discount_cents, points_cents, total_cents, paid_cents, refunded_cents, coupon_user_id?, points_used, address_snapshot_json, expected_at?, remark(≤100), pickup_code?, cancel_reason?, pay_deadline_at, paid_at, accepted_at, ready_at, delivered_at, completed_at, cancelled_at, version。
- `order_items`: order_id, product_id, sku_id?, set_meal_id?, name_snapshot, spec_snapshot, image_snapshot, unit_price_cents, qty, campaign_alloc_cents, coupon_alloc_cents, points_alloc_cents, paid_share_cents(行实付分摊), refunded_qty, refunded_cents。
- `order_status_logs`: order_id, from_status, to_status, actor_type, actor_id, reason, created_at。
- `order_quotes`: quote_token(唯一 uuid), user_id, store_id, request_json, totals_json, expires_at, used_at。
- `order_counters`: store_id, date, kind(order_no|pickup), last_seq；唯一 (store_id, date, kind)。
- `order_notes`: order_id, staff_id, text。

**规则**
1. **报价 `preview`**：校验门店接单中（02）、渠道开启、地址在范围（06）、商品可售（03）；按服务端价计算：活动价替换原价（17）→ 满减（17）→ 优惠券（16，`applicable`）→ 会员折扣（14，与券的叠加由 `settings.promo.stack_member_with_coupon` 决定，不叠加取优惠大者）→ 积分抵扣（14，≤ 折后 30%）→ 配送费（06）+ 包装费；每项优惠按行金额比例分摊到 `*_alloc_cents`，余数给最后一行；返回 `quote_token`（10 分钟）与明细。
2. **下单 `create`**：`quote_token` 必须有效、属本人、未用；重算一次，任一金额变化 → 4223；同一事务：标记 quote 已用（条件 UPDATE）→ 扣库存（07，按 sku_id 升序）→ 锁券（16 `redeem`）→ 扣积分（14）→ 写订单 / 明细 / 状态日志 → `order.created`；幂等键作用域 `user:{id}` + `order.create`；`pay_deadline_at = now + settings.order_pay_timeout_minutes`。
3. 单号：`{yyyymmdd}{store_id:03d}{seq:06d}`，seq 来自 `order_counters` 条件自增（不存在先插入，`IntegrityError` 后重读）；取餐码：出餐时从 `kind=pickup` 计数器取 3 位，当日循环。
4. 堂食必须带 `table_id`（桌台 qr_token 换取，12-queue 校验）；外卖必须 `address_id`；自提必须 `expected_at` 在可约时段。
5. **状态机**（SPEC §8）：每条边一个函数，条件 UPDATE + 状态日志 + 事件；非法边 4090；并发失败 4090。`paid → accepted` 在 `settings.order.auto_accept` 时由支付回调同事务完成。
6. 用户取消：`pending_payment` 直接取消；`paid`（未接单）取消 → 自动创建全额售后（10）并进入 `refunding`；其余状态不可取消（4090），引导售后。取消原因枚举：`changed_mind|wrong_item|too_slow|other`。
7. **超时关单任务**：`pending_payment` 且 `pay_deadline_at < now`：先查支付单（09 `query`）——已支付则补 `mark_paid`，否则取消并关闭支付单；每单一次条件 UPDATE，重复执行无副作用。
8. 取消 / 退款副作用：回补库存（07，按 ref 去重）、退券（16 `revert`，48 小时内）、退积分（14 `clawback`）、释放桌台。
9. 催单：`accepted|preparing` 可催，10 分钟一次（4225），写 note + `order.urged`。
10. 改地址：`paid|accepted` 可改，新地址必须在范围且配送费 ≤ 原费（否则 4220）；写快照与日志。
11. 堂食加菜：`served` 状态下 `POST /{id}/add-items` 建子单（`parent_order_id`），子单走报价与支付或挂账；`checkout` 合并父子单金额，付清后父子一起 `completed`。
12. 出餐：外卖 → 建配送任务（11）；自提 → 取餐码 + 通知；堂食 → `served`。核销：员工输入取餐码或扫码，`awaiting_pickup → picked_up → completed`。
13. 自动完成：`delivered|picked_up` 超过 `order_auto_complete_hours` → completed（任务）；completed 时积分入账（14）并开放评价（19）。
14. 超时标记任务：待接单 > 3 分钟、备餐 > 预计 ×1.5、配送 > 预计 ×1.5 → `sub_status=overdue` + `order.overdue`；恢复时清除。
15. 列表：`status_group=active|completed|after_sales|all`；游标 (created_at, id)；详情含状态时间线、支付记录摘要、配送 / 桌台信息、售后摘要。
16. 后台动作按权限码；`reject` 必填原因并触发规则 6 的自动售后；`note` 只对员工可见。
17. 价格快照：明细存下单时名称 / 规格 / 图 / 单价；后续改价不影响订单。

**接口** 见 `API.md` 附录 A.4 / A.5。agent：`preview` read，`create` high，`cancel` high，`urge` low，`address` high，列表 / 详情 read。

**事件** `order.created / paid / accepted / preparing / ready / delivering / delivered / picked_up / served / completed / cancelled / refunding / refunded / urged / overdue`。

**测试** 报价与下单金额一致；quote 过期 / 他人 / 二次使用；价格变化 4223；每条状态边合法 + 非法；超时关单（已支付 / 未支付两分支）；取消回补库存 / 券 / 积分一次；同一 quote 并发只成功一次；核销；堂食加菜与结账；催单限频；改地址费用规则；分摊余数；单号 / 取餐码并发唯一。

---

## 09-wechat-pay（微信支付）`wechat_pay`（已实现 v1，升级 v3）

**表**
- `payment_records`: business_type(order|recharge|membership|reservation), business_id, user_id, store_id, out_trade_no(唯一 ≤32), amount_cents, currency('CNY'), status(pending|success|failed|closed), channel('wechat_jsapi'), prepay_id, channel_txn_id(唯一), payer_openid, paid_at, expires_at, closed_at。
- `refund_records`: payment_id, business_ref(after_sales_id 等), out_refund_no(唯一), amount_cents, reason(≤80), status(refund_pending|refund_success|refund_failed|closed), channel_refund_id, attempts, last_error, succeeded_at。
- `pay_notify_logs`: kind(pay|refund), out_trade_no, channel_txn_id, serial, verified(bool), result(processed|duplicate|rejected), reason, received_at。

**规则**
1. 创建支付单：金额只来自业务对象（订单 `total_cents - paid_cents`）；同一业务同一时刻只允许一个 `pending`（未过期则复用并返回同一支付参数；过期则 `closed` 后新建）；`expires_at = min(业务截止, now + 1h)`。
2. 预下单 JSAPI：`resilient("wechat_pay_prepay", timeout 8s, attempts 1)`；成功存 `prepay_id`；客户端参数 `paySign = RSA-SHA256(appId\ntimeStamp\nnonceStr\nprepay_id=…\n)`；失败 5020，支付单保持 pending 可重试。
3. **回调 v3 验签与解密**（顺序固定，任一步失败即拒绝并写 `pay_notify_logs`）：① `Wechatpay-Timestamp` 与当前时间偏差 ≤ 5 分钟 ② `Wechatpay-Serial` 等于已缓存的平台证书序列号 ③ RSA-SHA256 验签 `timestamp\nnonce\nbody\n` ④ `resource` 用 APIv3Key AES-256-GCM 解密（nonce、associated_data）⑤ `mchid`、`appid` 等于本商户 ⑥ `trade_state == SUCCESS` ⑦ `currency == CNY` ⑧ `amount.total == payment.amount_cents`（整数分比较）⑨ `out_trade_no` 存在。开发环境无平台证书时允许 HMAC 联调签名（`pay_notify_hmac_secret`），`assert_production_safe` 禁止生产使用。
4. 回调幂等：`status == success` 且 `channel_txn_id` 相同 → `duplicate`，返回成功；条件 UPDATE `pending → success` 只有一个赢家；同事务回调业务 `on_paid(business_type, business_id, payment)`（注册表 `register_on_paid`）；业务方若状态冲突（订单已取消）：支付仍记 success，写 `needs_manual=true` 并自动创建全额退款（10），不回滚支付记录。
5. 响应：成功 `200 {"code":"SUCCESS","message":"成功"}`；拒绝 `4xx {"code":"FAIL","message":…}`；处理时间 ≤ 5 秒，重活走事件。
6. 查询：`GET /payments/{no}` 若 pending 且创建超过 2 分钟 → `resilient("wechat_pay_query")` 查渠道并按回调规则对账（同一处理函数）。
7. 关闭：业务取消且支付单 pending → 渠道关单（`resilient`，失败入任务重试）；本地置 `closed`。
8. 退款：`create_refund(payment, amount, reason, ref)`：`amount ≤ paid − 已退`；`out_refund_no = R + out_trade_no + seq`；调用渠道（attempts 1）；渠道受理 → `refund_pending`；退款回调（验签解密同上，`refund_status == SUCCESS`）→ `refund_success` → `on_refunded` 回调业务；失败 → `refund_failed` + 任务重试（同 out_refund_no，最多 5 次）→ 仍失败告警。
9. 对账任务：pending 超 2 分钟逐单查询；日终拉账单比对（设计接口 `reconcile(date)`）。
10. 安全：商户私钥只在内存；平台证书定期拉取（`resilient("wechat_pay_certs")`，缓存 12 小时，序列号变更时更新）；回调入口不要求用户令牌；记录原始头与序列号用于排查。

**接口** `POST /api/v1/app/orders/{id}/pay`、`POST /api/v1/app/payments`（通用业务）、`GET /api/v1/app/payments/{out_trade_no}`、`POST /api/v1/webhooks/wechat/pay`、`POST /api/v1/webhooks/wechat/refund`；内部 `create_refund`、`query_and_reconcile`、`close_payment`。

**事件** `payment.succeeded / failed / closed / refunded / refund_failed`。

**测试** 现有 4 项；复用 pending 支付单；过期后新建；v3 验签（用 `cryptography` 生成临时 RSA 密钥，缺库时跳过）+ 解密 + 九项校验各失败分支；金额不符不改订单；重复回调；已取消订单回调 → needs_manual + 自动退款；查询对账；退款成功 / 失败 / 重试；关单。

---

## 10-after-sales（售后）`after_sales`

**表**
- `after_sales`: order_id, user_id, store_id, type(refund|partial_refund|reship), reason_code(quality|wrong_item|missing|late|other), reason_text(≤200), evidence_json(media_id ≤ 6), amount_cents, status, applied_at, reviewed_by, reviewed_at, reject_reason, refund_id?, reship_order_id?, closed_at, overdue(bool)。
- `after_sales_items`: after_sales_id, order_item_id, qty, amount_cents。

**规则**
1. 申请窗口：订单 `paid` 之后、`completed` 后 `settings.after_sales.window_hours`（默认 24）内；同一订单同时只允许一个未结束售后（4090）。
2. 金额：全额 = `paid_cents − refunded_cents`；部分 = Σ 行 `paid_share_cents × qty / 原 qty`（整数分，余数向下），且行 `qty ≤ qty − refunded_qty`；总额 ≤ 可退额。
3. 商家拒单 / 用户已付未接单取消 → 系统自动创建 `refund` 全额售后并直接 `approved → refunding`（不需审核）。
4. 审核通过（`aftersales:approve`）：输入订单号后 4 位二次确认由前端完成，后端校验金额 → 创建退款（09）→ 订单 `refunding`（部分退款不改订单主状态，只累加 `refunded_cents`）→ 未出餐则回补库存（07，按行）→ 全额且未备餐则退券（16）。
5. 退款成功回调：售后 `refunded`；订单：全额 → `refunded`；部分 → 保持；积分追回（14 `clawback`）；事件 `after_sales.refunded`。
6. 退款失败：`refund_failed`，任务重试 5 次（同 out_refund_no），仍失败标人工并告警；后台可 `retry-refund` 或线下处理并记录（`manual_settled`）。
7. 拒绝：原因必填（模板：超出时效 / 证据不足 / 已核实无误 / 其他）；用户可再申请一次，第二次拒绝后不可再申请。
8. 补发：生成新订单（`parent_order_id` 指向原单，金额 0，渠道同原单），售后 `reshipped`。
9. 用户在 `pending_review` 可关闭；24 小时未审核 → `overdue=true`。

**状态机** `pending_review → approved → refunding → refunded | refund_failed → (retry) refunding`；`pending_review → rejected`；`approved → reshipped`；`pending_review → closed`。

**接口** app `POST /api/v1/app/after-sales`、`GET /after-sales`、`GET /{id}`、`POST /{id}/close`；admin `GET /after-sales`、`/{id}`、`POST /{id}/approve|reject|reship|retry-refund|manual-settle`。agent：`create_after_sales_draft` high。

**事件** `after_sales.created / approved / rejected / refunded / refund_failed`。

**测试** 窗口、单一未结束、部分金额与余数、通过 → 退款 → 回调全额 / 部分、失败重试与人工、拒绝后再申请一次、补发、自动售后（拒单）。

---

## 11-delivery（配送）`delivery`

**表**
- `delivery_tasks`: order_id(唯一), store_id, status(waiting_dispatch|assigned|picked_up|delivering|delivered|exception|cancelled), provider(self|dada|meituan), rider_id?, external_id?, distance_m, fee_cents, eta_at, assigned_at, picked_up_at, delivered_at, attempts, exception_reason, last_lng, last_lat。
- `riders`: store_id, name, phone_enc, phone_hash, status(online|offline|busy), active_orders, rating_bp。
- `rider_locations`: rider_id, lng, lat, recorded_at（只保留最近 100 条 / Redis）。
- `delivery_events`: task_id, event, payload_json, created_at。

**规则**
1. 订单 `ready` 且渠道为外卖 → 创建任务 `waiting_dispatch`；`provider` 由门店配置。
2. 自配送派单：候选 = 本店 `online` 且 `active_orders < 3` 且距门店 ≤ 3 km 的骑手，取最近；成功 → `assigned`（条件 UPDATE 任务 + 骑手 `active_orders + 1`）；无候选 30 秒后重试，3 次失败 → 人工（`exception_reason = no_rider`，驾驶舱待办）。
3. 第三方：适配器接口 `create(order) / cancel / query`，经 `resilient(provider, timeout 5s, attempts 1)`；回调更新状态（验签由适配器实现）。
4. 骑手动作：`accept`（assigned → 同状态确认）、`pickup`（校验取餐码 → picked_up → 订单 `delivering`）、`deliver`（距离收货坐标 ≤ 300 m，否则 4222；可由店长覆盖）→ `delivered` → 订单 `delivered`；`active_orders − 1`。
5. 位置上报 ≤ 1 次 / 10 秒（4290），写最近位置到任务。
6. 异常：骑手或后台标记（联系不上 / 地址错误 / 其他）→ `exception`，驾驶舱待办；可改派。
7. 取消：订单取消 → 任务 cancelled（第三方同步取消）。

**接口** app `GET /api/v1/app/orders/{id}/delivery`、`/delivery/track`；rider `GET /api/v1/rider/tasks`、`POST /tasks/{id}/accept|pickup|deliver`、`POST /location`；admin `GET /delivery/tasks`、`POST /tasks/{id}/assign|reassign|flag`、`GET /delivery/riders`。

**事件** `delivery.assigned / picked_up / delivered / exception / cancelled`。

**测试** 派单选择最近、无候选重试与人工、取餐码校验、送达距离守卫与覆盖、位置限频、状态边、取消同步。

---

## 12-queue（排队与桌台）`queueing`

**表**
- `queue_zones`: store_id, name(大厅|包间), prefix('A'), min_people, max_people, avg_minutes, enabled, seq。
- `queue_counters`: store_id, zone_id, date, last_number, calling_number；唯一 (store_id, zone_id, date)。
- `queue_tickets`: store_id, zone_id, user_id, phone_hash, number('A012'), people, status(waiting|called|seated|missed|cancelled), priority(bool, 过号重排), notify_sms(bool), created_at, called_at, arrived_at, seated_at, missed_at, cancelled_at, table_id?。
- `tables`: store_id, zone_id, no(≤8), seats, status(free|occupied|cleaning|reserved), current_order_id?, qr_token(唯一 32), deleted_at。

**规则**
1. 取号：门店营业且 `queue_enabled`、区域启用、`people` 在区间；同一用户或同一手机号只能有一个 `waiting|called` 票（4090，唯一部分索引兜底）；号码 = 计数器条件自增（`UPDATE … SET last_number = last_number + 1 WHERE …` 后读回；计数器不存在则插入，冲突后重读）；格式 `prefix + 3 位`。
2. 等待估计：`ahead = 本区 waiting 且 (priority 优先, number < 我的)`；`eta_minutes = ahead × avg_minutes`（avg 由近 20 组实际入座间隔滚动更新）。
3. 叫号 `call-next`：取 `priority=true` 的最早票，否则最小号 `waiting` → 条件 UPDATE → `called`，写 `calling_number`，事件 `queue.called`（20 通知：订阅消息 + 短信（勾选时））；可对同一票 `call` 重呼（不改状态）。
4. 过号：`called` 超过 `queue_miss_seconds`（默认 300）未 `arrive|seat` → 任务置 `missed`；`missed` 票 30 分钟内可 `rejoin` 一次 → 新票 `priority=true`（原票保持 missed）。
5. 到店确认 `arrive`：`called` 写 `arrived_at`，停止过号计时。
6. 入座 `seat`：`called|waiting`（店员可直接入座）→ `seated` + 选桌（桌 `free → occupied`，条件 UPDATE）；桌台 `qr_token` 供堂食扫码：`resolve_table(token)` 返回 store/table，用于 08 下单。
7. 取消：用户只能取消 `waiting`；店员可取消任意未入座票（原因）。
8. 桌台：开台（free → occupied，可不关联票）、并台（记录 merged_into）、换台（订单 `table_id` 更新）、清台（occupied → cleaning → free）；生成 / 重置 `qr_token`；批量生成二维码（返回 token 列表，图片由前端 / 媒体生成）。
9. 区域状态：`GET /queue/status` 返回每区 waiting 组数、预计等待、是否可取号。
10. 每日计数按 `date` 自然重置；历史票保留。

**接口** app `POST /api/v1/app/queue/tickets`、`GET /queue/tickets/current`、`POST /queue/tickets/{id}/cancel|arrive|rejoin`、`GET /queue/status?store_id`；staff `POST /api/v1/staff/queue/call-next`、`POST /staff/queue/tickets/{id}/call|miss|seat|cancel`、`GET /staff/queue/tickets?status`；admin `GET/PUT /queue/config`、`GET/POST/PUT /tables`、`POST /tables/{id}/open|merge|move|clear`、`POST /tables/batch/qrcode`、`POST /tables/{id}/reset-token`。agent：`queue_create` high、`queue_current` read、`queue_cancel` high。

**事件** `queue.created / called / missed / seated / cancelled`。

**测试** 50 线程并发取号号码唯一且递增；一人一票（用户 / 手机号）；估计时间；优先票先叫；过号任务与 rejoin 一次；入座占桌并发；qr_token 解析；清台流程；日期重置。

---

## 13-reservation（预约 / 订座）`reservation`

**表**
- `reservation_policies`: store_id(唯一), advance_days(可约天数 7), slot_minutes(30), deposit_from_people(≥ N 人收定金，0 不收), deposit_cents, free_cancel_hours(2), grace_minutes(15), max_active_per_user(2), no_show_strikes_to_deposit(2), no_show_block_days(30)。
- `reservation_slots`: store_id, zone_id?, date, start_time, end_time, capacity_people, capacity_tables, booked_people, booked_tables, status(open|closed)；唯一 (store_id, zone_id, date, start_time)。
- `reservations`: store_id, user_id, slot_id, zone_id?, table_id?, people, status(held|pending_deposit|confirmed|seated|completed|cancelled|no_show|expired), deposit_cents, payment_id?, hold_expires_at, contact_name, phone_enc, phone_hash, remark(≤100), verify_code(4 位), source(app|admin|phone), cancelled_at, cancel_fee_cents, no_show_at, seated_at。
- `user_reservation_stats`: user_id, store_id, no_show_count, blocked_until。

**规则**
1. 时段生成任务：按门店营业时间与 `slot_minutes` 生成未来 `advance_days` 的时段（幂等：存在则跳过），容量来自区域桌位汇总或策略手填；后台可关闭单个时段。
2. 可用性：`capacity_people − booked_people ≥ people` 且 `capacity_tables − booked_tables ≥ 1`，用条件 UPDATE 增加 `booked_*`（`WHERE booked_people + :p <= capacity_people AND booked_tables < capacity_tables`），rowcount 0 → 4225 "该时段已满"。
3. 保留：创建即 `held`（已占容量），`hold_expires_at = now + reservation_hold_seconds`；需定金 → `pending_deposit` 并创建支付单（09，business_type=reservation）；支付成功回调 → `confirmed`；不需定金 → 直接 `confirmed`。
4. 过期任务：`held|pending_deposit` 超时 → `expired` 并释放容量（条件 UPDATE 回减），支付单关闭。
5. 取消：`confirmed` 可取消；距开始 ≥ `free_cancel_hours` 免费（定金全退）；否则定金不退（`cancel_fee_cents = deposit`）；释放容量；退款走 09。
6. 爽约：开始时间 + `grace_minutes` 仍未 `seated` → `no_show`（任务），定金不退，`no_show_count + 1`；达到阈值后该用户预约必须付定金；`no_show_block_days` 内超过阈值 +1 次则禁止预约（4030）。
7. 入座：店员输入 `verify_code` 或扫码 → `seated` + 分配桌（12 桌台 occupied）；`completed` 由堂食订单结账或店员结束。
8. 限制：同一用户同时 ≤ `max_active_per_user` 个未完成预约；同一时段同一用户只能一个（唯一索引）；人数 1–20。
9. 后台手工预约（电话客人）：`source=phone`，可跳过定金。
10. 预约提醒：开始前 2 小时与 30 分钟发通知（20）。

**接口** app `GET /api/v1/app/reservations/slots?store_id&date&people`、`POST /reservations`、`GET /reservations`、`GET /{id}`、`POST /{id}/cancel`、`POST /{id}/pay`；staff `POST /api/v1/staff/reservations/{id}/seat|no-show`、`GET /staff/reservations?date`；admin `GET/PUT /reservations/policy`、`GET/PUT /reservations/slots`、`POST /reservations`（手工）。

**事件** `reservation.created / confirmed / cancelled / expired / no_show / seated`。

**测试** 并发抢最后容量只成功一个；保留过期释放；定金流程（支付回调确认）；免费 / 收费取消窗口；爽约计数与阈值；同用户并发上限；时段生成幂等；验证码入座。

---

## 14-membership（会员与积分）`membership`（积分已实现，扩展）

**表**
- `member_levels`: store_id?(空为全局), level(1..n), name, threshold_type(free|spend|points|paid), threshold_value, discount_bp(9000 = 九折), points_multiplier_bp(10000), free_delivery(bool), birthday_coupon_template_id?, price_cents?(付费), valid_days?(付费), seq。
- `members`: user_id(唯一), level, growth_cents(累计实付), expires_at?(付费会员), joined_at, status(active|expired)。
- `points_accounts`: user_id(唯一), balance, debt(欠分), version。
- `points_ledger`: user_id, delta, kind(earn|redeem|clawback|expire|adjust|gift|lottery|signin), ref_key(唯一), balance_after, remaining(用于 FIFO 过期), expires_at?, note, operator_id?。

**规则**
1. 积分获取：订单 `completed` 时 `floor(paid_cents / 100) × points_per_yuan × multiplier_bp / 10000`，`ref_key = order:{id}:earn`（唯一，重复无副作用），12 个月过期（`expires_at`）。
2. 积分抵扣（报价 / 下单）：`points_used` 为 `redeem_rate` 的整数倍；抵扣 ≤ 折后金额 × `cap_bp`；扣减 `UPDATE points_accounts SET balance = balance - n WHERE user_id = ? AND balance >= n`，0 行 → 4224；`ref_key = order:{id}:redeem`；取消 / 超时 → 退回 `order:{id}:redeem:revert`。
3. 追回：退款成功时按退款比例追回已发积分 `order:{id}:clawback`；余额不足则扣到 0 并记 `debt`，后续获取先抵扣 debt。
4. 过期任务（每日）：按 ledger `remaining` FIFO 扣减到期分，写 `expire` 条目；提前 7 天发 `points.expiring`。
5. 等级：`completed` 时 `growth_cents += paid`；重新计算等级 = 满足阈值的最高级（只升不降，`settings.member.downgrade=false`）；升级事件 `member.level_up`；付费等级通过支付（09，`business_type=membership`）开通 / 续费，`expires_at` 顺延；到期任务降回免费级并发 `member.expired`，提前 7 天 `member.expiring`。
6. 会员价：报价时对可享行按 `discount_bp` 折扣（商品 `member_price_cents` 优先）；与券的叠加由 16 的 `stack_with_member` 决定。
7. 生日礼：生日当月 1 日任务发券（16 定向发放，`ref = birthday:{user}:{year}`）。
8. 签到积分（可选）：`signin:{user}:{date}` 唯一；连续签到加成由 rules.py 计算。
9. 后台调整：原因必填；|delta| ≤ 1000，超出需 `second_approver_id` 且不等于操作者；写审计。
10. 积分与余额不可提现、不可转让。

**接口** app `GET /api/v1/app/member`、`/member/levels`、`POST /member/subscribe`、`GET /points`、`/points/ledger`、`POST /points/signin`；admin `GET /members`、`/{id}`、`POST /{id}/freeze|unfreeze|tag`、`GET /{id}/points-ledger`、`POST /{id}/points/adjust`、`GET/PUT /member-levels`。agent：`get_member / get_points` read。

**事件** `member.joined / level_up / expiring / expired`、`points.earned / redeemed / clawback / expiring / expired`。

**测试** 现有 4 项；earn/redeem/clawback 的 ref 唯一；条件扣减并发（50 线程）；抵扣上限；debt；等级只升不降；付费会员支付与到期；生日发券一年一次；调整阈值与复核。

---

## 15-recharge（储值）`recharge`

**表**
- `recharge_plans`: store_id?, amount_cents, bonus_cents, valid_days(365), status(on|off), seq, max_per_user_per_day?。
- `stored_accounts`: user_id(唯一), balance_cents, version。
- `stored_ledger`: user_id, delta_cents, kind(recharge|bonus|consume|revert|refund|expire|adjust), ref_key(唯一), balance_after, batch_id?(充值批次), note, operator_id?。
- `recharge_orders`: user_id, plan_id, amount_cents, bonus_cents, payment_id?, status(pending|paid|refunding|refunded|closed), paid_at, consumed_cents, refunded_cents, expires_at。

**规则**
1. 创建充值单：档位在售；`balance + amount + bonus ≤ recharge_max_balance_cents`（4225）；支付单（09，`business_type=recharge`）。
2. 到账（支付回调，同事务）：`UPDATE stored_accounts SET balance = balance + x`（不存在先插入）；两条 ledger：`recharge:{id}:pay`、`recharge:{id}:gift`（唯一键保证幂等）；`recharge.succeeded`。
3. 消费（订单用余额支付）：`UPDATE … SET balance = balance - n WHERE user_id = ? AND balance >= n`，0 行 → 4224；`order:{id}:consume`；余额支付的订单退款 → `order:{id}:revert` 原路退回余额。
4. 充值退款（用户 7 天内申请，财务审批 `recharge:adjust`）：`refund_cash = min(amount, max(0, balance − bonus_remaining))`；扣减 `refund_cash + min(bonus, balance − refund_cash)`；不足部分不退（避免 crmeb 的多退漏洞）；走 09 退款；记录 `refunded_cents`。
5. 到期：赠送部分按批次 `valid_days` 到期，任务扣减剩余赠送（`expire`）；本金不过期。
6. 对账任务（每日）：`Σ ledger.delta == balance` 逐用户校验，不一致写 `recharge_mismatches` 并告警。
7. 后台余额调整：原因必填，单次 ≤ 50000 分，审计。

**接口** app `GET /api/v1/app/recharge/plans`、`POST /recharge/orders`、`GET /recharge/account`、`GET /recharge/ledger`、`POST /recharge/orders/{id}/refund-request`；admin `GET/POST/PUT /recharge/plans`、`GET /recharge/orders`、`POST /recharge/orders/{id}/refund`、`GET /stored-ledger`、`POST /members/{id}/balance/adjust`、`GET /recharge/reconcile?date`。

**事件** `recharge.succeeded / refunded`、`balance.consumed / reverted`。

**测试** 50 线程并发消费不为负；到账幂等；退款公式三种情形（未消费 / 部分消费 / 消费超过本金）；赠送到期；对账发现不一致。

---

## 16-coupon（优惠券）`coupon`（领取已实现，扩展）

**表**
- `coupon_templates`: store_id?, name, type(amount|percent|free_delivery|exchange), value(amount_cents 或 percent_bp), max_discount_cents?, threshold_cents, scope(all|category|product), scope_ids_json, channels_json, valid_type(fixed|relative), valid_from?, valid_to?, valid_days?, per_user_limit, total, remaining, claim_mode(public|targeted|new_user|activity|code), code?, stack_with_member(bool), stack_with_campaign(bool), status(draft|active|paused|ended), deleted_at。
- `user_coupons`: user_id, template_id, slot(1..limit), status(unused|locked|used|expired|revoked), claimed_at, valid_from, valid_to, locked_order_id?, used_order_id?, used_at, source(claim|issue|new_user|lottery|campaign)；唯一 (template_id, user_id, slot)。
- `coupon_issue_logs`: template_id, user_id, ref_key(唯一), kind。

**规则**
1. 领取：模板 `active` 且在领取期；库存 `UPDATE remaining = remaining - 1 WHERE remaining > 0`（0 行 → 4225 "已领完"）；每人上限：`slot = 已有数 + 1`，`slot ≤ per_user_limit`，唯一索引冲突 → 4225；相对有效期在领取时计算。
2. 可用判断 `applicable(coupon, quote)`：unused、在有效期、渠道匹配、范围内行小计 ≥ 门槛（只统计范围内的行）；优惠：amount = `min(value, scope_subtotal)`；percent = `scope_subtotal × bp / 10000` 封顶 `max_discount_cents`；free_delivery = 配送费；exchange = 指定商品一件的价格。
3. 锁定 / 核销 / 退回：下单 `unused → locked`（条件 UPDATE + `locked_order_id`，失败 4090）；支付成功 `locked → used`；取消 / 超时 / 全额退款 → `locked|used → unused`，但 `used` 退回只允许核销后 48 小时内且仍在有效期；退回写 `coupon.reverted`。
4. 定向发放（`coupon:issue`）：用户 id 列表或手机号哈希；`ref_key = issue:{template}:{user}:{batch}` 唯一防重；消耗库存；可指定来源。
5. 新人券：首次登录事件 `user.registered` → 发放 `claim_mode=new_user` 模板（`ref = newuser:{user}:{template}`）。
6. 过期任务（每小时）：`unused` 且 `valid_to < now` → expired（批量条件 UPDATE）。
7. 叠加：报价只选一张券；`stack_with_member=false` 时取券与会员折扣中更大者；`stack_with_campaign=false` 时券的范围排除已享活动价的行。
8. 兑换码（`claim_mode=code`）：码唯一、一次性，按模板规则领取。
9. 统计：领取数、核销数、带动订单金额（按 `used_order_id` 汇总）。

**接口** app `GET /api/v1/app/coupon-templates`、`POST /coupon-templates/{id}/claim`、`POST /coupons/claim-by-code`、`GET /coupons?status`、`GET /coupons/applicable?quote_token`；admin 模板 CRUD、`publish|unpublish|stock`、`issue`、`stats`。agent：`list_coupons` read、`claim_coupon` low。

**事件** `coupon.claimed / used / reverted / expired / issued`。

**测试** 50 线程并发领取库存恰好用完；每人限领；范围门槛只算范围行；百分比封顶；锁定 / 核销 / 48 小时退回边界；定向发放重复无副作用；新人券；过期任务；兑换码一次性。

---

## 17-campaign（活动）`campaign`

**表**
- `campaigns`: store_id, type(full_reduction|time_discount|second_half|new_user_gift|grand_opening|special_page), name, start_at, end_at, channels_json, status(draft|active|paused|ended), priority(int, 大者优先), scope(all|category|product), scope_ids_json, rules_json, banner_media_id?, page_content_json?, per_user_limit?, budget_cents?, spent_cents, deleted_at。
- `campaign_participations`: campaign_id, user_id, order_id, discount_cents, ref_key(唯一 `order:{id}:campaign:{cid}`), status(applied|reverted)。

**规则（按类型，rules_json）**
1. `full_reduction`：`tiers: [{threshold_cents, reduce_cents}]`，取满足的最高档；减额按范围内行金额比例分摊。
2. `time_discount`：`{discount_bp}` 或 `{items: [{product_id, price_cents}]}`；活动价替换原价；每单每人限购 `per_user_limit`。
3. `second_half`：同一商品数量 ≥ 2 时第 2 件按 `discount_bp`（默认 5000）折扣，每组 2 件算一次。
4. `new_user_gift`：注册或首单触发：`{coupon_template_ids: [], points: n}`（16 定向发放 + 14 gift，ref 唯一）。
5. `grand_opening`：组合：`{coupon_pack: [...], time_discount: {...}, lottery_activity_id?, share_poster: bool, banner}`；活动页聚合展示；各子规则按上面执行。
6. `special_page`：仅内容与商品集合，不改价。
7. 引擎 `apply(quote_lines, user, channel, at)`：取范围内生效活动，按 `priority` 降序、`start_at` 升序；同一行只享一个改价类活动（time_discount / second_half），满减在改价后计算；返回每行 `campaign_alloc_cents` 与参与记录草稿；下单时写参与记录（唯一键），取消 / 退款 → `reverted` 并回退 `spent_cents`。
8. 冲突检查（后台发布前）：同范围、同时段、同为改价类 → 返回冲突列表；允许发布但提示优先级。
9. 预算：`spent_cents + discount ≤ budget_cents`，超出活动自动 `paused` 并通知。
10. 统计：曝光（前端上报 `POST /campaigns/{id}/expose`，按用户日去重）、参与数、订单数、优惠总额。

**接口** app `GET /api/v1/app/campaigns`、`/{id}`、`POST /{id}/expose`；admin CRUD、`publish|pause|end`、`check-conflict`、`stats`。

**事件** `campaign.published / ended / budget_exhausted`。

**测试** 每种类型计算（含分摊余数）；优先级与同行只享一个；限购；预算暂停；冲突检测；取消回退参与记录。

---

## 18-lottery（抽奖）`lottery`

**表**
- `lottery_activities`: store_id, name, start_at, end_at, status(draft|active|paused|ended), cost_type(free|points|order), cost_value, daily_limit_per_user(1), total_limit_per_user(0 不限), prize_count(8), rules_text, daily_budget_cents?, spent_today_cents。
- `lottery_prizes`: activity_id, seq(1..prize_count), name, type(none|points|coupon|balance|product|physical), value_json({points}|{template_id}|{cents}|{product_id}|{sku}), weight(int > 0), total(-1 不限), remaining, daily_limit?, image_media_id, value_cents(用于预算)。
- `lottery_draws`: activity_id, user_id, prize_id, prize_type, random_value, total_weight, weights_snapshot_json, status(none|won|fulfilled|failed), idempotency_key(唯一), cost_ref?, address_id?, fulfilled_at, fulfill_ref?, created_at。
- `lottery_user_counters`: activity_id, user_id, date, count, total_count；唯一 (activity_id, user_id, date)。

**规则**
1. 配置校验：奖品数 = `prize_count`；权重为正整数；至少一个 `none`；有库存奖品 `remaining ≤ total`；实物奖品需收货地址。
2. 抽奖只在服务端：`random_value = secrets.randbelow(total_weight)`；累积权重区间 + 二分定位（rules.py 纯函数，可单测分布）。
3. 次数：`daily_limit`：`INSERT counters … ON CONFLICT` 不可用时先插入再 `UPDATE count = count + 1 WHERE count < limit`，0 行 → 4225；`total_limit` 同理。
4. 成本：`points` 用 14 条件扣减（`lottery:{draw}:cost`），失败 4224；`order` 类按未使用的订单机会（`order:{id}:lottery` 唯一）。
5. 库存：中有库存奖品时 `UPDATE remaining = remaining - 1 WHERE remaining > 0`，0 行 → 退化为 `none` 奖（记录 `fallback=true`）；`daily_limit` 按日计数。
6. 幂等：`Idempotency-Key` 必填；重复返回首次结果（`idempotency_key` 唯一）。
7. 发奖（同事务）：points → 14 `gift`（`lottery:{draw}`）；coupon → 16 定向发放；balance → 15 `adjust`；product → 16 `exchange` 券；physical → `won` 待后台 `fulfill`（填物流）；失败 → `failed` 并回退库存与次数（补偿任务）。
8. 预算：`spent_today_cents + value ≤ daily_budget_cents`，超出当日只出 `none`。
9. 风控：每用户每分钟 ≤ 5 次（4290）；活动 `paused` 时拒绝；记录 IP / 设备到审计（网关层）；随机值与权重快照可审计复现。
10. 后台：奖品 CRUD（活动进行中只允许改库存与权重且写审计）、`simulate(n)` 离线验证分布、中奖记录与发货、统计（抽奖次数、中奖率、成本）。

**接口** app `GET /api/v1/app/lottery/activities`、`/{id}`、`POST /lottery/{id}/draw`、`GET /lottery/draws`、`POST /lottery/draws/{id}/address`；admin CRUD、`/prizes`、`/fulfill`、`/stats`、`/simulate`。

**事件** `lottery.drawn / won / fulfilled`。

**测试** 10 万次模拟分布误差 < 1%；库存耗尽退化；50 线程 vs 每日 1 次只成功一次；幂等同结果；积分成本扣减与失败回退；预算封顶；实物奖发货流程。

---

## 19-review（评价）`review`（创建已实现，扩展）

**表** `reviews`(+ store_id, product_ids_json, images_json ≤ 9, tags_json, anonymous, reply_text(≤200), reply_by, reply_at, hidden_reason, status(pending|visible|hidden), rating 1–5)；`review_summaries`: product_id, count, avg_bp, star_1..5（任务物化）。

**规则** 一单一评；本人 `completed` 后 30 天内；内容 ≤ 500；图片 ≤ 9 且 media `ready`；敏感词 → pending；商家回复一次（`review:reply`）；隐藏必填原因（审计）；列表只返回 visible（游标）；≤ 2 星发 `review.negative`（驾驶舱差评待回复）；汇总任务每 10 分钟。

**接口** app `POST /api/v1/app/orders/{id}/review`、`GET /products/{id}/reviews`、`/review-summary`、`GET /reviews/mine`；admin `GET /reviews`、`POST /{id}/moderate`、`/{id}/reply`。

**测试** 现有 6 项 + 窗口、图片上限与状态、回复一次、隐藏原因、汇总、差评事件。

---

## 20-notification（通知）`notification`

**表**
- `notification_templates`: event, channel(subscribe|sms|wecom|print), audience(user|merchant), template_id?(微信), content_tpl, enabled, quiet_hours_exempt(bool)。
- `subscribe_consents`: user_id, template_id, count, updated_at；唯一 (user_id, template_id)。
- `notifications`: event_id, channel, user_id?, store_id?, template, payload_json, status(pending|sent|failed|skipped), attempts, next_attempt_at, last_error, sent_at；唯一 (event_id, channel, user_id)。
- `sms_counters`: phone_hash, date, count。

**规则**
1. 消费发件箱 / 队列主题：`order.paid`（商家：企微 + 打印）、`order.accepted|ready|delivering|delivered|cancelled`（用户订阅消息）、`queue.called`（订阅 + 短信（勾选））、`reservation.*` 提醒、`after_sales.*`、`member.expiring`、`points.expiring`、`lottery.won`。
2. 去重：`(event_id, channel, user_id)` 唯一；重复消费跳过。
3. 订阅消息：需 `subscribe_consents.count > 0`，发送成功 `count − 1`（条件 UPDATE）；无额度 → `skipped` 并尝试其他渠道。
4. 短信：每手机号 10 条 / 天、1 条 / 分钟；验证码类不受静默时段限制。
5. 静默时段 22:00–08:00 只发交易类（叫号、出餐、配送）；营销类延后到 08:00。
6. 发送经 `resilient(channel, timeout 3s)`；失败按 1m / 5m / 30m 重试，5 次后 failed；后台可重发。
7. 模板渲染：占位符 `{order_no_tail}` 等白名单字段；缺字段 → skipped 并记录。
8. 开发环境所有渠道为 mock 发送器（记录到表）。

**接口** app `POST /api/v1/app/subscribe-consents`；admin `GET/PUT /notifications/templates`、`GET /notifications?status`、`POST /{id}/resend`、`GET /notifications/stats`。

**测试** 去重；额度扣减与 skipped；短信限额；静默时段延后；重试与失败；模板渲染缺字段。

---

## 22-admin-rbac（后台账号、角色、权限、审计）`admin_rbac`

**表**
- `admin_users`: username(唯一), password_hash, name, phone_enc, phone_hash, status(active|disabled), mfa_secret_enc?, mfa_enabled, failed_attempts, locked_until, must_change_password, password_changed_at, refresh_version, last_login_at, deleted_at。
- `admin_roles`: name(唯一), description, builtin；`admin_permissions`: code(主键), module, description；`admin_role_permissions`(role_id, code)；`admin_user_roles`(user_id, role_id)；`admin_store_scopes`(user_id, store_id)；`admin_login_logs`(user_id?, username, ip, ua, success, reason, created_at)。

**规则**
1. 口令：`check_password_policy`；argon2 / pbkdf2；首次登录 `must_change_password`；90 天提醒。
2. 登录：失败 `failed_attempts + 1`（条件 UPDATE），≥ 5 → `locked_until = now + 15 分钟`（4290 带剩余秒）；成功清零；`mfa_enabled` → 返回 `mfa_token`（kind=mfa，5 分钟），`/auth/mfa` 校验 TOTP（漂移 ±1）后签发 access（2 小时滑动）+ refresh（7 天，轮换）。
3. 依赖：`current_staff()`（kind=admin 令牌）、`require_permission(code)`（每请求加载权限码，60 秒缓存，失败 4030）、`store_scope(staff)`（空=全部；越界 4031）、`require_fresh()`（退款、改权限等 15 分钟内重登）。
4. 不能给自己升权；不能停用自己；停用 / 改密 / 重置 MFA → `refresh_version + 1`；角色删除前需无用户。
5. 权限码全集按 SPEC §4.3 作为种子数据；默认角色种子（店员 / 店长 / 运营 / 财务 / 系统管理员）。
6. `GET /me` 返回权限码、门店范围、菜单；所有写接口审计；登录日志保留 180 天。

**接口** `POST /api/v1/admin/auth/login|mfa|refresh|logout`、`GET /me`、`POST /me/password`、`POST /me/mfa/enroll|confirm`、`GET/POST/PUT /staff`、`POST /staff/{id}/disable|enable|reset-password|reset-mfa`、`GET/POST/PUT/DELETE /roles`、`GET /permissions`、`GET /audit-logs`、`/{id}`。

**测试** 锁定与解锁；MFA 流程；权限 4030 / 范围 4031；自我升权拒绝；停用撤销会话；审计写入；口令策略；新鲜令牌。

---

## 23-app-settings（系统设置）`app_settings`

**表** `app_settings`: store_id?(空=全局), group, key, value_json, version, effective_at, updated_by；唯一 (store_id, group, key)；`app_settings_history`。

**规则**
1. 分组与键由 `schema.py` 注册（类型、范围、默认）：`order`（pay_timeout_minutes、auto_accept、auto_complete_hours、accept_overdue_minutes）、`after_sales`（window_hours）、`fee`（packaging_cents）、`points`（per_yuan、redeem_rate、cap_bp、expire_months）、`promo`（stack_member_with_coupon）、`inventory`（reserve_on_create、low_stock_threshold）、`notify`（quiet_hours）、`print`、`privacy`（policy_text、deactivate_text）、`brand`（theme_id、logo_media_id、name）、`agent`（enabled、write_enabled、primary_model、budget）。
2. 读取 `get_setting(db, store_id, group, key)`：门店值 → 全局值 → `config.py` 默认；30 秒缓存，写入时 `version + 1` 失效。
3. `effective_at` 未来时间 → 到点生效（任务）；写历史与审计；校验失败 4001 带字段。
4. 公开子集 `GET /api/v1/app/config`：brand、privacy、fee、agent.enabled。

**测试** 解析顺序；校验；定时生效；缓存失效；公开子集不泄露内部键。

---

## 24-report（报表与导出）`report`

**表** `store_daily_stats`: store_id, date, revenue_cents(现金收入 − 退款), balance_consumed_cents(余额消费，单独统计), order_count, paid_count, refund_cents, refund_count, aov_cents, channel_json, hour_json, new_members, recharge_cents, coupon_claimed, coupon_used, lottery_draws；`product_daily_stats`: store_id, date, product_id, qty, revenue_cents；`export_jobs`: type, params_json, requested_by, status(pending|running|done|failed), row_count, media_id?, error, expires_at。

**规则**
1. 营业额口径：`Σ payment_records.success.amount − Σ refund_success`（现金渠道）；余额支付的订单计入 `balance_consumed_cents`，不计入营业额（充值已在收款时计入）——避免重复计算。
2. 物化：今日每 5 分钟增量（按 `paid_at >= last_run`）、日终定稿；幂等（按 date 覆盖）。
3. 驾驶舱 `summary(range)`：当日物化 + 实时待办计数（待接单 / 待出餐 / 待配送 / 售后待审 / 差评待回复 / 低库存 / 排队超时）直接索引查询；对比昨日同时段与上周同日。
4. 图表：趋势（7 / 30 天）、时段（小时）、渠道构成、品类构成、商品 Top10。
5. 导出：≤ 5000 行同步 CSV；> 5000 异步任务（worker 生成 CSV → 21 media → 通知），7 天过期；敏感导出（含手机号）需 `member:sensitive` 并审计，文件名含操作人与时间。

**接口** admin `GET /dashboard/summary?range`、`/todo`、`/charts?type`、`GET /reports/daily?date`、`/range?from&to`、`POST /exports`、`GET /exports`、`/{id}/download`。agent（admin）：`admin_dashboard` read。

**测试** 口径（退款扣减、余额分离）；物化幂等；待办计数；导出任务生命周期与权限。

---

## 25-jobs（任务注册与进程入口）`jobs`

把各模块 `jobs.py` 的函数注册到框架 `JobRegistry`（名称、周期、锁 TTL），提供 `python -m jobs scheduler|worker` 入口：scheduler 跑 `Scheduler.run_forever`，worker 跑 `OutboxWorker`（投递到 `mq` broker）与消费者（20-notification、24-report 的消费者函数）。任务清单按 `SPEC.md` §10。

**测试** 注册表包含全部任务名且无重复；`run_due` 执行并写运行记录；worker 一轮投递。

---

## 26-agent-support（智能体支撑）`agent_support`

**表** `kb_docs`: store_id?, title, doc_type(rule|activity|system), title_path, content_md, valid_from?, valid_to?, version, status(draft|published|disabled), index_status(pending|indexed|failed), updated_by；`kb_chunks`: doc_id, version, seq, text, char_count；`kb_eval_cases`: store_id, question, expected_doc_id, expected_points_json。

**规则**
1. 文档保存 → `version + 1` → 切片（按标题路径前缀，300–500 token ≈ 450–750 字，重叠 50 token）→ `kb_chunks` → 事件 `kb.doc.updated`（智能体服务消费后做向量化并回写 `index_status`）。
2. 有效期过期的文档不进入检索（智能体按 `valid_to` 过滤）。
3. 测试面板 `POST /kb/test {question}` 转发智能体服务检索接口（服务签名）。
4. 智能体配置存 23 `agent` 组；`GET /agent/config` 聚合返回。
5. 服务签名依赖复用 `identity.api.service_call`；`agent: yes` 写接口在路由上声明 `Depends(service_call)`（返回服务名写入审计 actor）。

**接口** admin `GET/POST/PUT /kb/docs`、`/{id}`、`POST /{id}/publish|disable|reindex`、`POST /kb/test`、`GET/PUT /agent/config`、`GET /agent/conversations`（代理到智能体服务）。

**测试** 切片长度与重叠；版本递增与事件；有效期过滤；配置聚合。
