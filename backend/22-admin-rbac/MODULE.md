# 模块：admin-rbac（管理后台账号、角色与审计）

| 项 | 内容 |
|---|---|
| 状态 | **仅设计，代码未实现**（权限码声明方式已在 SPEC §5.2 定义） |
| 依赖 | framework、所有业务模块（权限码声明） |

## 1 职责与边界

| 负责 | 不负责 |
|---|---|
| 后台账号、登录、会话 | 小程序用户登录（auth-login） |
| 角色与权限码 | 业务数据权限的具体规则（各模块声明过滤条件） |
| 门店范围（数据权限） | 财务结算 |
| 操作审计（只追加） | 日志系统（SPEC §10） |

## 2 数据模型（设计）

| 表 | 字段 | 约束 | 说明 |
|---|---|---|---|
| `admin_users` | id, username(唯一), password_hash, status, mfa_secret(加密), last_login_at | — | 密码用 argon2 或 bcrypt |
| `admin_roles` | id, name(唯一), description | — | 角色 |
| `admin_permissions` | code(主键), description | — | 权限码，如 `order:read` |
| `admin_role_permissions` | role_id, permission_code | 复合主键 | 角色拥有的权限 |
| `admin_user_roles` | user_id, role_id | 复合主键 | 用户角色 |
| `admin_store_scopes` | user_id, store_id | 复合主键 | 门店范围；空表示全部门店 |
| `audit_logs` | id, actor_id, action, resource, resource_id, before_json, after_json, ip, trace_id, created_at | 只允许 INSERT | 审计 |

## 3 权限码清单（全集，与 `$LUZ_DESIGN_ROOT/references/system/web-admin.md` 及 handoff 权限语义一致；新增先登记此表）

| 权限码 | 含义 | 所属模块 |
|---|---|---|
| `dashboard:read` | 驾驶舱 | report |
| `order:read` / `order:accept` / `order:prepare` / `order:ready` / `order:ship` / `order:complete` / `order:cancel` / `order:refund` / `order:note` / `order:print` | 订单查看与各状态动作、备注、打印 | order / payment |
| `aftersales:read` / `aftersales:approve` | 售后查看与审批 | after-sales |
| `delivery:read` / `delivery:dispatch` | 配送查看与指派 | delivery-dispatch |
| `table:read` / `table:write` / `queue:read` / `queue:call` / `queue:seat` | 桌台与排队 | queue |
| `product:read` / `product:write` / `product:publish` / `category:write` | 商品与分类 | catalog |
| `setmeal:read` / `setmeal:write` | 套餐 | catalog |
| `stock:read` / `stock:adjust` | 库存查看与增减 | inventory-oversell |
| `member:read` / `member:write` / `member:sensitive` | 会员（含敏感字段查看，写审计） | membership |
| `points:read` / `points:adjust` | 积分流水与调整 | membership |
| `recharge:read` / `recharge:config` / `recharge:adjust` | 充值记录、档位配置、余额调整 | recharge |
| `coupon:read` / `coupon:write` / `coupon:issue` | 优惠券配置与定向发放 | marketing-coupon |
| `campaign:read` / `campaign:write` | 活动 | marketing-coupon |
| `review:read` / `review:moderate` / `review:reply` | 评价查看、审核、回复 | review |
| `store:read` / `store:write` | 门店 | store |
| `notify:read` / `notify:template` / `notify:resend` | 通知查看、模板、重发 | notification |
| `kb:read` / `kb:write` | 知识库 | agent-support |
| `agent:read` / `agent:config` | 智能体对话审计与配置 | agent-support |
| `report:read` / `report:export` | 报表与导出 | report |
| `staff:manage` / `role:manage` | 员工与角色 | admin-rbac |
| `audit:read` | 审计查询 | admin-rbac |
| `settings:read` / `settings:write` | 系统设置 | settings |

默认角色（可在项目中调整）：店员（order:read/accept/prepare/ready/complete/note/print、queue:*、table:*、product:read、review:read）；店长（店员 + order:cancel/refund、delivery:*、product:*、setmeal:*、stock:*、member:read/write、points:adjust、coupon:issue、store:*、report:read、dashboard:read）；运营（product:*、setmeal:*、coupon:*、campaign:*、review:*、kb:*、notify:template、dashboard:read、report:read）；财务（report:*、recharge:*、aftersales:*、audit:read、member:read）；系统管理员（staff:manage、role:manage、settings:*、audit:read、agent:config、notify:*）。

## 4 接口

| 方法 | 路径 | 权限 | 说明 |
|---|---|---|---|
| POST | `/api/v1/admin/auth/login` | 无 | 账号密码 + 验证码；失败 5 次锁定 15 分钟 |
| POST | `/api/v1/admin/auth/mfa` | 无 | 二次验证（TOTP） |
| POST | `/api/v1/admin/auth/logout` | 登录 | 注销会话 |
| GET | `/api/v1/admin/me` | 登录 | 当前账号、角色、权限码、门店范围（前端据此显示菜单） |
| GET/POST/PUT | `/api/v1/admin/staff` | `staff:manage` | 账号管理 |
| GET/POST/PUT | `/api/v1/admin/roles` | `role:manage` | 角色管理 |
| GET | `/api/v1/admin/audit-logs` | `audit:read` | 审计查询（只读） |

## 5 关键规则

| 规则 | 说明 |
|---|---|
| 权限校验 | 接口装饰器声明权限码；缺失返回 4030 |
| 菜单隐藏 | 前端按 `/admin/me` 的权限码隐藏菜单，但后端必须校验，不能依赖前端隐藏 |
| 审计 | 所有写接口在同一事务内写审计行；审计写入失败则业务写入回滚 |
| 账号停用 | 停用后现有会话立即失效（会话表或 token 版本号） |
| 最小权限 | 默认角色只含只读权限；写权限需单独授予 |

## 6 并发与一致性

审计表只追加；权限变更后，已签发的会话在下一次请求时重新加载权限（不依赖旧令牌中的权限快照）。

## 7 配置

| 变量 | 说明 |
|---|---|
| `ADMIN_SESSION_TTL_MINUTES` | 默认 120 |
| `ADMIN_LOGIN_MAX_FAILURES` | 默认 5 |

## 8 错误码

| 场景 | code |
|---|---|
| 账号或密码错误 | 4010（不区分原因） |
| 账号锁定 | 4290 |
| 权限不足 | 4030 |

## 9 测试计划（实现时补）

权限拒绝、门店范围过滤、审计回滚、账号停用后会话失效、登录锁定。

## 10 前端对接

管理后台启动后调用 `/admin/me` 获取菜单与权限；无权限页面不显示入口，直接访问 URL 时显示 403 页面。
