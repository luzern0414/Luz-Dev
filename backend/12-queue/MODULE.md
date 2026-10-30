# 模块：12-queue（堂食排队叫号，规划）`queueing`

| 项 | 内容 |
|---|---|
| 状态 | **仅设计，代码未实现** |
| 依赖 | 00-framework、20-notification（叫号提醒）、08-order（入座后可点餐，堂食订单） |

## 1 职责与边界

| 负责 | 不负责 |
|---|---|
| 取号（人数、包间 / 大厅） | 点餐与支付（order、payment） |
| 排队人数与等待时间估算 | 桌台管理（商家后台，设计） |
| 叫号、过号、入座确认 | 短信通道 |

## 2 数据模型（设计）

| 表 | 字段 | 约束 | 说明 |
|---|---|---|---|
| `queue_tickets` | id, store_id, zone(hall/room), party_size, number, status, user_id, phone, created_at, called_at, seated_at | `(store_id, zone, number)` 唯一 | 号码按门店与区域独立递增 |
| `queue_counters` | store_id, zone, next_number | 主键 (store_id, zone) | 发号计数器 |
| `tables`（设计，桌台） | id, store_id, zone, capacity, status(free/occupied) | — | 入座时分配桌台 |

## 3 状态机

`waiting → called → seated`；`called → missed`（超时未到，5 分钟）；`waiting → cancelled`（用户取消）；`missed → waiting`（用户重新取号排到队尾）。

## 4 规则

| 规则 | 说明 |
|---|---|
| 取号发号 | `queue_counters` 条件 UPDATE 取号：`UPDATE ... SET next_number = next_number + 1 RETURNING`（MySQL 用 `LAST_INSERT_ID` 方案，见实现说明） |
| 同一手机号 | 同一时间只能有一张 waiting / called 的号 |
| 等待估算 | 前方组数 × 平均用餐时长（按门店近 7 日统计，默认 15 分钟 / 桌），按人数归入大厅或包间分别计算 |
| 叫号 | 按号码顺序叫号；叫号后发送提醒；5 分钟内未到店标记 missed |
| 过号 | missed 的号码不回到原位，用户可重新取号 |
| 营业时间 | 非营业时段不接受取号 |

## 5 接口

| 方法 | 路径 | 鉴权 | agent | 说明 |
|---|---|---|---|---|
| POST | `/api/v1/app/queue/tickets` | access | write_high | 取号：`{party_size, zone}`，幂等 |
| GET | `/api/v1/app/queue/tickets/current` | access | read | 当前排队状态（含前方组数、预计等待） |
| POST | `/api/v1/app/queue/tickets/{id}/cancel` | access（归属） | write_high | 取消排队 |
| POST | `/api/v1/app/queue/tickets/{id}/arrive` | access（归属） | no | 叫号后“我已到店” |
| POST | `/api/v1/staff/queue/call-next` | 员工令牌 | no | 叫下一位 |
| POST | `/api/v1/staff/queue/tickets/{id}/seat` | 员工令牌 | no | 入座并分配桌台 |

## 6 并发与一致性

发号必须原子：并发取号不能出现重复号码，由计数器条件更新与唯一约束共同保证。叫号与过号的状态流转使用条件 UPDATE。

## 7 错误码

| 场景 | code |
|---|---|
| 非营业时间 | 4220 |
| 已有排队中的号 | 4090 |
| 人数超出上限（默认 20） | 4001 |

## 8 测试计划（实现时补）

并发取号号码唯一、等待估算、叫号顺序、超时过号、重复取号拒绝。

## 9 前端对接

| 页面 | 接口 | 行为 |
|---|---|---|
| 排队取号页 | POST tickets | 人数步进器、区域选择、确认取号 |
| 排队中页 | current | 大号码、前方组数、预计等待；被叫号后弹出叫号弹窗 |
| 入座确认页 | arrive | 确认到店后跳转点餐（堂食模式锁定） |
