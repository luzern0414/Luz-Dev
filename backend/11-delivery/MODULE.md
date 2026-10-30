# 模块：11-delivery（配送调度，规划）`delivery`

| 项 | 内容 |
|---|---|
| 状态 | **仅设计，代码未实现**（实现时按本文件的表结构与接口落地） |
| 依赖 | 00-framework、08-order（已付款订单）、06-geo（距离与范围）、20-notification（骑手与用户通知） |

## 1 职责与边界

| 负责 | 不负责 |
|---|---|
| 把已付款外卖订单分配给骑手（或商家自配送员） | 骑手 App（独立应用，设计时另立） |
| 配送任务状态（待分配、已派单、已取餐、配送中、已送达、异常） | 支付与退款 |
| 骑手实时位置上报与轨迹 | 地图底图 |
| 配送费计算（按距离与时段） | 优惠券计算（order 调用） |

## 2 数据模型（设计）

| 表 | 字段 | 约束 | 说明 |
|---|---|---|---|
| `delivery_tasks` | id, store_id, order_id(唯一), status, rider_id, attempts, exception_reason, assigned_at, picked_at, delivered_at, distance_m, fee_cents, version | `order_id` 唯一 | 一单一任务；`version` 用于管理端并发控制 |
| `riders` | id, name, phone(加密), status(online/offline/busy), lat, lng, location_at | `phone` 唯一 | 骑手档案 |
| `rider_locations` | rider_id, lat, lng, at | 索引 (rider_id, at) | 轨迹，保留 7 天 |
| `delivery_events` | id, task_id, from_status, to_status, at, operator | — | 时间线（用于前端进度） |

## 3 状态机

`waiting_dispatch → assigned → picked_up → delivering → delivered`；`waiting_dispatch｜assigned → cancelled`（订单取消）；`assigned → waiting_dispatch`（骑手超时拒单，自动重派）；异常进入 `exception`。

所有流转用条件 UPDATE（`WHERE status = 旧状态`）；每次流转写 `delivery_events`。

## 4 派单规则（设计）

1. 候选骑手：在线、未满单（同时最多 3 单）、距商家直线距离 ≤ 3 公里。
2. 排序：预计到店时间最短优先；同分取等待时间最长者。
3. 派单后 30 秒未接单，自动重派下一位；3 次未接单转人工调度。
4. 批量合单（设计）：同一区域、取餐时间相差 ≤ 5 分钟的订单可合并派单，合单后每单仍有独立任务记录。

## 5 接口

| 方法 | 路径 | 鉴权 | agent | 说明 |
|---|---|---|---|---|
| GET | `/api/v1/app/orders/{id}/delivery` | access（归属） | read | 配送进度、骑手信息（脱敏电话）、预计送达 |
| GET | `/api/v1/app/orders/{id}/delivery/track` | access（归属） | no | 骑手最新位置（每 10 秒轮询） |
| GET | `/api/v1/admin/delivery/tasks` | admin `delivery:read` | no | 待派单 / 已派单列表 |
| GET | `/api/v1/admin/delivery/riders` | admin `delivery:read` | no | 候选骑手与负载 |
| POST | `/api/v1/admin/delivery/tasks/{id}/assign` | admin `delivery:dispatch` | no | 指派骑手；必须带 `Idempotency-Key`，条件校验任务状态与版本 |
| POST | `/api/v1/admin/delivery/tasks/{id}/reassign` | admin `delivery:dispatch` | no | 重新指派；必须带 `Idempotency-Key` 与审计 |
| POST | `/api/v1/admin/delivery/tasks/{id}/flag` | admin `delivery:dispatch` | no | 标记异常并进入人工处理 |
| POST | `/api/v1/rider/tasks/{id}/accept` | 骑手令牌 | no | 接单（`assigned` 状态确认） |
| POST | `/api/v1/rider/tasks/{id}/pickup` | 骑手令牌 | no | 取餐（需取餐码核对） |
| POST | `/api/v1/rider/tasks/{id}/deliver` | 骑手令牌 | no | 送达（定位需在收货点 300 米内） |
| POST | `/api/v1/rider/location` | 骑手令牌 | no | 位置上报（每 10 秒） |

## 6 并发与一致性

- 多个调度员同时指派：条件 UPDATE `status = waiting_dispatch AND version = 请求版本` 只允许一次成功；成功后 `status = assigned`、`version + 1`。
- 骑手确认接单：仅指派给当前骑手且任务仍为 `assigned` 时成功；超时或拒单按审计事件回到 `waiting_dispatch`。
- 派单与订单取消竞争：取消需先将任务置为 cancelled（条件 UPDATE），派单方发现非 waiting_dispatch 则放弃。
- 位置上报高频：写入缓存（Redis），定时批量落库。

## 7 错误码

| 场景 | code |
|---|---|
| 任务已被接单 | 4090 |
| 不在收货点范围内送达 | 4220 |
| 取餐码错误 | 4001 |

## 8 测试计划（实现时补）

派单顺序、超时重派、并发接单、取消与派单竞争、管理端指派幂等与版本冲突、审计写入、送达范围校验、位置上报限频。

## 9 前端对接

| 页面 | 接口 | 行为 |
|---|---|---|
| 订单详情（外卖进行中） | delivery | 进度条按 `delivery_events` 渲染；履约卡显示骑手与“联系骑手” |
| 配送轨迹页 | track | 地图上显示骑手与收货点，每 10 秒刷新 |
