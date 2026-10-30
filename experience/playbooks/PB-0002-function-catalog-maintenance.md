# 维护功能逻辑目录

- 来源经验：EXP-0002
- 状态：verified
- 适用场景：Luz 后端模块设计、订单/支付/预约/库存/会员/优惠券/配送/抽奖等通用业务逻辑复用，以及人类查阅功能基线
- 不适用场景：把参考状态直接当作目标项目已实现事实；保存真实商户密钥、支付凭据或用户敏感数据
- 前置条件：技术方案已经生成；项目的 Luz System 设计会话、绑定表、代码和接口现状可以核对
- 关键步骤：
  1. 先按 SKILL.md 路由读取 references/function-catalog.json，并搜索 experience/INDEX.md。
  2. 用模块 ID、功能 ID、sourceRefs 和表格行筛选相关逻辑；只借鉴模式，按目标项目重新确认接口、权限、金额、状态机和并发约束。
  3. 修订只改 references/function-catalog.json（唯一来源）：模块/功能唯一 ID、逻辑项、状态值与 backend/MODULES.md 的实现状态一致，每个功能有验证行与状态/失败行。
  4. 运行 python -X utf8 scripts/build_function_catalog.py 生成阅读页 references/function-catalog.html（Luz Design 阅读页，按关键词与模块筛选），再运行功能目录测试与 validate_skill_package.py（含 check_document 零 ERROR）。
  5. 人类查阅打开 references/function-catalog.html；页面不提供编辑，修改回到第 3 步。
  6. 新增经验使用 scripts/record_experience.py；只有测试或用户确认等证据存在时才标记 verified。
- 失败模式与恢复：HTML 与 canonical JSON 漂移时 validate_skill_package.py 报“stale”，重新构建；目录文字一律经转义渲染；check_document 报错时改 JSON 里的措辞再构建。
- 测试与证据：tests/test_function_catalog.py 通过；validate_skill_package.py 检查 canonical JSON、页面与重新渲染结果一致、阅读页结构、安全渲染、无外部依赖与 check_document。
- 可复用代码：references/function-catalog.json、scripts/build_function_catalog.py、assets/reference/function-catalog.template.html。
- 维护责任与复查条件：Luz Design 阅读页规则、目录 schema、必选业务模块或 backend 模块状态变化时重新验证并更新 EXP-0002。
