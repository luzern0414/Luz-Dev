# 模块：after-sales（售后）

| 项 | 内容 |
|---|---|
| 状态 | 已实现：申请（按行原子占用可退数量与金额）、审核、驳回、撤销、退货确认、退款事件、退款结果与重试；测试 5 项（含随机申请 / 驳回 60 轮不超退） |
| 代码 | `10-after-sales/code/after_sales/`、测试 `10-after-sales/code/tests/test_after_sales.py` |
| 依赖 | framework（StateMachine、发件箱）；退款执行在 09-wechat-pay |
| 模式 | `references/patterns/after-sales.md`；规则 RFD-001～RFD-006 |

## 1 数据

| 表 | 关键字段 | 说明 |
|---|---|---|
| `refundable_lines` | 订单行 ID、订单、用户、数量、行实付、已占用数量、已占用金额、支付时间 | 支付成功时写入；行实付来自分摊结果 |
| `after_sales` | 订单、用户、类型、状态、金额、原因、驳回原因 | 一次申请一条 |
| `after_sale_lines` | 售后单、订单行、数量、金额 | 申请明细 |

## 2 状态机

`applied → approved | rejected | cancelled`；`approved → return_pending | refunding | cancelled`；`return_pending → returned | cancelled`；`returned → refunding`；`refunding → refunded | refund_failed`；`refund_failed → refunding`。

## 3 接口（设计）

| 方法 | 路径 | 鉴权 |
|---|---|---|
| POST | `/api/v1/app/orders/{id}/after-sales` | 本人；`Idempotency-Key` |
| POST | `/api/v1/app/after-sales/{id}/cancel` | 本人 |
| POST | `/api/v1/admin/after-sales/{id}/approve` · `/reject` · `/returned` · `/retry-refund` | `after_sale:audit`、`order:refund`；写审计 |

## 4 事件

`after_sale.applied`、`after_sale.refund_requested`（`out_refund_no = AS{id}`，重试不变）。退款结果由支付模块回调 `on_refund_result`。
