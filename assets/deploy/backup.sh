#!/usr/bin/env bash
# PostgreSQL 每日备份模板（references/operations.md §5、DATA-003）。由 cron 每日执行：
#   15 3 * * * /opt/luz/backup.sh >> /var/log/luz-backup.log 2>&1
# 备份加密后上传异地对象存储；每季度做恢复演练并记录耗时。云数据库已有自动备份时，以云备份为主，本脚本作补充。
set -euo pipefail

STAMP=$(date +%Y%m%d-%H%M%S)
WORK=/var/backups/luz
KEEP_DAYS=${KEEP_DAYS:-14}
mkdir -p "$WORK"

docker compose -f /opt/luz/docker-compose.prod.yml exec -T postgres \
  sh -c 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" --format=custom' > "$WORK/db-$STAMP.dump"

# 加密：BACKUP_PASSPHRASE_FILE 指向受限文件
gpg --batch --yes --pinentry-mode loopback --passphrase-file "${BACKUP_PASSPHRASE_FILE:?}" \
  --symmetric --cipher-algo AES256 -o "$WORK/db-$STAMP.dump.gpg" "$WORK/db-$STAMP.dump"
rm -f "$WORK/db-$STAMP.dump"

# 上传异地（按云厂商替换，例如 coscli / ossutil / aws s3 cp）
${UPLOAD_COMMAND:?set UPLOAD_COMMAND} "$WORK/db-$STAMP.dump.gpg"

find "$WORK" -name 'db-*.dump.gpg' -mtime +"$KEEP_DAYS" -delete
echo "backup ok $STAMP"
