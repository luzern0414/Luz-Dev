# {项目名} · 后端需求清单（模板）

> 用途：把一个具体项目的需求映射到 backend 的模块与接口。复制本文件为 `backend/requirements/{项目名}.md` 后填写。未填写的“模块开关”默认关闭。

## 1 基本信息

| 项 | 值 |
|---|---|
| 项目名 | |
| 前端 | 项目 `HANDOFF.json` + `$LUZ_DESIGN_ROOT/references/system/miniprogram.md` |
| 管理后台 | 项目 `HANDOFF.json` + `$LUZ_DESIGN_ROOT/references/system/web-admin.md`（如有） |
| 部署环境 | 开发 / 测试 / 生产 |
| 数据库 | SQLite（仅开发）/ MySQL 8 / PostgreSQL 14 |
| 时区 | Asia/Shanghai |

## 2 模块开关

| 模块 | 开关 | 说明 | 依赖 |
|---|---|---|---|
| framework | 必开 | 基础框架 | — |
| auth-login | 必开 | 登录 | framework |
| address | | 收货地址 | auth-login |
| map-location | | 地址坐标、配送范围 | address |
| inventory-oversell | 必开（有商品时） | 库存 | framework |
| order | 必开 | 订单 | inventory-oversell |
| payment | 必开（有收款时） | 支付 | order |
| membership | | 积分与会员 | order |
| marketing-coupon | | 优惠券与营销 | order |
| review | | 评价 | order |
| delivery-dispatch | | 配送调度（设计，未实现） | order、map-location |
| queue | | 排队叫号（设计，未实现） | notification |
| recharge | | 储值充值（设计，未实现） | payment |
| notification | | 消息通知（设计，未实现） | framework |
| admin-rbac | | 管理后台账号（设计，未实现） | framework |

规则：开关为“必开”的模块不可关闭；依赖模块未开启时，依赖它的模块必须同时关闭。

## 3 接口清单（按模块勾选）

| 模块 | 接口 | 是否需要 | 前端页面 |
|---|---|---|---|
| auth-login | POST `/app/auth/login`、`/refresh` | | 启动 / 登录 |
| address | GET/POST `/app/addresses`、POST `/{id}/default`、DELETE `/{id}` | | 地址列表、地址填写 |
| order | POST `/app/orders`、GET `/{id}`、POST `/{id}/cancel` | | 确认订单、订单详情 |
| payment | POST `/app/orders/{id}/pay`、GET `/app/payments/{no}`、回调 | | 收银台、支付结果 |
| membership | GET `/app/points` | | 我的 → 积分 |
| marketing-coupon | POST `/app/coupon-templates/{id}/claim` | | 活动专题、领券 |
| review | POST `/app/orders/{id}/review` | | 评价页 |

## 4 配置项

| 变量 | 值（开发） | 值（生产） | 来源 |
|---|---|---|---|
| `LUZ_ENV` | dev | prod | |
| `LUZ_DATABASE_URL` | sqlite:///./luz-dev.db | mysql+pymysql://… | 密钥服务 |
| `LUZ_JWT_SECRET` | 开发默认 | 随机 64 字节 | 密钥服务 |
| `WECHAT_APPID` | | 小程序 AppID | 配置 |
| `WECHAT_APPSECRET` | | 密钥服务 | 密钥服务 |
| `WECHAT_PAY_*` | | 见 `09-wechat-pay/MODULE.md` §9 | 密钥服务 |
| `TENCENT_MAP_KEY` | | 地图 Key | 密钥服务 |

## 5 业务参数

| 参数 | 值 | 对应模块 |
|---|---|---|
| 起送价 | | order（设计） |
| 配送半径（km） | | map-location |
| 待支付超时（分钟） | 15 | order |
| 每人地址上限 | 20 | address |
| 积分抵扣比例 | | membership |
| 优惠券限领与库存 | | marketing-coupon |
| 评价时效（天） | 30 | review |

## 6 验收

- [ ] 模块开关与代码挂载一致（只导入开启的模块，`create_app` 只挂载开启模块的 router）。
- [ ] `python -m pytest -q` 通过（框架、所有已开启模块）。
- [ ] 生产配置下 `assert_production_safe()` 不报错。
- [ ] 接口清单中每个“需要”的接口都有 HTTP 测试（至少：鉴权失败、参数错误、成功）。
- [ ] 高并发相关接口（下单、领券、积分扣减）有并发测试。
- [ ] 第三方接口在测试中全部替换为假实现，不访问外网。
