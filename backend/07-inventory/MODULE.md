# 模块：inventory-oversell（库存与超卖防护）

| 项 | 内容 |
|---|---|
| 状态 | 已实现（条件扣减、回补），测试 6 项通过，含 50 线程并发用例 |
| 代码 | `07-inventory/code/inventory/`、测试 `07-inventory/code/tests/` |
| 依赖 | framework |

## 0 预占模型（已实现）

`inventory/reservation.py`：`reserve` / `confirm` / `release` / `expired_refs` / `ledger_balance`，预占状态 `reserved → confirmed | released`，流水表 `stock_ledger`，测试 `test_reservation.py` 8 项（含 50 并发、关单与支付竞态）。做法与方案对比见 `references/patterns/inventory.md`；规则 ORDER-003、ORDER-008。

## 1 职责与边界

| 负责 | 不负责 |
|---|---|
| SKU 库存数量与上下架状态 | 商品基础信息（商品模块，设计） |
| 原子扣减、回补 | 下单流程（order） |
| 超卖防护的数据库规则 | 预售与预占（设计，见 §8） |
| 库存变动版本号（用于缓存失效） | 缓存实现（Redis，设计） |

## 2 数据模型

| 表 | 字段 | 约束 | 说明 |
|---|---|---|---|
| `skus` | id, name, price_cents, stock, status(on_sale/off_sale), version | `stock >= 0`（应用层与条件 UPDATE 共同保证） | version 每次变动 +1 |

## 3 接口

库存无独立对外接口。管理后台的库存调整接口在 admin 模块中（`PATCH /api/v1/admin/skus/{id}/stock`，权限 `sku:stock`，必须写审计，调整量与原因必填）。

## 4 核心规则

### 4.1 扣减

```sql
UPDATE skus
SET stock = stock - :qty, version = version + 1
WHERE id = :sku_id AND stock >= :qty AND status = 'on_sale';
-- rowcount = 1 → 成功；0 → 库存不足或已下架（统一返回 4091）
```

- 禁止先 `SELECT stock` 再判断再 `UPDATE`：两步之间会被其他请求插入，造成超卖。
- 扣减与订单写入在同一事务。订单任何后续步骤失败，扣减一并回滚。

### 4.2 回补

```sql
UPDATE skus SET stock = stock + :qty, version = version + 1 WHERE id = :sku_id;
```

只允许在订单从“未支付”流转到“已取消”（或已支付退款）的那一次执行，由 order 状态机保证不重复。

### 4.3 为什么不用应用内锁

应用内互斥锁只在单进程有效，多实例部署即失效；数据库条件 UPDATE 由存储引擎保证，任何实例、任何并发量都成立。

## 5 高并发场景设计

| 场景 | 数据库层（必须） | 入口层（建议） | 说明 |
|---|---|---|---|
| 普通商品 | 条件 UPDATE | 按用户限流 | 足够 |
| 限量抢购（秒杀） | 条件 UPDATE + 唯一约束“一人一单” | 排队令牌、按 IP 限流、验证码 | 令牌数 = 库存数，超出直接返回售罄 |
| 超热点 SKU（同一行竞争激烈） | 条件 UPDATE；数据库行锁排队 | Redis 预扣（Lua 原子）作为前置过滤 | Redis 扣减成功才进入数据库；数据库失败需回补 Redis |
| 大促跨库 | 按商品分片，同一 SKU 永远落在同一分片 | 库存分桶（拆成 N 个子库存行，随机取桶） | 适合库存极大、单行成为瓶颈的场景 |

### 5.1 Redis 预扣（秒杀前置，设计）

```lua
-- KEYS[1] = 库存键；ARGV[1] = 数量
local stock = tonumber(redis.call('GET', KEYS[1]) or '-1')
if stock < tonumber(ARGV[1]) then return -1 end
return redis.call('DECRBY', KEYS[1], ARGV[1])
```

规则：Redis 返回成功后才进入数据库下单；数据库下单失败必须执行 Redis `INCRBY` 回补；Redis 与数据库库存定期对账。

## 6 配置

| 变量 | 说明 |
|---|---|
| `INVENTORY_HOT_SKU_THRESHOLD` | 每秒请求超过此值的 SKU 启用 Redis 预扣（设计） |

## 7 错误码

| 场景 | code | HTTP |
|---|---|---|
| 库存不足 | 4091 | 409 |
| 商品下架 | 4091 | 409（message 区分） |
| 数量非正 | 4001 | 400 |

## 8 设计：预占（先占后付）

适用于下单后需要一段时间才支付的场景。`inventory_reservations`：order_id、sku_id、qty、status(reserved/committed/released)、expire_at。预占与扣减的区别：预占可过期自动释放，扣减只能回补。过期释放由定时任务执行：`status = reserved AND expire_at < now` → 回补并标记 released。

## 9 测试用例（已实现）

| 用例 | 断言 |
|---|---|
| 库存充足扣减 | 库存减少 |
| 库存不足 | 4091，库存不变 |
| 下架商品 | 4091 |
| 回补 | 库存恢复 |
| 50 线程抢 10 件 | 成功 10 次，失败 40 次，最终库存 0 |

## 10 可复用代码

`07-inventory/code/inventory/__init__.py`：`Sku`、`deduct`、`restore`。并发测试可直接复用到其他“数量类”资源（优惠券名额、活动名额）。

## 11 前端对接

| 页面 | 行为 |
|---|---|
| 商品详情 | 展示“剩余 N 件”（仅低于阈值时展示，避免泄露精确库存）；库存为 0 显示“已售罄”，按钮置灰 |
| 购物车 / 确认订单 | 提交时若返回 4091，在对应商品行显示“库存不足”，并建议减少数量 |
