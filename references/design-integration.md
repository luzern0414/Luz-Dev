# 接入 Luz System 前端设计

分工：**Luz System 负责系统设计与前端的体验和界面**（模块范围、架构、功能清单、界面、可交互原型与硬件，均经用户按哈希批准；视觉标准来自 Luz Design）；**Luz Dev 负责把它变成可上线的前端**：真实接口与数据、登录与权限、状态处理、构建、联调、发布。Luz Dev 不改变已批准的页面、流程、权限与层级；需要改动时退回 Luz System。

工具：`scripts/design_intake.py`（测试 `tests/test_design_intake.py`）。

## 1. 设计产物与稳定 ID

Luz System 会话目录 `D:\Codes\skill输出\Luz-System\NNN-项目\`：成品 HTML 在根目录，过程文件都在 `raw/` 下（早期会话没有 `raw/`，`design_intake.py` 两种都认）。

| 文件 | 内容 |
|---|---|
| `raw/99-delivery/MANIFEST.json` | 会话、各阶段文件路径 |
| `raw/99-delivery/APPROVALS.json` | 各阶段状态、批准人、时间、文件 SHA-256；原型另有浏览器验收截图哈希 |
| `raw/01-system/03-system-architecture.html` | 功能、页面、导航、组件、操作、数据实体、流程、状态、集成、角色 |
| `raw/01-system/04-system-design.html` | 系统设计（页面细节、状态、权限） |
| `raw/02-visual/visual-board.html` | 视觉方向与令牌 |
| `raw/03-prototype/interactive-prototype.html` | 可交互原型（模拟数据） |
| `HANDOFF.json`（可选，交给其他团队时才有） | 页面、流程、组件、素材、接口意图、假设、证据；没有时以上面的批准文件和绑定表为准 |

| ID | 含义 | Luz Dev 的对应工作 |
|---|---|---|
| `F-` 功能 | 业务功能 | 方案模块与功能目录条目 |
| `P-` 页面 | 页面 | 前端路由 / 小程序页面；标注 `data-page-id` |
| `A-` 操作 | 按钮、提交、跳转 | 绑定到接口或跳转；标注 `data-action-id` |
| `D-` 数据实体 | 业务对象 | 数据表与接口模型 |
| `I-` 集成 | 支付、人脸、短信、地图等 | 后端适配器 |
| `FL-` 流程 | 用户旅程 | 端到端测试用例 |
| `S-` 状态 | 加载、空、错误、成功、禁用… | 每个页面的状态实现（UI-001） |
| `R-` 角色 | 用户、店员、管理员 | 权限码与数据范围 |
| `C-` 组件、`N-` 导航 | 复用组件、导航 | 组件库与导航结构 |

## 2. 接入步骤

```powershell
# 1) 核验：阶段已批准，且文件与批准时的哈希一致（原型还核验浏览器验收截图）
python -X utf8 scripts/design_intake.py inspect "D:\Codes\skill输出\Luz-System\001-健身房小程序和后台系统" --require architecture,system_design,prototype --out <方案数据目录>\design-intake.json

# 2) 生成绑定表：每个操作 / 实体 / 集成 / 页面一行
python -X utf8 scripts/design_intake.py bind "<会话目录>" --out <项目>\packages\contracts\design-bindings.json

# 3) 填写绑定表（与 OpenAPI 契约同时完成），检查
python -X utf8 scripts/design_intake.py check <项目>\packages\contracts\design-bindings.json

# 4) 实现前端后检查覆盖：每个操作与页面都在源码中实现，没有残留原型模拟数据
python -X utf8 scripts/design_intake.py check <绑定表> --frontend <项目>\apps\miniprogram --frontend <项目>\apps\admin
```

绑定表每个操作的 `kind`：`api`（填写“方法 路径”）、`navigate`（页面跳转）、`local`（纯前端交互）、`platform`（微信能力，如 `wx.requestPayment`，在 notes 说明）、`n/a`（说明原因）。设计更新后再次 `bind` 会保留已填内容，并把设计中删除的行标记为 `removedFromDesign`。

## 3. 实现规则

| 规则 | 做法 |
|---|---|
| 保留 ID | 页面根元素与操作控件保留 `data-page-id`、`data-action-id`（或在组件中用常量标注），用于覆盖检查、埋点与自动化测试 |
| 替换模拟数据 | 原型中的 `data-fixture-id` 与 DEMO 数据全部换成接口数据；正式代码不引用 mock 目录（PROC-001） |
| 状态 | 原型中每个 `data-state-kind`（加载、空、错误、成功、禁用）都要由真实接口状态驱动，补齐超时、离线、无权限、登录过期 |
| 令牌 | 颜色、字号、间距使用设计令牌（Luz Design 主题，经 Luz System 设计确定），不写死数值 |
| 新增交互 | 前端出现设计中不存在的 `A-` 操作时检查失败；先回到 Luz System 补设计并重新批准 |
| 技术约束 | 实现中发现平台限制（例如支付必须先查单再展示结果）时，把约束与选项写给 Luz System，由其更新设计 |
| 结果页 | 支付、提交、异步任务的落点按 `references/result-patterns.md` |
| 对照验收 | 关键页面截图与原型验收截图对照，差异写入实施记录 |

## 4. 技术栈对应

| 端 | 默认 | 说明 |
|---|---|---|
| 小程序 | 原生小程序 + TDesign / Vant（只用行为） | 页面目录对应 `P-`；请求统一封装（MP-001） |
| 管理后台 | Vue 3 + Vite + TypeScript + Element Plus | 路由对应 `P-`；按钮权限对应 `R-` 与权限码 |
| 移动 App | Flutter / React Native | 同上 |

契约：`packages/contracts/openapi.yaml` 是前后端唯一接口来源，前端客户端由它生成；绑定表中的 `api` 必须在 OpenAPI 中存在。

## 5. 门禁

- 方案的“页面与前端接入”章节写明：设计会话路径、各阶段批准状态、页面 / 操作数量、绑定表位置。
- 设计未批准或哈希不一致时，前端开发不开始（`inspect --require`）。
- 交付前 `check --frontend` 通过：操作与页面覆盖 100%，无模拟数据残留，无未设计的操作。
