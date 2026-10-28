# Plan-first Luz task delivery

- 来源经验：EXP-0001
- 状态：verified
- 适用场景：任何 Luz Dev 开发、审查、排障、分析、解释或运维任务
- 不适用场景：与 Luz Dev 无关的普通问答
- 前置条件：可以写入 D:\Codes\skill输出\Luz-Dev
- 关键步骤：
  1. 先运行 scripts/create_tech_plan.py 创建 NN-主题.html。
  2. 读取 HANDOFF、仓库和 experience/INDEX.md 中与本任务相关的已验证资产。
  3. 在开始修改或外部操作前补全技术方案。
  4. 执行变更，并为范围变化追加修订。
  5. 运行测试与包校验，创建 EXP-NNNN 经验记录。
  6. 只有具备可复核证据时才晋升经验或复用代码。
- 失败模式与恢复：方案目录不可写时停止任务并报告；编号锁超时则检查残留进程；经验缺少证据时保持 candidate。
- 测试与证据：tests/test_skill_automation.py 的 8 项测试；scripts/validate_skill_package.py 的 PASS 结果。
- 可复用代码：scripts/create_tech_plan.py、scripts/record_experience.py。
- 维护责任与复查条件：模板、输出路径、任务类型或证据规则变化时重新验证。
