<!-- 模板只是骨架，不是规范：先运行 python -X utf8 scripts/context.py launch-compliance 读完规则卡；与规则卡冲突时按规则卡写。 -->
# 发布、回滚与恢复手册

复制到项目 `docs/RUNBOOK.md`，把域名、主机、联系人替换为实际值。脚本在 `/opt/luz/deploy/`：`release.sh`、`backup.sh`、`verify-restore.sh`。

## 1 发布

| 步骤 | 命令 / 动作 | 通过条件 |
|---|---|---|
| 0 准备 | CI 全绿（lint、测试、迁移检查、契约、安全扫描、镜像扫描）；预发已部署同一 digest 并通过 k6 smoke | 发布单写明版本、digest、迁移 revision、是否含 contract 迁移 |
| 1 预检 | `release.sh` 自动：镜像按 digest、磁盘余量 ≥ 20%、最近每日备份 < 26h（否则先备份） | 任一不满足即停止 |
| 2 发布前备份 | 自动：`backup.sh dump pre-release-<时间>` | 文件已加密并外传 |
| 3 迁移 | 自动：`docker compose --profile release run --rm migrate`（`alembic upgrade head` 后执行 `python -m admin_rbac.bootstrap --seed` 写入权限码与内置角色、`python -m app_settings sync` 同步文案种子） | 失败即停止，此时旧版本未被替换 |
| 4 api 滚动替换 | 自动：扩到 2 倍 → 新容器 `/readyz` 健康 → 等 15s 让 nginx 解析 → 旧容器 SIGTERM（30s）→ 删除 | 新容器不健康则删除新容器，旧版本继续服务 |
| 5 worker / scheduler | 自动：停旧起新（stop_grace_period 60s） | 心跳健康 |
| 6 冒烟 | 自动：外部 URL、各副本 `/readyz`、可选 `SMOKE_CMD`（k6 smoke / 下单→支付→回调沙箱） | 全部通过 |
| 7 观察 15 分钟 | 自动：每 30s 查 Prometheus，见 §2 触发条件 | 任一触发 → 自动回滚（`AUTO_ROLLBACK=1`） |
| 8 记录 | 自动写 `.release/history.tsv`；人工补发布记录（变更、执行人、结果） | — |

迁移规则（expand / contract）：

| 版本 | 允许 | 禁止 |
|---|---|---|
| expand（本次） | 新表、可空新列、带默认值的新列、`CREATE INDEX CONCURRENTLY`、双写 | 删列/删表/改名、新增 NOT NULL 约束、改类型 |
| contract（下一次） | 删除旧列/表、加 NOT NULL（数据已回填）；文件内写 `# luz: contract`，CI 才放行 | 与新代码同版本上线 |

大表回填用批处理任务分批执行，不放在迁移事务里。

## 2 回滚

触发条件（任一满足，`release.sh watch` 自动判断；人工发现同样适用）：

| 条件 | 阈值 |
|---|---|
| 5xx 比例 | > 2%，5 分钟窗口 |
| p95 延迟 | > 发布前基线 × 2（且 > 300ms） |
| 支付回调验签失败 | 10 分钟 > 5 次 |
| 冒烟 / 任一副本 `/readyz` | 失败 |
| 关键流程 | 下单、支付、回调、退款任一人工验证失败 |

操作：

```bash
/opt/luz/deploy/release.sh rollback              # 回到 .release/previous 记录的 digest
/opt/luz/deploy/release.sh rollback <镜像@digest> # 指定版本
```

数据库不随应用回滚（expand/contract 保证旧代码兼容新结构）。迁移本身有误时按顺序选择：

1. 前向修复：新增一个修正迁移，走正常发布。
2. `alembic downgrade <rev>`：仅限 CI 已验证 downgrade 且不丢数据的迁移，先停 worker / scheduler。
3. 恢复数据：用 §3 的发布前备份或 PITR，需要停写并公告，按演练 RTO 估算时间。

## 3 恢复

### 3.1 逻辑备份恢复（单表误删、整体回到某日）

```bash
# 1 取回并校验
sha256sum -c db-daily-<时间>.dump.gpg.sha256
gpg -o db.dump -d db-daily-<时间>.dump.gpg            # 公钥加密时在持有私钥的机器上执行
# 2 先恢复到临时库核对，不直接覆盖生产
docker compose exec -T -u postgres postgres createdb -U luz luz_restore
docker compose exec -T -u postgres postgres pg_restore -U luz -d luz_restore --no-owner -j 4 < db.dump
# 3 单表：从 luz_restore 导出所需行再导回生产；整体：停 api/worker/scheduler 后用 --clean 恢复到生产库
```

### 3.2 时间点恢复 PITR（误操作后回到某一分钟，RPO ≈ 1 分钟）

1. 确认目标时间（UTC），停止 `api`、`worker`、`scheduler`、`nginx` 对外流量（维护页）。
2. 停止 postgres，把当前数据卷改名保留（不要删除）：`docker volume create luz-app_pgdata_new`。
3. 解密目标时间之前最近的基础备份 `base-<时间>.tar.gz.gpg`，解压到新卷的 `pgdata/` 目录，属主 999:999、权限 700。
4. 准备 WAL：本地 `/var/backups/luz/wal/` 或从异地取回 `wal/*.gpg` 解密到同一目录。
5. 在 `pgdata/postgresql.auto.conf` 追加：
   ```text
   restore_command = 'cp /wal-archive/%f %p'
   recovery_target_time = '2026-10-28 08:15:00+00'
   recovery_target_action = 'promote'
   ```
   并创建空文件 `pgdata/recovery.signal`。
6. compose 中 pgdata 卷指向新卷，启动 postgres，日志出现 `archive recovery complete` 后核对数据。
7. 删除 `restore_command`/`recovery_target_*` 设置；立即执行一次 `backup.sh base`（新时间线）。
8. 依次启动 api → worker → scheduler → nginx；观察 §2 指标 15 分钟；写事故记录。

### 3.3 Redis

Redis 只承载锁、限流、幂等与短期状态：丢失后重启即可，业务状态以 PostgreSQL 为准。若项目在 Redis 存放了不可重建的业务数据，按 RDB 每日外传 + AOF everysec 处理，并在此补充恢复步骤。

## 4 演练与留存

| 项 | 频率 | 证据 |
|---|---|---|
| 自动恢复校验 `verify-restore.sh` | 每周 | 指标 `luz_backup_last_success_timestamp_seconds{kind="restore_test"}`、日志中的耗时 |
| 人工恢复演练（§3.1 + §3.2 各一次） | 每季度 | 演练记录：日期、备份文件、目标时间、RTO、RPO、问题与改进 |
| 回滚演练（预发 `release.sh rollback`） | 每次大版本前 | 发布记录 |
| 限流演练（预发 `limit_req_dry_run` → 正式） | 上线前、调整阈值后 | nginx 日志 `limit_req` 字段统计 |
| 日志留存 | 持续 | nginx 访问日志、应用认证/权限/支付回调/管理操作日志集中存储不少于 6 个月（交易信息 3 年），存储开启只读或对象锁，到期自动删除（OBS-004，`references/config-logging.md` §3.9） |
| 备份留存 | 持续 | 每日逻辑备份与基础备份 30 天；WAL 覆盖最老基础备份之后；异地桶开启版本与对象锁 |

## 5 改运行配置与回滚

运行参数（超时、请求体与上传上限、限流阈值、熔断、认证时间窗、功能开关、最低可用版本、日志级别）写在代码仓库 `backend/config/runtime/`：`base.toml` 是全部键与默认值，`prod.toml` 只写生产与默认不同的值。服务器上的副本在 `/opt/luz/config/runtime/`，整目录只读挂载进 api、worker、scheduler，各进程监听目录并热加载（防抖 1.5 秒），不重建镜像、不重启。秘密与部署参数仍在 `/etc/luz/api.env`，改了要重启。规范见 `references/config-logging.md` §2。

| 步骤 | 命令 / 动作 | 通过条件 |
|---|---|---|
| 1 改文件 | 在仓库改 `prod.toml`（或 `base.toml`）；提交说明写原因。限流、上传上限、维护模式、认证这类 risk = high 的键先写 `[overrides.<名称>]` 只覆盖几家门店、一个端或一部分用户 | — |
| 2 评审与校验 | CI 运行 `python -X utf8 -m luzback.config_check --env all`（每个环境文件按字段说明加载一遍）与秘密扫描；risk = high 的键要第二人评审 | 每个环境 `ok version=<12 位>`；记下 prod 的版本号 |
| 3 同步 | 发布机把三个 `.toml` 与 `COMMIT`（写入 `git rev-parse HEAD`）同步到 `/opt/luz/config/runtime/`：先写到同目录的临时文件再 `mv` 改名，或 `rsync --delay-updates`；只替换目录里的文件，不替换目录本身（目录挂载跟着原目录） | 服务器上 `python3 -c 'import tomllib,sys;[tomllib.load(open(f,"rb")) for f in sys.argv[1:]]' /opt/luz/config/runtime/*.toml` 不报错 |
| 4 生效 | 各进程监听到变化后自动加载。要立刻生效或怀疑监听没起作用时手动触发：api 每个副本 `docker compose exec --index <n> api python -c "import os,urllib.request as u; print(u.urlopen(u.Request('http://127.0.0.1:8000/internal/config/reload', method='POST', headers={'Authorization': 'Bearer ' + os.environ['LUZ_OPS_TOKEN']})).read().decode())"`；worker、scheduler `docker compose kill -s HUP worker scheduler` | 返回 `result` 为 `loaded` 或 `unchanged`；`rejected` 时返回每条错误（键、当前值、原因、允许范围），旧配置继续生效，修好文件重来 |
| 5 核对版本 | api 各副本 `GET /internal/config`（同上方式带运维令牌）；全部实例查 `config_loads` 每个实例最新一条：`SELECT DISTINCT ON (instance_id) instance_id, config_version, result, restart_keys_json, created_at FROM config_loads ORDER BY instance_id, created_at DESC;` | 每个实例的 `config_version` 都等于第 2 步记下的版本；`restart_pending` 非空的键要滚动重启后才生效（`release.sh deploy` 同一镜像） |
| 6 观察 | 灰度覆盖期间看错误率、延迟与相关业务指标（§2 阈值）；`config.reload_rejected` 与各实例版本不一致超过 5 分钟会告警 | 正常则把值写进 `prod.toml` 主体并删掉覆盖，再走一遍 1–5 |

回滚：在仓库还原那次配置提交（`git revert <提交>`），走同样的 2–5 步；各实例热加载后回到旧版本号。服务器上手工改的文件也要补提交，否则下次同步会被覆盖。要重启才生效的键（`[db] statement_timeout_ms`、`[logging] queue_capacity`、`[breaker]`）改动或回滚后都要滚动重启。
