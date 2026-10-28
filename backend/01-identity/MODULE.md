# 模块：auth-login（登录）

| 项 | 内容 |
|---|---|
| 状态 | 核心逻辑已实现（code 换 openid、令牌签发、刷新轮换），测试 6 项通过；短信验证码与管理后台登录仅设计 |
| 代码 | `01-identity/code/identity/`、测试 `01-identity/code/tests/` |
| 依赖 | framework（JWT、错误码、数据库） |

## 1 职责与边界

| 负责 | 不负责 |
|---|---|
| 微信小程序登录（code → openid） | 用户资料编辑（个人资料模块） |
| 用户创建与取回（upsert） | 权限判定（admin-rbac） |
| access / refresh 令牌签发与轮换 | 支付、地址等业务数据 |
| 手机号验证码校验（设计，见 §9） | 短信通道实现 |
| 敏感操作的 `fresh` 校验（设计） | 图形验证码（网关层） |

## 2 数据模型

| 表 | 字段 | 约束 | 说明 |
|---|---|---|---|
| `users` | id, openid, unionid, nickname, refresh_version, created_at | `openid` 唯一；`unionid` 索引 | refresh_version 用于刷新令牌轮换与强制下线 |

手机号绑定表 `user_phones`（设计）：user_id、phone（加密存储）、bound_at；唯一索引 `phone`。

## 3 接口

| 方法 | 路径 | 鉴权 | 幂等 | agent | 说明 |
|---|---|---|---|---|---|
| POST | `/api/v1/app/auth/login` | 无 | 否（天然幂等：同 openid 复用用户） | no | body: `{code}`；返回 `user_id, access_token, refresh_token, expires_in` |
| POST | `/api/v1/app/auth/refresh` | 刷新令牌 | 否 | no | body: `{refresh_token}`；返回新一对令牌，旧刷新令牌失效 |
| POST | `/api/v1/app/auth/logout` | access | 否 | no | 递增 refresh_version，全部刷新令牌失效 |
| POST | `/api/v1/app/auth/sms/send` | 无 | 否 | no | 设计：限流 4290，60 秒内同手机号只发一次 |
| POST | `/api/v1/app/auth/sms/verify` | 无 | 否 | no | 设计：验证码 5 分钟有效，错误 5 次作废 |

上游：微信 `https://api.weixin.qq.com/sns/jscode2session`，参数 `appid, secret, js_code, grant_type=authorization_code`。

## 4 关键流程

**小程序登录**
1. 前端 `wx.login()` 得到 `code`（5 分钟有效，一次性）。
2. 调用 `/auth/login`，后端用注入的 `code2session` 换取 `openid`（超时 3 秒，失败 → 5020）。
3. 取回或创建用户（`openid` 唯一索引兜底并发首次登录）。
4. 签发 access（2 小时）与 refresh（30 天，含 `rv` 版本号）。

**刷新令牌轮换**
1. 解码 refresh（类型必须为 refresh，签名与过期校验通过）。
2. 比对 `rv` 与数据库 `refresh_version`，不一致 → 4010，视为已撤销。
3. 条件更新 `refresh_version = 旧值 + 1`；rowcount 为 0 说明并发已使用 → 4010。
4. 签发新一对令牌。

**敏感操作（设计）**
- 接口声明 `fresh=5min`：access 的 `iat` 之后必须在 5 分钟内有一次手机号验证码通过（写入 `fresh_until` 声明）。否则返回 4010 并携带 `data.require = "sms_verify"`，前端弹出验证码。

## 5 状态机

用户无状态机；令牌状态：`valid → expired`（时间）或 `valid → revoked`（refresh_version 变化）。

## 6 并发与一致性

| 场景 | 机制 |
|---|---|
| 同一用户并发首次登录 | `openid` 唯一索引，IntegrityError 后回滚并重新读取 |
| 同一刷新令牌并发使用 | 条件 UPDATE 的 rowcount 判断，只有一个成功 |
| 大量登录请求 | 接口限流（网关）；code2session 设超时 |

## 7 配置

| 变量 | 说明 |
|---|---|
| `WECHAT_APPID` | 小程序 AppID |
| `WECHAT_APPSECRET` | 小程序 AppSecret，仅从环境变量或密钥服务读取 |
| `LUZ_JWT_SECRET` | 见 framework |

## 8 错误码

| 场景 | code | HTTP |
|---|---|---|
| code 为空 | 4001 | 400 |
| 微信返回无 openid | 4010 | 401 |
| 微信接口失败 / 超时 | 5020 | 502 |
| 令牌过期、签名错误、类型错误 | 4010 | 401 |
| 刷新令牌已撤销或已使用 | 4010 | 401 |
| 验证码错误次数超限（设计） | 4290 | 429 |

## 9 测试用例（已实现）

| 用例 | 断言 |
|---|---|
| 首次登录 | 创建用户，access 可解析出 user_id |
| 同 openid 再次登录 | 复用同一用户，表中只有 1 条 |
| 微信失败 | 5020 |
| 刷新轮换 | 新刷新令牌不同；旧刷新令牌再次使用失败 |
| 访问令牌当刷新令牌 | 4010 |
| 伪造刷新令牌 | 4010 |

## 10 可复用代码

`01-identity/code/identity/__init__.py`：`User`、`login_with_code`、`refresh_tokens`、`current_user_id`。
接入时在项目里提供 `code2session` 实现（真实微信调用），示例：

```python
import httpx
def wechat_code2session(code: str) -> dict:
    r = httpx.get("https://api.weixin.qq.com/sns/jscode2session", params={
        "appid": APPID, "secret": SECRET, "js_code": code, "grant_type": "authorization_code"}, timeout=3)
    data = r.json()
    if "errcode" in data and data["errcode"]:
        raise RuntimeError(f"wechat error {data['errcode']}")
    return {"openid": data["openid"], "unionid": data.get("unionid")}
```

## 11 前端对接

| 页面 | 接口 | 行为 |
|---|---|---|
| 启动 / 登录页 | `/auth/login` | 拿到 `wx.login` code 后调用；成功写入本地令牌 |
| 任意接口 | `Authorization` 头 | `4010` 时先调 `/auth/refresh`，成功则重放原请求一次；失败则跳登录 |
| 我的 → 退出 | `/auth/logout` | 清空本地令牌 |
| 敏感操作弹窗 | `/auth/sms/*` | `data.require == "sms_verify"` 时弹出验证码 |
