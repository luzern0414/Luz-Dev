# Luz Dev Experience

这个目录保存 Luz Dev 的任务经验、已验证的 Playbook、可复用实现，以及每次调用后沉淀的偏好、教训与有效信息。

## 两类条目

| 存放位置 | 内容 | 写入 | 上限 |
|---|---|---|---|
| `notes.json` | 偏好（preference）、教训（lesson）、有效信息（fact），每条一句通用原则；与 Luz-System、Luz-Design、Luz-Writing 同一文件名、字段与命令 | `record_experience.py note --kind … --key … --text … [--why …]`，另有 `list`、`search`、`prune`、`check` | 偏好 60、教训 120、有效信息 120 |
| `records/EXP-*.md` | 一次任务的完整经验：摘要、决策、失败尝试、证据、可复用候选 | `record_experience.py record --key … …` | 有效记录 120 |

`notes.json` 的结构：`{"schema_version": "1.0", "caps": {...}, "notes": [...]}`，每条有 `key`、`kind`、`text`、可选 `why`、`uses`、`first_seen`、`last_confirmed`（年-月-日），被替换过的再带 `previous`（`text`、`replaced_at`）。

去重、替换、上限、裁剪与隐私规则以 `SKILL.md` 的“每次调用后沉淀”为准；INDEX.md 在每次写入后自动重建。

## 状态

- candidate：来自单次任务，可用于检索和观察，不能作为默认规则。
- verified：有测试、生产结果或用户确认支持，可被后续方案引用。
- deprecated：已失效或被替代，保留历史，不再用于新任务。

## 使用流程

1. 任务开始时运行 `record_experience.py search <关键词>`，应用匹配的偏好与教训，优先读取 verified 资产。
2. 每次调用结束前用 `note` 写入或确认至少一条偏好、教训或有效信息（必做）；产出了可复用做法、失败分析或值得保留的证据时，再用 `record` 记录任务经验，同一主题沿用原 key。
3. 证据充分后使用 promote 转为 verified。
4. 规则失效时使用 deprecate，写明原因和替代经验。

## 复用边界

可复用代码必须包含说明、依赖、最小示例、测试和已知限制。不得保存真实凭证、商户数据、支付私钥或个人敏感信息。仓库是公开的：姓名、手机号、微信号、客户或公司名称、本机路径只能放在 git 忽略的 `*.local.json` 中。
