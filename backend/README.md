# 后端（backend）

本目录是 Luz Dev 的后端基线：包含架构规范、编号业务模块、可复用代码、契约、迁移与部署配置。先阅读 `SPEC.md` 与 `MODULES.md`，再只加载当前任务需要的模块文件。

## 入口与目录

| 路径 | 作用 |
|---|---|
| `SKILL.md` / `WORKFLOW.md` | 开发技能的入口与交付门禁（位于上一级） |
| `SPEC.md` | 分层、接口、错误码、幂等、并发、安全、测试与交付总则 |
| `API.md` | 接口清单、鉴权、幂等、幂等键与实现状态 |
| `CONTRACTS.md` | 领域事件、稳定 ID、平台契约 |
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
| `tests/` | 跨模块集成测试 |

编号模块覆盖身份、门店、目录、购物车、地址、定位、库存、订单、微信支付、售后、配送、排队、预约、会员、储值、优惠券、活动、抽奖、评价、通知、媒体、后台 RBAC、应用设置、报表、任务与智能体支持。目录编号是稳定引用，不表示所有模块默认随项目启用。

## 运行

依赖为 Python 3.11+，版本约束见 `requirements.txt`。

~~~powershell
# 从 Luz-Dev 根目录执行
python -m pip install -r backend/requirements.txt
python -m pytest -q backend
uvicorn app:app --app-dir backend --reload
~~~

`LUZ_FEATURES` 控制模块装配；未启用的模块不应导入、建表或暴露接口。数据库、密钥和外部服务配置通过环境变量提供，生产启动必须通过 `assert_production_safe`。

## 模块使用方法

1. 从 `projects/<project-slug>/HANDOFF.json` 确认页面、状态、权限与 API intention。
2. 在 `requirements/<project>.md` 记录启用的模块和项目差异。
3. 阅读所选 `NN-<name>/MODULE.md`，先确定数据所有权、事务边界与状态机，再实现代码。
4. 契约变化先更新 `API.md`、`CONTRACTS.md` 或 OpenAPI，再修改生产者和消费者。
5. 为正常、非法状态、权限、重复请求、并发、依赖失败与恢复路径补测试。
6. 在项目的 `docs/IMPLEMENTATION.md` 记录设计 ID 到实现和测试证据的映射。

## 职责边界

- Luz Dev 负责 API、数据模型、事务、权限执行、审计、缓存、后台任务、部署与运维。
- Luz System 负责用户角色、流程、页面层级、组件、交互状态，Luz Design 负责视觉令牌、Figma/素材与 API intention。
- 平台限制导致用户体验变化时，先向 Design 提供约束和选项并更新交接，不在后端或前端静默改写已批准体验。
- 页面到 API 的绑定不作为后端设计的依据；以 `API.md` 和各模块 `MODULE.md` 的实际契约为准。
