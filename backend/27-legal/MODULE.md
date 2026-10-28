# 模块：legal（协议与同意）

| 项 | 内容 |
|---|---|
| 状态 | 已实现：协议版本草稿与发布（旧版本退役、写审计）、当前生效版本、用户待同意文档、主动同意（校验版本与文本哈希）、撤回；测试 5 项 |
| 代码 | `27-legal/code/legal/`、测试 `27-legal/code/tests/test_legal.py` |
| 依赖 | framework（审计） |
| 说明 | `references/legal-documents.md`；模板 `assets/legal/`；规则 LEGAL-001～LEGAL-003、PII-*、FACE-002 |

## 1 数据

| 表 | 关键字段 |
|---|---|
| `legal_documents` | 类型（terms / privacy / face_consent / prepaid / refund / activity）、版本、标题、正文、正文哈希、状态（draft / published / retired）、生效时间；唯一（类型, 版本） |
| `user_consents` | 用户、文档、类型、版本、正文哈希、渠道、同意时间、撤回时间（只追加） |

## 2 接口（设计）

| 方法 | 路径 | 鉴权 |
|---|---|---|
| GET | `/api/v1/app/legal/{kind}` | 可选 |
| GET | `/api/v1/app/legal/pending` | 登录 |
| POST | `/api/v1/app/legal/consents` | 登录；提交文档 ID、版本、哈希 |
| POST | `/api/v1/app/legal/consents/{kind}/withdraw` | 登录 |
| POST | `/api/v1/admin/legal/documents` · `/{id}/publish` | `legal:publish`；写审计 |

## 3 规则

- 已发布文本不可修改，变更只能发布新版本；新版本发布后用户需重新同意。
- 同意只接受当前生效版本与客户端展示文本的哈希，防止对旧文本或被篡改的文本“同意”。
- 撤回后调用方停止相应处理并删除数据（如人脸），覆盖第三方。
