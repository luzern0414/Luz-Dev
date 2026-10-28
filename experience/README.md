# Luz Dev Experience

这个目录保存 Luz Dev 的任务经验、已验证的 Playbook 和可复用实现。

## 状态

- candidate：来自单次任务，可用于检索和观察，不能作为默认规则。
- verified：有测试、生产结果或用户确认支持，可被后续方案引用。
- deprecated：已失效或被替代，保留历史，不再用于新任务。

## 使用流程

1. 任务开始后搜索 INDEX.md，优先读取 verified 资产。
2. 任务结束时用 scripts/record_experience.py record 创建记录。
3. 证据充分后使用 promote 转为 verified。
4. 规则失效时使用 deprecate，写明原因和替代经验。

## 复用边界

可复用代码必须包含说明、依赖、最小示例、测试和已知限制。不得保存真实凭证、商户数据、支付私钥或个人敏感信息。
