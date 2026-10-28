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

规则要点：
1. 折扣率：`rate_bp`（万分比应付比例，8850 = 八八五折，优先）或 `rate_percent`（90 = 九折）。
2. 取整：优惠额 = 基数 × (10000 − rate_bp) // 10000，**优惠向下取整到分**（应付因此向上），封顶 `cap_cents`，不超过基数。
3. 适用范围：`sku_ids`（真实 SKU）、`categories`、`line_indexes`（按订单行号，0 起；16-coupon 已算好适用行时用，同一 SKU 多行也能精确限定）。
4. 活动先算，券在活动后的剩余金额上算；同一互斥组只取优惠最大者；未分组的多张券默认一单一券。
5. 分摊：每项优惠只在适用行之间按剩余金额比例分摊（最大余数法，合计严格相等，任何一行不为负）。

## 2 待接入

活动表（时间、范围、预算、互斥组）与后台配置；活动开始后规则只读（MKT-004）。
