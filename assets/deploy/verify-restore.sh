#!/usr/bin/env bash
# 备份恢复校验（DATA-003）：把最近一份 pg_dump 恢复到一次性的隔离容器，跑完整性检查，记录耗时（RTO 证据）。
# 每周由 cron 执行；季度演练时手动执行并把输出贴进演练记录（RUNBOOK.md §4）。
#
#   /opt/luz/deploy/verify-restore.sh                 # 最近一份本地加密 dump
#   /opt/luz/deploy/verify-restore.sh <file.dump.gpg> # 指定文件（例如从异地下载回来的副本，验证异地链路）
#
# 结果：成功写入 node_exporter 指标 luz_backup_last_success_timestamp_seconds{kind="restore_test"}，
#      告警规则 BackupRestoreTestStale 在 8 天无成功时提醒。
set -Eeuo pipefail
umask 077

ENV_FILE=${BACKUP_ENV_FILE:-/etc/luz/backup.env}
# shellcheck disable=SC1090
[ -f "$ENV_FILE" ] && . "$ENV_FILE"

ROOT=${BACKUP_ROOT:-/var/backups/luz}
TEXTFILE_DIR=${TEXTFILE_DIR:-/var/lib/node_exporter/textfile}
# 与生产同一镜像（同大版本、同扩展），否则恢复结果不代表生产
RESTORE_IMAGE=${RESTORE_IMAGE:-${POSTGRES_IMAGE:-pgvector/pgvector:0.8.1-pg16}}
# 恢复后必须存在的表（逗号分隔）；按项目补充核心业务表
REQUIRED_TABLES=${REQUIRED_TABLES:-alembic_version,outbox_events,scheduler_runs}
# 可选：额外的校验 SQL，结果必须为 t（例：select count(*) > 0 from orders）
CHECK_SQL=${CHECK_SQL:-}

NAME="luz-restore-test-$$"
WORK=$(mktemp -d "${TMPDIR:-/tmp}/luz-restore.XXXXXX")
log() { printf '%s verify-restore %s\n' "$(date -u +%FT%TZ)" "$*"; }
cleanup() { docker rm -f "$NAME" >/dev/null 2>&1 || true; rm -rf "$WORK"; }
trap cleanup EXIT

metric() {   # status duration
    local f="$TEXTFILE_DIR/luz_backup_restore_test.prom" now prev=0
    now=$(date +%s)
    [ -f "$f" ] && prev=$(awk '/^luz_backup_last_success_timestamp_seconds/{print $2}' "$f" | head -1)
    mkdir -p "$TEXTFILE_DIR"
    {
        echo "# TYPE luz_backup_last_success_timestamp_seconds gauge"
        echo "luz_backup_last_success_timestamp_seconds{kind=\"restore_test\"} $([ "$1" = ok ] && echo "$now" || echo "${prev:-0}")"
        echo "# TYPE luz_backup_last_status gauge"
        echo "luz_backup_last_status{kind=\"restore_test\"} $([ "$1" = ok ] && echo 1 || echo 0)"
        echo "# TYPE luz_backup_restore_duration_seconds gauge"
        echo "luz_backup_restore_duration_seconds $2"
    } > "$f.$$"
    chmod 644 "$f.$$"; mv -f "$f.$$" "$f"
}
trap 'rc=$?; metric fail 0; log "FAILED (exit $rc)"; exit $rc' ERR

decrypt() {   # in out
    if [ -n "${BACKUP_PASSPHRASE_FILE:-}" ] && [ -z "${BACKUP_GPG_RECIPIENT:-}" ]; then
        gpg --batch --yes --pinentry-mode loopback --passphrase-file "$BACKUP_PASSPHRASE_FILE" -o "$2" -d "$1"
    else
        # 公钥加密时，解密私钥只在恢复校验机/保管人处；生产主机上通常没有，应在独立校验机运行本脚本
        gpg --batch --yes -o "$2" -d "$1"
    fi
}

START=$(date +%s)
SRC=${1:-$(find "$ROOT/dump" -type f -name 'db-daily-*.dump.gpg' -printf '%T@ %p\n' | sort -n | tail -1 | cut -d' ' -f2-)}
[ -n "$SRC" ] && [ -f "$SRC" ] || { log "no dump found under $ROOT/dump"; exit 1; }
log "source $(basename "$SRC")"

# 1. 校验和
if [ -f "$SRC.sha256" ]; then (cd "$(dirname "$SRC")" && sha256sum -c --quiet "$(basename "$SRC").sha256"); else log "WARN no .sha256 next to source"; fi

# 2. 解密
decrypt "$SRC" "$WORK/db.dump"
GLOBALS="${SRC/\/db-/\/globals-}"; GLOBALS="${GLOBALS%.dump.gpg}.sql.gpg"
[ -f "$GLOBALS" ] && decrypt "$GLOBALS" "$WORK/globals.sql" || true

# 3. 一次性隔离容器（无网络、无持久卷）
docker run -d --name "$NAME" --network none \
    -e POSTGRES_PASSWORD=restore-test -e POSTGRES_USER=postgres -e POSTGRES_DB=restore \
    --tmpfs /var/lib/postgresql/data:size=${RESTORE_TMPFS:-8g} \
    -v "$WORK:/restore:ro" "$RESTORE_IMAGE" >/dev/null
for _ in $(seq 1 60); do
    docker exec "$NAME" pg_isready -q -U postgres -d restore && break
    sleep 2
done
docker exec "$NAME" pg_isready -q -U postgres -d restore

# 4. 恢复：全局对象（已存在的角色报错可忽略）→ 数据
[ -f "$WORK/globals.sql" ] && docker exec "$NAME" psql -q -U postgres -d postgres -f /restore/globals.sql >/dev/null 2>&1 || true
docker exec "$NAME" pg_restore -U postgres -d restore --no-owner --no-acl --exit-on-error -j 4 /restore/db.dump

# 5. 完整性检查
IFS=',' read -r -a tables <<< "$REQUIRED_TABLES"
for t in "${tables[@]}"; do
    ok=$(docker exec "$NAME" psql -U postgres -d restore -tAc "select to_regclass('public.$t') is not null")
    [ "$ok" = t ] || { log "missing table $t"; false; }
done
docker exec "$NAME" psql -U postgres -d restore -tAc "select 'alembic revision: ' || version_num from alembic_version" || true
docker exec "$NAME" psql -U postgres -d restore -tAc \
  "select 'tables=' || count(*) from information_schema.tables where table_schema='public'"
if [ -n "$CHECK_SQL" ]; then
    ok=$(docker exec "$NAME" psql -U postgres -d restore -tAc "$CHECK_SQL")
    [ "$ok" = t ] || { log "CHECK_SQL returned '$ok'"; false; }
fi

DURATION=$(( $(date +%s) - START ))
metric ok "$DURATION"
log "restore test ok duration=${DURATION}s (record as RTO evidence)"
