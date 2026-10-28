#!/usr/bin/env bash
# Luz PostgreSQL 备份脚本（references/operations.md §5、DATA-002、DATA-003）。
# 覆盖：每周基础备份（PITR 起点）+ WAL 持续归档外传（RPO ≈ 1 分钟）+ 每日 pg_dump 逻辑备份 + 全局对象（角色），
#      加密、sha256 校验、异地上传钩子、30 天保留、node_exporter textfile 新鲜度指标。
# 云数据库（RDS/TencentDB 等）已开启自动备份与 PITR 时，以云备份为主，本脚本的 dump 作为跨厂商的逻辑副本。
#
# 用法（宿主机 root 的 crontab；时间按业务低峰调整）：
#   */5 * * * *  /opt/luz/deploy/backup.sh wal-sync   >> /var/log/luz/backup.log 2>&1
#   15 3 * * *   /opt/luz/deploy/backup.sh daily      >> /var/log/luz/backup.log 2>&1   # dump + globals + prune
#   45 3 * * 0   /opt/luz/deploy/backup.sh base       >> /var/log/luz/backup.log 2>&1   # 每周基础备份
#   30 4 * * 1   /opt/luz/deploy/verify-restore.sh    >> /var/log/luz/backup.log 2>&1   # 每周自动恢复校验
#   发布前：     /opt/luz/deploy/backup.sh dump pre-release-<version>                  # release.sh 自动调用
#
# 配置（/etc/luz/backup.env，600，root）：
#   BACKUP_GPG_RECIPIENT  首选：公钥加密，私钥离线保存（服务器被攻破也解不开备份）
#   BACKUP_PASSPHRASE_FILE 备选：对称加密口令文件（两者至少一个）
#   UPLOAD_COMMAND        异地上传钩子，调用方式：$UPLOAD_COMMAND <本地文件> <远端相对路径>
#                         例：/opt/luz/deploy/hooks/upload-cos.sh（内部用 coscli/ossutil/aws s3 cp，目标桶开启版本与对象锁）
#   REMOTE_PRUNE_COMMAND  可选：远端保留清理钩子 $REMOTE_PRUNE_COMMAND <保留天数>（或直接用桶生命周期规则）
#   KEEP_DAYS             本地与远端保留天数，默认 30
set -Eeuo pipefail
umask 077

ENV_FILE=${BACKUP_ENV_FILE:-/etc/luz/backup.env}
# shellcheck disable=SC1090
[ -f "$ENV_FILE" ] && . "$ENV_FILE"

COMPOSE_FILE=${COMPOSE_FILE:-/opt/luz/deploy/docker-compose.prod.yml}
COMPOSE=(docker compose -f "$COMPOSE_FILE")
ROOT=${BACKUP_ROOT:-/var/backups/luz}
WAL_DIR=${WAL_DIR:-$ROOT/wal}                 # postgres archive_command 写入的目录（容器内 /wal-archive）
TEXTFILE_DIR=${TEXTFILE_DIR:-/var/lib/node_exporter/textfile}
KEEP_DAYS=${KEEP_DAYS:-30}
HOST_TAG=${HOST_TAG:-$(hostname -s)}
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
LOCK_FILE=${LOCK_FILE:-/run/lock/luz-backup.lock}

log() { printf '%s backup[%s] %s\n' "$(date -u +%FT%TZ)" "$CMD" "$*"; }
die() { log "ERROR: $*"; exit 1; }

# ---------------------------------------------------------------- 指标（node_exporter textfile，原子替换）
write_metric() {   # kind status size duration
    local kind=$1 status=$2 size=${3:-0} duration=${4:-0} f now
    mkdir -p "$TEXTFILE_DIR"
    f="$TEXTFILE_DIR/luz_backup_${kind}.prom"
    now=$(date +%s)
    local prev_ok=0
    [ -f "$f" ] && prev_ok=$(awk '/^luz_backup_last_success_timestamp_seconds/{print $2}' "$f" | head -1)
    {
        echo "# HELP luz_backup_last_success_timestamp_seconds Unix time of the last successful backup step."
        echo "# TYPE luz_backup_last_success_timestamp_seconds gauge"
        if [ "$status" = ok ]; then
            echo "luz_backup_last_success_timestamp_seconds{kind=\"$kind\"} $now"
        else
            echo "luz_backup_last_success_timestamp_seconds{kind=\"$kind\"} ${prev_ok:-0}"
        fi
        echo "# TYPE luz_backup_last_run_timestamp_seconds gauge"
        echo "luz_backup_last_run_timestamp_seconds{kind=\"$kind\"} $now"
        echo "# TYPE luz_backup_last_status gauge"
        echo "luz_backup_last_status{kind=\"$kind\"} $([ "$status" = ok ] && echo 1 || echo 0)"
        echo "# TYPE luz_backup_last_size_bytes gauge"
        echo "luz_backup_last_size_bytes{kind=\"$kind\"} $size"
        echo "# TYPE luz_backup_last_duration_seconds gauge"
        echo "luz_backup_last_duration_seconds{kind=\"$kind\"} $duration"
    } > "$f.$$"
    chmod 644 "$f.$$"
    mv -f "$f.$$" "$f"
}

CURRENT_KIND=""
on_error() {
    local rc=$?
    [ -n "$CURRENT_KIND" ] && write_metric "$CURRENT_KIND" fail 0 0 || true
    log "FAILED (exit $rc) at line ${BASH_LINENO[0]}"
    exit "$rc"
}
trap on_error ERR

# ---------------------------------------------------------------- 加密、校验、外传
encrypt() {   # in out
    if [ -n "${BACKUP_GPG_RECIPIENT:-}" ]; then
        gpg --batch --yes --trust-model always --recipient "$BACKUP_GPG_RECIPIENT" \
            --cipher-algo AES256 --compress-algo none --encrypt -o "$2" "$1"
    elif [ -n "${BACKUP_PASSPHRASE_FILE:-}" ]; then
        gpg --batch --yes --pinentry-mode loopback --passphrase-file "$BACKUP_PASSPHRASE_FILE" \
            --symmetric --cipher-algo AES256 --compress-algo none -o "$2" "$1"
    else
        die "set BACKUP_GPG_RECIPIENT or BACKUP_PASSPHRASE_FILE"
    fi
}

seal() {   # 明文文件 → .gpg + .sha256，删除明文；输出加密文件路径
    local plain=$1 enc="$1.gpg"
    encrypt "$plain" "$enc"
    shred -u "$plain" 2>/dev/null || rm -f "$plain"
    (cd "$(dirname "$enc")" && sha256sum "$(basename "$enc")" > "$(basename "$enc").sha256")
    echo "$enc"
}

offsite() {   # 本地文件 远端路径（同时上传 .sha256）
    [ -n "${UPLOAD_COMMAND:-}" ] || die "UPLOAD_COMMAND not set: backups must leave this host"
    "$UPLOAD_COMMAND" "$1" "$2"
    [ -f "$1.sha256" ] && "$UPLOAD_COMMAND" "$1.sha256" "$2.sha256"
}

pg_exec() {   # 在 postgres 容器内以 postgres 用户执行（本地 socket，官方镜像 local 连接为 trust）
    "${COMPOSE[@]}" exec -T -u postgres postgres "$@"
}

# ---------------------------------------------------------------- 每日逻辑备份 + 全局对象
do_dump() {
    local label=${1:-daily} dir="$ROOT/dump" start size f g
    CURRENT_KIND=pg_dump
    [ "$label" = daily ] || CURRENT_KIND=""          # 发布前快照不覆盖每日新鲜度指标
    start=$(date +%s)
    mkdir -p "$dir"
    f="$dir/db-$label-$STAMP.dump"
    g="$dir/globals-$label-$STAMP.sql"
    # -Fc：自定义格式，可选择性恢复；pg_dump 在线一致性快照，不阻塞读写
    pg_exec sh -c 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" --format=custom --no-password' > "$f"
    # 角色、表空间等全局对象 pg_dump 不包含，需单独备份（不导出口令哈希可加 --no-role-passwords）
    pg_exec sh -c 'pg_dumpall -U "$POSTGRES_USER" --globals-only --no-password' > "$g"
    # 试读归档目录，确认文件完整可解析
    pg_exec pg_restore --list < "$f" > /dev/null
    [ -s "$f" ] || die "empty dump"
    f=$(seal "$f"); g=$(seal "$g")
    offsite "$f" "$HOST_TAG/dump/$(basename "$f")"
    offsite "$g" "$HOST_TAG/dump/$(basename "$g")"
    size=$(stat -c %s "$f")
    [ -n "$CURRENT_KIND" ] && write_metric pg_dump ok "$size" "$(( $(date +%s) - start ))"
    CURRENT_KIND=""
    log "dump ok label=$label file=$(basename "$f") size=$size"
}

# ---------------------------------------------------------------- 每周基础备份（PITR 起点）
do_base() {
    local dir="$ROOT/base" start size f
    CURRENT_KIND=basebackup
    start=$(date +%s)
    mkdir -p "$dir"
    f="$dir/base-$STAMP.tar.gz"
    # -Ft -D -：tar 输出到 stdout（单表空间时可用）；-X fetch 把备份期间所需 WAL 一并打包，备份可独立恢复
    pg_exec sh -c 'pg_basebackup -U "$POSTGRES_USER" -D - -Ft -X fetch --checkpoint=fast --label="luz-$0" --no-password' "$STAMP" | gzip -6 > "$f"
    gzip -t "$f"
    f=$(seal "$f")
    offsite "$f" "$HOST_TAG/base/$(basename "$f")"
    size=$(stat -c %s "$f")
    write_metric basebackup ok "$size" "$(( $(date +%s) - start ))"
    CURRENT_KIND=""
    log "basebackup ok file=$(basename "$f") size=$size"
}

# ---------------------------------------------------------------- WAL 外传（archive_command 已写入本地 WAL_DIR）
do_wal_sync() {
    local shipped="$WAL_DIR/.shipped" n=0 start enc
    CURRENT_KIND=wal_offsite
    start=$(date +%s)
    mkdir -p "$shipped" "$ROOT/wal-enc"
    # 只处理已完成的段（不含 .part），按名称顺序外传
    while IFS= read -r -d '' seg; do
        local name; name=$(basename "$seg")
        [ -e "$shipped/$name" ] && continue
        cp "$seg" "$ROOT/wal-enc/$name"
        enc=$(seal "$ROOT/wal-enc/$name")
        offsite "$enc" "$HOST_TAG/wal/$(basename "$enc")"
        rm -f "$enc" "$enc.sha256"
        : > "$shipped/$name"
        n=$((n + 1))
    done < <(find "$WAL_DIR" -maxdepth 1 -type f ! -name '*.part' -print0 | sort -z)
    write_metric wal_offsite ok 0 "$(( $(date +%s) - start ))"
    CURRENT_KIND=""
    log "wal-sync ok shipped=$n"
}

# ---------------------------------------------------------------- 保留清理
do_prune() {
    # 逻辑备份与基础备份：保留 KEEP_DAYS 天（发布前快照同样适用）
    find "$ROOT/dump" "$ROOT/base" -type f \( -name '*.gpg' -o -name '*.sha256' \) -mtime +"$KEEP_DAYS" -print -delete 2>/dev/null || true
    # WAL：只删除早于“仍保留的最老基础备份”且已外传的段，保证保留期内任一时刻都可 PITR
    local oldest_base
    oldest_base=$(find "$ROOT/base" -type f -name 'base-*.gpg' -printf '%T@ %p\n' 2>/dev/null | sort -n | head -1 | cut -d' ' -f2-)
    if [ -n "$oldest_base" ]; then
        find "$WAL_DIR" -maxdepth 1 -type f ! -name '*.part' ! -newer "$oldest_base" -print0 |
        while IFS= read -r -d '' seg; do
            [ -e "$WAL_DIR/.shipped/$(basename "$seg")" ] && rm -f "$seg" "$WAL_DIR/.shipped/$(basename "$seg")"
        done
    fi
    if [ -n "${REMOTE_PRUNE_COMMAND:-}" ]; then "$REMOTE_PRUNE_COMMAND" "$KEEP_DAYS"; fi
    log "prune ok keep_days=$KEEP_DAYS"
}

# ---------------------------------------------------------------- 入口
CMD=${1:-daily}
mkdir -p "$ROOT" "$(dirname "$LOCK_FILE")"
exec 9>"$LOCK_FILE"
if [ "$CMD" = wal-sync ]; then
    flock -n 9 || { echo "another backup is running; skip wal-sync"; exit 0; }
else
    flock -w 1800 9 || die "lock timeout"
fi

case "$CMD" in
    dump)      do_dump "${2:-daily}" ;;
    base)      do_base ;;
    wal-sync)  do_wal_sync ;;
    prune)     do_prune ;;
    daily)     do_dump daily; do_wal_sync; do_prune ;;
    *) echo "usage: $0 {daily|dump [label]|base|wal-sync|prune}" >&2; exit 2 ;;
esac
