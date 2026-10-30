# 运维与上线

Luz Dev 负责从开发到上线的全流程。本篇覆盖环境、部署、网关、发布与回滚、监控告警、备份恢复、CI/CD、压测与上线检查；资质与备案见 `compliance.md`，协议见 `legal-documents.md`。
模板（运维套件，文件清单见 §8）：`assets/deploy/`、`assets/monitoring/`、`assets/ci/`、`assets/loadtest/`，镜像构建 `backend/deploy/Dockerfile`。相关规范：REL-*、OPS-*、DATA-002、DATA-003、OBS-*、SECRET-*、CONF-*、PROC-004。运行配置文件与日志的写法见 `config-logging.md`。

## 1. 环境

| 环境 | 用途 | 规则 |
|---|---|---|
| 本地 | 开发 | 依赖用容器启动（`backend/deploy/docker-compose.yml`）；测试数据可清理 |
| 测试 / 预发 | 联调、验收、演练、压测 | 与生产同构（同一 compose 模板、同版本数据库、同配置结构）；微信支付使用独立商户配置或小额真实验证；数据脱敏 |
| 生产 | 正式 | 只能通过发布流程变更（`release.sh`）；镜像按 digest 部署；秘密只在密钥服务或受限文件 |

每个环境一份配置清单（`config/<env>.md`）：配置项、用途、获取方式、验证方法；秘密只写存放位置（PROC-004、SECRET-001）。运行参数（超时、上限、开关）写在 `config/runtime/*.toml`，服务端热加载，不在后台展示（`config-logging.md` §2、OPS-005）。

## 2. 部署默认方案

| 层 | 默认 | 说明 |
|---|---|---|
| 服务器 | 云服务器（1–2 台起步）+ 云数据库或自建 PostgreSQL 16 | 小程序后端服务器域名必须完成 ICP 备案（compliance.md） |
| 运行 | Docker Compose：`nginx`、`api`（2 副本）、`worker`（发件箱）、`scheduler`（定时任务）、`postgres`、`redis`；`--profile monitoring` 加监控栈 | `assets/deploy/docker-compose.prod.yml` |
| 进程 | 每容器 1 个 uvicorn 进程，按副本扩容；`--proxy-headers`、优雅关闭 20s、keep-alive 75s | `stop_grace_period` 30s（worker 60s）必须大于优雅关闭时间 |
| 健康 | `/healthz` 存活（镜像 HEALTHCHECK，不查依赖）；`/readyz` 就绪（DB + 检查项，compose 与滚动发布用） | 依赖故障只摘流量，不连锁重启 |
| 加固 | 非 root（uid 10001）、只读根文件系统 + tmpfs、`no-new-privileges`、`cap_drop: ALL`、资源与 pids 限额、json-file 轮转 | 数据层网络 `internal: true`，不能出公网 |
| 入口 | Nginx 1.30：Mozilla intermediate TLS、HSTS 2 年、安全头、未知 Host 444、JSON 访问日志、限流 | `assets/deploy/nginx.conf`，见下表 |
| 迁移 | 一次性 `migrate` 容器先于 api 替换执行；expand/contract | `assets/deploy/RUNBOOK.md` §1 |
| 管理后台 | 静态资源由 Nginx 提供，独立子域名；`/api/v1/admin/` 只在 admin 域名转发，可限制来源 IP | api 域名访问 `/api/v1/admin/` 返回 404 |
| 小程序 | 微信开发者工具上传 → 体验版验收 → 提交审核 → 发布 | 服务器域名在小程序后台配置为 HTTPS 合法域名 |

网关参数（nginx.conf，上线首周可用 `limit_req_dry_run on` 观察）：

| 项 | 值 |
|---|---|
| 通用接口 `/api/v1/app/`、`/api/v1/` | 10 r/s 每 IP，burst 20 nodelay，超限 429 |
| 登录 `/api/v1/app/auth/login`、`/api/v1/admin/auth/login` | 10 r/m 每 IP，burst 5；应用层另有账号锁定 |
| 短信 `/api/v1/app/auth/sms/send` | 1 r/m 每 IP，burst 2；应用层另有手机号维度 |
| 支付与渠道回调 `/api/v1/payments/notify/`、`/api/v1/webhooks/` | 不限流，只靠验签；read 5s；体积 64k / 256k |
| 并发连接 | 每 IP 20、每虚拟主机 1000 |
| 超时 | 上游 connect 3s / send 10s / read 30s（报表导出 60s）；客户端 header 10s / body 15s |
| 请求体 | 1m（应用层 256KB）；上传走对象存储预签名直传 |
| 不对外 | `/metrics`、`/readyz`、`/healthz`、`/docs`、`/openapi.json` 返回 404；未列出路径 404 |
| 白名单 | `geo $luz_ratelimit_exempt`（压测机、固定出口），key 为空不计数 |

真实客户端 IP：nginx 为最外层时向上游发送 `X-Forwarded-For: $remote_addr`（覆盖，不追加）；api 只信任 nginx 固定地址 `172.30.0.10`（uvicorn `FORWARDED_ALLOW_IPS`、应用 `LUZ_TRUSTED_PROXIES`，后者取最右侧不可信地址）。前面有云 SLB/CDN 时，在 nginx 打开 `set_real_ip_from <回源网段>` + `real_ip_recursive on`，后端配置不变；禁止信任 `0.0.0.0/0`。`/metrics` 只对 data 网段（`LUZ_INTERNAL_NETWORKS=172.30.1.0/24`）开放，Prometheus 另带 `LUZ_METRICS_TOKEN`。

服务器文件清单（均不进仓库）：

| 路径 | 内容 | 权限 |
|---|---|---|
| `/etc/luz/api.env` | 应用配置与秘密（模板 `api.env.example`） | 600 root |
| `/etc/luz/redis.conf` | requirepass、持久化、maxmemory（模板 `redis.conf.example`） | 640 root:999 |
| `/etc/luz/secrets/postgres_password`、`metrics_token`、`grafana_admin_password` | compose secrets | 400；`metrics_token` 需 uid 65534 可读 |
| `/etc/luz/secrets/alertmanager/{critical,chat,deadman}_webhook` | 告警通道 URL | 440 root:65534 |
| `/etc/luz/monitoring.env` | exporter 连接串（只读账号 `luz_monitor`） | 600 root |
| `/etc/luz/backup.env` | 加密公钥/口令文件路径、上传钩子 | 600 root |
| `/etc/luz/keys/`、`/etc/luz/certs/` | 支付密钥；证书 + `ffdhe2048.pem` | 400 / 证书目录 700 |
| `/var/backups/luz/wal` | WAL 归档目录 | 700 999:999 |

## 3. 发布流程

```text
合并到主分支 → CI：lint、单元、集成（PostgreSQL/Redis）、迁移检查、契约测试、安全扫描 → 构建镜像（SBOM、provenance、镜像扫描）
 → 预发：release.sh 部署 → k6 smoke → 关键流程（下单、支付、退款）
 → 打 v* tag → 生产 environment 人工审批
 → release.sh：预检 → 发布前备份 → 迁移 → api 健康门控滚动替换 → worker/scheduler → 冒烟 → 观察 15 分钟（触发即自动回滚）
 → 发布小程序体验版验收 → 提交审核 → 发布
```

| 回滚触发（任一） | 阈值 |
|---|---|
| 5xx 比例 | > 2%，5 分钟窗口 |
| p95 | > 发布前基线 × 2（且 > 300ms） |
| 支付回调验签失败 | 10 分钟 > 5 次 |
| 冒烟 / 任一副本 `/readyz` / 关键流程 | 失败 |

回滚：`release.sh rollback` 回到上一 digest，不回退数据库（迁移遵循 expand/contract，CI 对未标 `# luz: contract` 的破坏性迁移直接失败）；迁移本身有误时前向修复，其次 CI 已验证的 `alembic downgrade`，最后才用发布前备份或 PITR。小程序可在后台回退到上一版本（以官方说明为准）。每次发布写发布记录：版本、digest、迁移 revision、执行人、结果（`release.sh` 自动写 `.release/history.tsv`）。细节见 `assets/deploy/RUNBOOK.md`。

发布前齐备：带版本的源码与迁移、环境与秘密清单、功能开关与安全默认值、带来源证明的构建产物、接口与事件兼容说明、备份与恢复步骤、回滚触发条件与负责人、告警接收方。发布顺序：校验配置与秘密 → 先做向后兼容的表结构变更 → 部署能兼容新旧数据的生产者与消费者 → 平台支持时逐步切流 → 核对健康、关键链路、业务指标与用户可见状态 → 清理放到下一个版本。只有数据契约仍兼容时才回滚代码，不可逆的迁移用前向修复；回滚路径不明确的发布不算完成。

## 4. 监控与告警

Prometheus 抓取 api 各副本（Docker DNS 发现）、sql-exporter（业务表）、postgres/node/blackbox exporter；规则在 `assets/monitoring/rules/`。critical → 电话/短信 webhook，warning → 群消息，Watchdog → 外部死人开关。

| 类别 | 指标 / 告警 | 阈值 |
|---|---|---|
| 可用性 | 99.9% SLO 多窗口燃烧率 | 1h&5m > 14.4×、6h&30m > 6×（critical）；3d&6h > 1×（warning） |
| 错误 | `ApiHigh5xxRatio` | 5xx > 2% 持续 5 分钟 |
| 延迟 | 按路由 p95（直方图） | 读 > 300ms、写 > 800ms（10 分钟，warning）；整体读 > 1s / 写 > 2s（critical）；支付回调 p95 > 3s |
| 外部 | blackbox 探测、证书 | 探测失败 3 分钟；证书 < 21 天 warning、< 7 天 critical |
| 饱和度 | CPU、内存、磁盘、1m p99、数据库连接 | CPU > 85%、可用内存 < 10%、磁盘 < 15% 或 24h 内写满、连接 > 80% |
| 异步 | 发件箱最老积压、死信、任务最近成功、任务失败 | 积压 > 5 分钟；死信 > 0；分钟级任务 15 分钟 / 日任务 50 小时无成功；1h 失败 ≥ 3 |
| 支付 | 待确认超 30 分钟、需人工、验签失败 | > 0（warning）、> 20（critical）；验签失败 10 分钟 > 5 |
| 备份 | 每日备份、基础备份、WAL 外传、WAL 归档、恢复校验 | > 26h；> 8 天；> 15 分钟；归档失败即告警；恢复校验 > 8 天 |
| 安全 | 登录/短信限流命中、未处理异常 | 持续 > 1/s；10 分钟 > 10 次 |

日志：应用与 nginx 都输出 JSON 到 stdout（带 `request_id`/`trace_id`，OBS-001）；本地 json-file 只做轮转缓冲（api 50m×5、nginx 100m×10）。网络日志（nginx 访问日志、认证/权限/支付回调/管理操作日志、安全事件）由采集器集中存储 **不少于 6 个月**（《网络安全法》第二十三条，2025 年修正、2026-01-01 施行，原第二十一条，期限不变），存储开启只读或对象锁防删改；分类、字段、脱敏、留存期限表与日志告警见 `config-logging.md` §3（OBS-004 至 OBS-008），保留期限写入方案。

## 5. 备份与恢复

| 对象 | 方式 | 频率 | 保存 |
|---|---|---|---|
| 数据库（PITR） | `archive_command` 持续归档 WAL（`archive_timeout=60`，RPO ≈ 1 分钟）+ `backup.sh base` 基础备份 | WAL 每 5 分钟加密外传；基础备份每周 | 异地加密；WAL 覆盖最老基础备份之后 |
| 数据库（逻辑） | `backup.sh daily`：`pg_dump -Fc` + `pg_dumpall -g`，`pg_restore --list` 试读，sha256 | 每日 + 每次发布前 | 本地与异地 30 天（云数据库自带备份时以云备份为主，本副本跨厂商） |
| 加密 | 首选 GPG 公钥（私钥离线），备选对称口令文件 | — | 异地桶开启版本与对象锁 |
| 恢复校验 | `verify-restore.sh`：隔离容器恢复 + 表检查 + 耗时 | 每周自动；每季度人工 PITR 演练 | 指标与演练记录（RTO/RPO） |
| Redis | AOF everysec + RDB；只存锁/限流/幂等等可重建状态 | — | 丢失可重建；存业务数据时每日外传 RDB |
| 上传文件 | 对象存储开启版本或跨区域复制 | — | — |
| 配置与证书 | 变更即备份 | — | 受限访问 |

备份成功后写 node_exporter textfile 指标 `luz_backup_last_success_timestamp_seconds{kind}`，超过 26 小时告警。恢复步骤（逻辑恢复、PITR）见 `assets/deploy/RUNBOOK.md` §3（DATA-003）。

## 6. 安全加固

- 服务器：SSH 仅密钥登录、禁止 root 密码登录、安全组只开放 80/443 与受限的管理端口；系统自动安全更新。
- 数据库、Redis 不暴露公网（data 网络 `internal: true`、不映射端口）；Redis 口令只在配置文件，不在命令行参数。
- Prometheus、Alertmanager、Grafana 只绑定 127.0.0.1，远程访问走 SSH 隧道。
- 管理后台：登录限速与锁定、可选来源 IP 白名单、操作审计（AUTH-005、OBS-002）。
- 微信支付回调地址公网可达；防火墙策略允许回调到达，nginx 不对回调限流（PAY-013）。
- CI：最小权限 `GITHUB_TOKEN`、action 固定 commit SHA（2026-03 trivy-action tag 被篡改事件）、部署密钥仅在 environment 中可见。

## 7. 上线检查表

上线前逐项确认并在方案“上线与合规”章节记录证据（逐项模板：`assets/deploy/LAUNCH-CHECKLIST.md`）：

| 类别 | 检查项 |
|---|---|
| 资质 | 主体资质、ICP 备案（网站与小程序）、小程序类目、支付商户号与 appid 绑定、特殊能力资质（compliance.md） |
| 协议 | 用户协议、隐私政策、小程序隐私保护指引、专项同意（人脸、储值）已上线且有同意记录（legal-documents.md） |
| 配置 | 生产配置与秘密到位；联调密钥已移除；服务器域名已配置 |
| 数据 | 迁移在预发演练通过；初始化数据（管理员、字典、门店）就绪；首次登录强制改密 |
| 功能 | 关键流程真实走通：登录、下单、支付（真实小额）、回调、查单、退款、对账 |
| 前端 | Luz System 设计绑定检查通过；关键页面与原型截图对照 |
| 网关 | 未知 Host 444；运维端点公网 404；伪造 XFF 无效；TLS 评级 A；限流观察期结束 |
| 压测 | 预发 k6 load 通过：读 p95 < 300ms、写 p95 < 800ms、错误率 < 0.5% |
| 运维 | 健康检查、监控告警（测试告警已送达）、日志集中留存 ≥ 6 个月、备份与恢复演练、回滚演练与上一版本 digest |
| 文档 | README、`config/`、发布记录、RUNBOOK、遗留问题 |
| 发布后 | 自动观察 15 分钟 + 人工值守 30 分钟、核对首日订单与对账、用户反馈渠道 |

## 8. 运维套件文件

| 文件 | 覆盖 |
|---|---|
| `assets/deploy/nginx.conf` | TLS（Mozilla intermediate）、HSTS、安全头、444、JSON 日志、真实 IP、限流、超时、keepalive、回调与后台分离 |
| `assets/deploy/docker-compose.prod.yml` | 生产编排：副本、健康依赖、加固、资源限额、日志轮转、网络分区、secrets、WAL 归档、monitoring profile |
| `backend/deploy/Dockerfile`、`backend/.dockerignore` | 多阶段、digest 固定基础镜像、哈希锁依赖（`requirements.lock`）、非 root、exec CMD、HEALTHCHECK |
| `assets/deploy/api.env.example`、`redis.conf.example` | 应用与 Redis 配置模板（变量名与 `luzback/config.py` 一致） |
| `assets/deploy/release.sh` | 发布 / 回滚 / 观察：预检、发布前备份、迁移、健康门控滚动替换、量化回滚触发 |
| `assets/deploy/backup.sh`、`verify-restore.sh` | 基础备份、WAL 外传、每日 dump + globals、加密、异地钩子、30 天保留、新鲜度指标；恢复校验 |
| `assets/deploy/RUNBOOK.md` | 发布、回滚、逻辑恢复、PITR、演练与留存 |
| `assets/deploy/LAUNCH-CHECKLIST.md` | 上线逐项证据表 |
| `assets/monitoring/prometheus.yml`、`rules/luz-recording.yml`、`rules/luz-alerts.yml` | 抓取、SLO/延迟记录规则、告警规则 |
| `assets/monitoring/alertmanager.yml`、`blackbox.yml` | 告警路由（critical/warning/Watchdog）、外部探测与证书 |
| `assets/monitoring/sql-exporter/` | 发件箱、定时任务、支付积压的 SQL 指标（只读账号） |
| `assets/monitoring/grafana/` | 数据源与仪表盘自动加载、`Luz API 概览` 仪表盘 |
| `assets/ci/github-actions.yml` | CI/CD 模板（§9） |
| `assets/loadtest/k6-api.js` | k6 场景：浏览、登录、下单；smoke / load / soak |

## 9. CI/CD 与测试门槛

| 阶段 | 工具 | 失败条件 |
|---|---|---|
| lint | ruff 0.17 | 规则或格式差异 |
| typecheck（可选） | mypy 2.4 | 先覆盖 00-framework，稳定后改为阻断 |
| unit | pytest（SQLite） | 失败 |
| integration | pytest + PostgreSQL/Redis 服务容器 | 失败 |
| migration | alembic：upgrade head → check → downgrade -1 → upgrade；单 head；expand/contract 检查 | 任一步失败、多 head、未标记的破坏性迁移 |
| contract | Schemathesis 4.32 对 OpenAPI | 5xx、响应不符 schema、未声明状态码 |
| security | pip-audit、bandit（HIGH 阻断）、trivy fs + image（SHA 固定） | 已知漏洞、密钥泄露、CRITICAL/HIGH 且有修复 |
| build | buildx + gha 缓存、SBOM、provenance | 构建失败 |
| staging | `release.sh` + k6 smoke | 健康检查超时、观察期触发、阈值失败 |
| production | GitHub environment 人工审批（仅 v* tag），生产串行不取消 | 无人批准；观察期触发自动回滚 |

k6 阈值：`http_req_failed < 0.5%`、读 p95 < 300ms / p99 < 1s、写 p95 < 800ms / p99 < 2s、`checks > 99%`、`dropped_iterations < 100`。压测只打预发，压测机 IP 加入 nginx 限流白名单。
