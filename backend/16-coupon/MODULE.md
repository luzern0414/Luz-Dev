# 模块：marketing-coupon（营销活动与优惠券）

| 项 | 内容 |
|---|---|
| 状态 | 优惠券核心已实现（模板库存、每人限领、门槛抵扣、核销防重），测试 5 项通过；满减活动、拼团、限时折扣为设计 |
| 代码 | `16-coupon/code/coupon/`、测试 `16-coupon/code/tests/` |
| 依赖 | framework、order（下单时计算抵扣、核销）、membership（会员专享券，设计） |

## 1 职责与边界

| 负责 | 不负责 |
|---|---|
| 优惠券模板（面额、门槛、库存、限领、有效期） | 订单金额合计（order 调用 `discount_for`） |
| 用户领取、槽位限领 | 积分抵扣（membership） |
| 下单时计算抵扣金额 | 支付 |
| 核销防重（一张券只能用一次） | 活动页面的运营配置（管理后台） |
| 满减活动（设计） | — |

## 2 数据模型

| 表 | 字段 | 约束 | 说明 |
|---|---|---|---|
| `coupon_templates` | id, name, face_cents, threshold_cents, remaining_stock, per_user_limit, valid_until | `remaining_stock >= 0` | 模板 |
| `user_coupons` | id, template_id, user_id, slot, status(unused/used/expired), order_id | `(template_id, user_id, slot)` 唯一 | slot 为该用户在该模板下的序号 |
| `promotions`（设计） | id, name, type(full_reduction/discount/gift), rules_json, starts_at, ends_at, status | — | 满减、折扣活动 |

## 3 接口

| 方法 | 路径 | 鉴权 | 幂等 | agent | 说明 |
|---|---|---|---|---|---|
| GET | `/api/v1/app/coupon-templates` | 可选 | — | yes | 可领取的活动券（隐藏已领完、已过期） |
| POST | `/api/v1/app/coupon-templates/{id}/claim` | access | 必须 | yes（需确认） | 领取 |
| GET | `/api/v1/app/coupons?status=unused` | access | — | yes | 我的券；下单页用 `applicable` 参数过滤门槛 |
| GET | `/api/v1/admin/coupon-templates` | admin `coupon:read` | — | yes | 管理列表 |
| POST | `/api/v1/admin/coupon-templates` | admin `coupon:write` | 必须 | no | 创建模板（审计） |
| PATCH | `/api/v1/admin/coupon-templates/{id}/stock` | admin `coupon:write` | 必须 | no | 调整库存（审计，原因必填） |

## 4 关键规则

| 规则 | 说明 |
|---|---|
| 库存 | 条件 UPDATE `remaining_stock > 0`，售罄返回 4220 “coupon sold out” |
| 每人限领 | 槽位唯一约束；应用层先数量检查，唯一约束兜底 |
| 有效期 | 领取时检查 `valid_until`；券的使用有效期单独记录（设计：领取后 N 天） |
| 门槛 | 订单金额 `< threshold_cents` 不可用，抵扣为 0 |
| 抵扣上限 | 抵扣金额不超过订单金额 |
| 叠加规则 | 默认一单一券；满减活动与优惠券是否叠加由活动配置（设计） |
| 核销 | 下单事务内 `redeem`：条件 UPDATE `unused → used`，订单取消时回滚为 unused（设计：订单取消回调） |

## 5 状态机

用户券：`unused → used`（下单核销）；`unused → expired`（定时任务，过期）；`used → unused`（订单取消，设计）。

## 6 并发与一致性

| 场景 | 机制 |
|---|---|
| 库存超发 | 条件 UPDATE；与 inventory 同一思路 |
| 同一用户并发领取超限 | 槽位唯一约束；失败时整体回滚（库存也回滚） |
| 同一张券并发核销 | 条件 UPDATE，只有一单成功 |
| 核销与订单取消竞争 | 订单取消回滚券状态时同样使用条件 UPDATE |

## 7 配置

| 变量 | 说明 |
|---|---|
| `COUPON_DEFAULT_VALID_DAYS` | 领取后有效天数（设计） |

## 8 错误码

| 场景 | code | HTTP |
|---|---|---|
| 模板不存在 | 4040 | 404 |
| 活动已结束 | 4220 | 422 |
| 每人限领已满 | 4220 | 422 |
| 库存售罄 | 4220 | 422 |
| 券不可用（已用、过期） | 4090 | 409 |

## 9 测试用例（已实现）

| 用例 | 断言 |
|---|---|
| 领取扣库存 | remaining 减 1 |
| 超过每人限领 | 4220 |
| 售罄 | 4220 “sold out” |
| 活动已结束 | 4220 |
| 门槛抵扣 | 未达门槛 0；达门槛等于面额；无门槛按订单金额封顶 |
| 重复核销 | 4090，订单号保持第一次的值 |

## 10 可复用代码

`16-coupon/code/coupon/__init__.py`：`CouponTemplate`、`UserCoupon`、`claim`、`discount_for`、`redeem`。

## 11 前端对接

| 页面 | 接口 | 行为 |
|---|---|---|
| 活动专题页 | coupon-templates | 展示可领取券；售罄置灰 |
| 领券按钮 | claim | 成功 toast“已领取”；限领则提示“已达领取上限” |
| 确认订单 → 优惠券 | coupons | 只列出满足门槛的券，不满足的灰显并注明“还差 ¥X” |
