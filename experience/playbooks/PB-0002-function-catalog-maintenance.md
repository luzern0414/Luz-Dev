# 维护可编辑功能逻辑目录

- 来源经验：EXP-0002
- 状态：verified
- 适用场景：Luz 后端模块设计、订单/支付/预约/库存/会员/优惠券/配送/抽奖等通用业务逻辑复用，以及人类编辑功能基线
- 不适用场景：把参考状态直接当作目标项目已实现事实；保存真实商户密钥、支付凭据或用户敏感数据
- 前置条件：技术方案已经生成；项目 HANDOFF、代码和接口现状可以核对
- 关键步骤：
  1. 先按 SKILL.md 路由读取 references/function-catalog.json，并搜索 experience/INDEX.md。
  2. 用模块 ID、功能 ID、sourceRefs 和表格行筛选相关逻辑；只借鉴模式，按目标项目重新确认接口、权限、金额、状态机和并发约束。
  3. 人类需要浏览或修订时打开 references/function-catalog.html，开启“编辑表格”。浏览器修改只属于 localStorage 草稿。
  4. 正式维护先导出 JSON，校验 schemaVersion、模块/功能唯一 ID、九个逻辑项、状态值和验证/失败行，再替换 references/function-catalog.json。
  5. 运行 python -X utf8 scripts/build_function_catalog.py 重新生成 HTML，并运行功能目录测试与 validate_skill_package.py。
  6. 新增经验使用 scripts/record_experience.py；只有测试或用户确认等证据存在时才标记 verified。
- 失败模式与恢复：HTML 与 canonical JSON 漂移时停止发布并重新构建；导入失败时保留既有草稿；XSS 风险通过 textContent 与 script-close 转义阻断；模板新增控件后同步静态契约测试和包校验。
- 测试与证据：tests/test_function_catalog.py 6 项通过；tests/test_skill_automation.py 9 项通过；validate_skill_package.py 检查 canonical JSON、HTML 内嵌数据、编辑控件、安全渲染和无外部依赖。
- 可复用代码：references/function-catalog.json、scripts/build_function_catalog.py、assets/reference/function-catalog.template.html。
- 维护责任与复查条件：Luz Design Admin 规范、目录 schema、必选业务模块、页面控件或运行环境变化时重新验证并更新 EXP-0002。
