# 示例：小程序模块 ↔ 后端模块 ↔ 接口

这是一个餐饮零售类小程序的参考映射，用来在写方案时挑后端模块与接口；左列是示例页面分组，不对应任何设计文件。项目自己的映射是绑定表 `packages/contracts/design-bindings.json`（`design_intake.py bind` 从已批准的 Luz System 会话生成，见 `design-integration.md`）。后端模块在 `backend/NN-name/`，接口以 `backend/API.md` 附录 A 为准（状态列：已实现 / 设计 / 待建）。前缀 `/api/v1/app` 省略。

| 小程序模块 | 后端模块 | 接口 |
|---|---|---|
| 00-global（登录拦截、状态、降级） | 01-identity、23-app-settings | `POST /auth/login`、`/auth/refresh`、`/auth/logout`、`GET /config`、`GET /content` |
| 01-home | 02-store、08-order、17-campaign、16-coupon、14-membership、03-catalog | `GET /stores/{id}`、`GET /orders?status_group=active&limit=1`、`GET /campaigns`、`GET /coupon-templates`、`GET /member`、`GET /products?sort=sales_desc&limit=4` |
| 02-catalog | 03-catalog、07-inventory | `GET /categories`、`GET /products`、`GET /products/{id}/specs` |
| 03-product-detail | 03-catalog、19-review | `GET /products/{id}`、`GET /products/{id}/review-summary`、`GET /products/{id}/reviews` |
| 04-set-meal | 03-catalog | `GET /set-meals`、`GET /set-meals/{id}` |
| 05-cart | 04-cart | `GET /cart`、`POST /cart/items`、`PATCH /cart/items/{id}`、`DELETE /cart/items/{id}`、`POST /cart/merge`、`DELETE /cart` |
| 06-checkout | 08-order、05-address、06-geo、16-coupon、14-membership、12-queue | `POST /orders/preview`、`GET /addresses`、`GET /delivery-check`、`GET /coupons/applicable?quote_token`、`GET /points`、`GET /time-slots`、`POST /orders` |
| 07-payment | 09-wechat-pay、08-order | `POST /orders/{id}/pay`、`GET /payments/{out_trade_no}`、`GET /orders/{id}` |
| 08-orders | 08-order | `GET /orders?status_group=&cursor=` |
| 09-order-detail | 08-order、11-delivery、12-queue | `GET /orders/{id}`、`POST /orders/{id}/cancel`、`/urge`、`/address`、`/confirm-receipt`、`/add-items`、`/checkout`、`GET /orders/{id}/pickup-code` |
| 10-after-sales | 10-after-sales、21-media | `POST /after-sales`、`GET /after-sales`、`GET /after-sales/{id}`、`POST /after-sales/{id}/close`、`POST /media/presign` |
| 11-address | 05-address、06-geo | `GET/POST /addresses`、`PUT/DELETE /addresses/{id}`、`POST /addresses/{id}/default`、`GET /geo/reverse`、`GET /geo/geocode` |
| 12-queue | 12-queue、20-notification | `POST /queue/tickets`、`GET /queue/tickets/current`、`POST /queue/tickets/{id}/cancel`、`/arrive`、`/rejoin`、`GET /queue/status`、`POST /subscribe-consents` |
| 13-reservation | 13-reservation、09-wechat-pay | `GET /reservations/slots`、`POST /reservations`、`GET /reservations`、`GET /reservations/{id}`、`POST /reservations/{id}/cancel`、`/pay` |
| 14-membership | 14-membership、09-wechat-pay | `GET /member`、`GET /member/levels`、`POST /member/subscribe` |
| 15-points | 14-membership | `GET /points`、`GET /points/ledger`、`POST /points/signin` |
| 16-recharge | 15-recharge、09-wechat-pay | `GET /recharge/plans`、`POST /recharge/orders`、`GET /recharge/account`、`GET /recharge/ledger`、`POST /recharge/orders/{id}/refund-request` |
| 17-coupons | 16-coupon | `GET /coupon-templates`、`POST /coupon-templates/{id}/claim`、`POST /coupons/claim-by-code`、`GET /coupons?status` |
| 18-campaigns | 17-campaign | `GET /campaigns`、`GET /campaigns/{id}`、`POST /campaigns/{id}/expose` |
| 19-grand-opening | 17-campaign、16-coupon、18-lottery | `GET /campaigns/{id}`（type=grand_opening）、领券与抽奖接口 |
| 20-lottery | 18-lottery、05-address | `GET /lottery/activities`、`GET /lottery/activities/{id}`、`POST /lottery/{id}/draw`、`GET /lottery/draws`、`POST /lottery/draws/{id}/address` |
| 21-reviews | 19-review、21-media | `POST /orders/{id}/review`、`GET /reviews/mine`、`POST /media/presign` |
| 22-search | 03-catalog | `GET /search/suggest?q`、`GET /search/hot`、`GET /products?keyword` |
| 23-favorites | 03-catalog | `GET /favorites`、`POST /favorites`、`DELETE /favorites/{product_id}` |
| 24-profile | 01-identity | `GET/PUT /me`、`POST /auth/sms/send`、`/auth/sms/verify`、`/auth/phone/bind`、`POST /me/deactivate` |
| 25-settings | 20-notification、智能体 | `POST /subscribe-consents`、`GET/DELETE /api/v1/agent/memory` |
| 26-store | 02-store、06-geo | `GET /stores`、`GET /stores/{id}`、`GET /stores/nearby` |
| 27-delivery-tracking | 11-delivery | `GET /orders/{id}/delivery`、`GET /orders/{id}/delivery/track` |
| 28-agent-chat | 智能体服务 | `/api/v1/agent/conversations*`（`agent/shared/protocol.md`） |
| 29-share | 03-catalog、17-campaign、02-store、08-order | 落地页按对象调用对应详情接口 |

错误码处理遵循 `backend/SPEC.md` §4 与设计里的界面状态；写接口一律带 `Idempotency-Key`。接口变更先改后端 `MODULE.md` 与 `API.md` 附录 A，再改项目的绑定表。
