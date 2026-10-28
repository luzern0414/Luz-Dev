# 发布、回滚与恢复手册

复制到项目 `docs/RUNBOOK.md`，把域名、主机、联系人替换为实际值。脚本在 `/opt/luz/deploy/`：`release.sh`、`backup.sh`、`verify-restore.sh`。

## 1 发布

| 步骤 | 命令 / 动作 | 通过条件 |
|---|---|---|
| 0 准备 | CI 全绿（lint、测试、迁移检查、契约、安全扫描、镜像扫描）；预发已部署同一 digest 并通过 k6 smoke | 发布单写明版本、digest、迁移 revision、是否含 contract 迁移 |
| 1 预检 | `release.sh` 自动：镜像按 digest、磁盘余量 ≥ 20%、最近每日备份 < 26h（否则先备份） | 任一不满足即停止 |
| 2 发布前备份 | 自动：`backup.sh dump pre-release-<时间>` | 文件已加密并外传 |
| 3 迁移 | 自动：`docker compose --profile release run --rm migrate` | 失败即停止，此时旧版本未被替换 |
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
| 日志留存 | 持续 | nginx 访问日志、应用认证/权限/支付回调/管理操作日志集中存储 ≥ 180 天，存储开启只读或对象锁（网络安全法） |
| 备份留存 | 持续 | 每日逻辑备份与基础备份 30 天；WAL 覆盖最老基础备份之后；异地桶开启版本与对象锁 |
