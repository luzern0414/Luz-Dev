# Luz Dev Experience

这个目录保存 Luz Dev 的任务经验、已验证的 Playbook、可复用实现，以及每次调用后沉淀的偏好、教训与有效信息。

## 两类条目

| 存放位置 | 内容 | 写入 | 上限 |
|---|---|---|---|
| `notes.json` | 用户偏好（preference）、教训（lesson）、有效信息（fact），每条一句通用原则 | `record_experience.py note --kind … --key … --text …` | 偏好 60、教训 120、有效信息 120 |
| `records/EXP-*.md` | 一次任务的完整经验：摘要、决策、失败尝试、证据、可复用候选 | `record_experience.py record --key … …` | 有效记录 120 |

两类条目都按 `key` 去重：同一 key 再写会替换原内容，保留一次 `previous` 供追溯，不新增条目。超过上限时 `prune` 先删除 180 天未确认的，再删除使用次数最少、最久未确认的；经验记录超限时自动废弃价值最低的 candidate。`check` 发现重复 key、重复内容、个人信息或超限即失败。INDEX.md 在每次写入后自动重建。

## 状态

- candidate：来自单次任务，可用于检索和观察，不能作为默认规则。
- verified：有测试、生产结果或用户确认支持，可被后续方案引用。
- deprecated：已失效或被替代，保留历史，不再用于新任务。

## 使用流程

1. 任务开始时运行 `record_experience.py search <关键词>`，应用匹配的偏好与教训，优先读取 verified 资产。
2. 任务结束时用 `note` 写入新的偏好、教训与有效信息；用 `record` 记录任务经验，同一主题沿用原 key。
3. 证据充分后使用 promote 转为 verified。
4. 规则失效时使用 deprecate，写明原因和替代经验。

## 复用边界

可复用代码必须包含说明、依赖、最小示例、测试和已知限制。不得保存真实凭证、商户数据、支付私钥或个人敏感信息。仓库是公开的：姓名、手机号、微信号、客户或公司名称、本机路径只能放在 git 忽略的 `*.local.json` 中。
