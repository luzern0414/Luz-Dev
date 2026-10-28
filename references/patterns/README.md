# 业务逻辑模式库

Luz Dev 后端开发的业务逻辑参考。每篇写清：方案对比与默认选择、数据模型、状态机、并发与幂等、失败与补偿、测试清单，以及对应的参考代码与规范编号。写技术方案时，先按方案的模块读对应篇目，再用 Luz-Crawl 调研项目特有的部分（`references/research.md`）。

| 篇目 | 解决的问题 | 参考代码 | 测试 |
|---|---|---|---|
| [inventory.md](inventory.md) | 库存模型、扣减时机、防超卖、预占 / 确认 / 释放、流水对账 | `07-inventory` | 13 |
| [order.md](order.md) | 下单流程、价格快照、状态机、超时关单、订单号 | `08-order` | 12 |
| [consistency.md](consistency.md) | 幂等、事务边界、发件箱、Saga / TCC 对比、补偿与对账 | `00-framework` | 框架测试 |
| [pricing.md](pricing.md) | 满减 / 折扣 / 券叠加互斥、分摊、部分退款金额 | `17-campaign/pricing` | 9 |
| [coupon.md](coupon.md) | 券发放、领取、锁定、核销、回退、过期 | `16-coupon` | 10 |
| [accounts.md](accounts.md) | 储值本金 / 赠送、余额流水、积分、退卡 | `15-recharge/wallet` | 5 |
| [reservation.md](reservation.md) | 课程 / 教练 / 场地名额、候补、取消截止、签到、爽约 | `13-reservation/booking` | 7 |
| [after-sales.md](after-sales.md) | 仅退款 / 退货退款、额度控制、防超退 | `10-after-sales` | 5 |
| [seckill.md](seckill.md) | 秒杀、热点库存、分层削峰、Redis 预扣 | — | 压测清单 |
| [jobs.md](jobs.md) | 定时 / 延迟任务、单实例、幂等、分批 | `08-order/jobs.py` | 5 |
| [cache.md](cache.md) | 缓存读写模式、穿透 / 击穿 / 雪崩 | — | 清单 |
| [catalog-cart.md](catalog-cart.md) | SPU / SKU、改价、上下架、购物车合并与失效 | — | 清单 |
| [multi-store.md](multi-store.md) | 多门店 / 多租户数据隔离 | `22-admin-rbac` | 清单 |

支付、退款、对账见 `backend/09-wechat-pay/MODULE.md`；结果页见 `references/result-patterns.md`；安全与合规规则见 `references/standards.json`。

## 共同原则

1. 数据库裁决并发：条件更新与唯一约束，不先读后写，不用应用内锁。
2. 状态字段只通过 `luzback.StateMachine.advance` 改变。
3. 每个写操作都幂等；资金与数量类变化写只追加的流水，并能对账。
4. 事务里不调外部接口；事务外的副作用走发件箱。
5. 每个“已提交但后续失败”的路径都有补偿，每天对账兜底。
6. 金额用分；部分退款按分摊后的行实付。
