# 后端（backend）

本目录是 Luz Dev 的后端基线：包含架构规范、编号业务模块、可复用代码、契约、迁移与部署配置。先阅读 `SPEC.md` 与 `MODULES.md`，再只加载当前任务需要的模块文件。

## 入口与目录

| 路径 | 作用 |
|---|---|
| `SKILL.md` / `WORKFLOW.md` | 开发技能的入口与交付门禁（位于上一级） |
| `SPEC.md` | 分层、接口、错误码、幂等、并发、安全、测试与交付总则 |
| `API.md` | 接口清单、鉴权、幂等、幂等键与实现状态 |
| `CONTRACTS.md` | 每个模块的公开导出（跨模块只调这些）与支付业务注册 |
| `ROUTING.md` | 网关路由、版本与灰度 |
| `SECURITY.md` | 认证、授权、输入输出、支付回调与 API 安全 |
| `MICROSERVICES.md` | 服务边界、数据归属、事件、Saga 与降级 |
| `MODULES.md` / `MODULE-TEMPLATE.md` | 编号模块目录与模块文档骨架 |
| `app.py` | 按功能开关自动发现并装配编号模块路由 |
| `NN-<name>/MODULE.md` | 当前模块的职责、数据、状态机、接口、并发与测试契约 |
| `NN-<name>/code/` | 可复用实现；只修改当前任务涉及的模块 |
| `alembic/` | 数据库迁移 |
| `requirements/` | 项目模块开关与配置实例 |
| `deploy/` | 容器、环境与部署示例 |
| `tests/` | 跨模块测试：集成主链路、路由装配与冲突、`jobs list`、事件契约、迁移 |

## 模块

| 分类 | 模块（标准 / 基础 / 规划见 `MODULES.md`） |
|---|---|
| 账号与权限 | 01-identity（会员登录与会话）、05-address、22-admin-rbac（后台账号、MFA、角色与 `module:*` 权限、门店范围、审计）、27-legal（协议与同意、购买条款） |
| 交易与资金 | 02-store、03-catalog、04-cart、11-delivery（规划）、07-inventory、08-order、09-wechat-pay、10-after-sales、15-recharge（储值）、29-member-card（会员卡：次卡 / 期限卡 / 时段卡） |
| 会员与营销 | 14-membership（积分）、16-coupon、17-campaign（计价与活动）、18-lottery |
| 通知与内容 | 19-review、20-notification（订阅消息 / 短信 / 站内信）、21-media、26-agent-support（规划）、28-wechat-mp（小程序服务端接口唯一客户端） |
| 运营与基础设施 | 00-framework、06-geo、12-queue、23-app-settings、24-report（规划）、13-reservation、25-jobs（定时任务与事件 worker） |

每个模块的状态、规则、事件、权限码与定制点见 `MODULES.md`，对外接口见 `CONTRACTS.md`。目录编号是稳定引用，不表示所有模块默认随项目启用。

## 运行

依赖为 Python 3.12+：运行时依赖在 `requirements.txt`，开发与测试另装 `requirements-dev.txt`（含 pytest）。

~~~powershell
# 在 backend/ 目录执行
python -m pip install -r requirements-dev.txt
python -X utf8 -m pytest -q                 # 全部模块测试 + 跨模块测试（tests/）
$env:LUZ_DATABASE_URL = "sqlite:///./luz-dev.db"
alembic upgrade head                        # 建表（全部模块）；改模型后 alembic revision --autogenerate，CI 跑 alembic check
python -m admin_rbac.bootstrap --seed       # 同步权限码与内置角色（每次发布）
python -m admin_rbac.bootstrap boss 店长     # 首次部署：创建首个超级管理员（临时口令只显示一次）
uvicorn app:app --reload                    # API
python -m jobs list                         # 列出全部定时任务与事件订阅；python -m jobs worker / scheduler 运行
~~~

`LUZ_FEATURES` 控制模块装配（未启用的模块不导入、不暴露接口；表仍由迁移建立）。数据库、密钥和外部服务配置通过环境变量提供；生产启动时 `Settings.assert_production_safe()` 检查框架配置并执行各模块用 `register_production_check` 登记的检查（identity、wechat_pay、wechat_mp），任何一项不通过即拒绝启动。

## 模块使用方法

1. 从 `projects/<project-slug>/HANDOFF.json` 确认页面、状态、权限与 API intention。
2. 在 `requirements/<project>.md` 记录启用的模块和项目差异。
3. 阅读所选 `NN-<name>/MODULE.md`，先确定数据所有权、事务边界与状态机，再实现代码。
4. 契约变化先更新 `API.md`、`CONTRACTS.md` 或 OpenAPI，再修改生产者和消费者。
5. 为正常、非法状态、权限、重复请求、并发、依赖失败与恢复路径补测试。
6. 在项目的 `docs/IMPLEMENTATION.md` 记录设计 ID 到实现和测试证据的映射。

## 职责边界

- Luz Dev 负责 API、数据模型、事务、权限执行、审计、缓存、后台任务、部署与运维。
- Luz System 负责用户角色、流程、页面层级、组件、交互状态与接口意图（交接契约），Luz Design 负责视觉令牌与素材规范。
- 平台限制导致用户体验变化时，先向 Luz System 提供约束和选项并更新交接，不在后端或前端静默改写已批准体验。
- 页面到 API 的绑定不作为后端设计的依据；以 `API.md` 和各模块 `MODULE.md` 的实际契约为准。
