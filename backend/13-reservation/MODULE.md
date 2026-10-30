# 模块：13-reservation（预约名额）`booking`

| 项 | 内容 |
|---|---|
| 状态 | 已实现：时段名额原子占位、一人一约、时间冲突、候补与自动递补、取消截止、签到窗口、爽约任务；测试在 `code/tests/`（含 40 并发抢 8 个名额） |
| 代码 | `13-reservation/code/booking/`、测试 `13-reservation/code/tests/test_booking.py` |
| 依赖 | 00-framework（StateMachine、发件箱） |
| 模式 | `references/patterns/reservation.md`；规则 RSV-001、RSV-002 |

## 1 数据

| 表 | 关键字段 |
|---|---|
| `booking_slots` | 资源（课程 / 教练 / 场地）、开始、结束、容量、已约、状态 |
| `bookings` | 时段、用户、状态、排队时间；唯一（时段, 用户） |

## 2 状态机

`booked → checked_in | cancelled | no_show`；`waitlisted → booked | cancelled`；`cancelled → booked | waitlisted`。

## 3 接口（设计）

| 方法 | 路径 | 鉴权 |
|---|---|---|
| GET | `/api/v1/app/slots?resource&date` | 可选 |
| POST | `/api/v1/app/slots/{id}/bookings` | 本人；`Idempotency-Key` |
| POST | `/api/v1/app/bookings/{id}/cancel` | 本人 |
| POST | `/api/v1/admin/bookings/{id}/check-in` · `/cancel` | `booking:manage` |

## 4 待接入

次卡 / 课时：预约冻结、签到扣减、取消解冻（按 `references/patterns/accounts.md` 的冻结做法）；排课生成任务。
