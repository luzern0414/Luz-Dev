# 模块：campaign（营销活动与计价）

| 项 | 内容 |
|---|---|
| 状态 | 计价引擎已实现：满减、折扣、优惠券、互斥组、优先级、适用范围、优惠分摊（最大余数法）、部分退款金额；测试 9 项（含随机性质测试） |
| 代码 | `17-campaign/code/pricing/`（纯函数）、测试 `17-campaign/code/tests/test_pricing.py` |
| 模式 | `references/patterns/pricing.md`、`references/patterns/coupon.md`；规则 MONEY-001～MONEY-003、MKT-001～MKT-005 |

## 1 用法

```python
from pricing import Line, Promotion, FULL_REDUCTION, COUPON_AMOUNT, quote
q = quote([Line(1, 2, 3000)], [Promotion("满50减10", FULL_REDUCTION, threshold_cents=5000, amount_cents=1000)])
q.payable_cents, q.line_discount_cents, q.rejected
```

订单提交时把 `line_discount_cents` 与行实付写入订单明细；售后按行实付计算退款。

## 2 待接入

活动表（时间、范围、预算、互斥组）与后台配置；活动开始后规则只读（MKT-004）。
