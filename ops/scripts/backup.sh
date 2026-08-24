#!/usr/bin/env bash
set -euo pipefail

: "${BACKUP_MYSQL_HOST:?}"
: "${BACKUP_MYSQL_USER:?}"
: "${BACKUP_MYSQL_PASSWORD:?}"
: "${BACKUP_MYSQL_DATABASE:?}"
: "${BACKUP_ENCRYPTION_PASSWORD_FILE:?}"
: "${BACKUP_COS_DESTINATION:?}"

backup_root=/var/lib/grader-backup
stamp="$(date -u +%Y%m%dT%H%M%SZ)"
install -d -m 0750 "$backup_root"
work_dir="$(mktemp -d "$backup_root/work.XXXXXX")"
trap 'rm -rf "$work_dir"' EXIT

MYSQL_PWD="$BACKUP_MYSQL_PASSWORD" mysqldump \
    --host="$BACKUP_MYSQL_HOST" \
    --user="$BACKUP_MYSQL_USER" \
    --single-transaction --routines --events --triggers \
    "$BACKUP_MYSQL_DATABASE" >"$work_dir/mysql.sql"
tar --create --file="$work_dir/grader-data.tar" -C /srv grader-data
tar --create --gzip --file="$work_dir/backup.tar.gz" -C "$work_dir" mysql.sql grader-data.tar
openssl enc -aes-256-cbc -salt -pbkdf2 \
    -pass "file:$BACKUP_ENCRYPTION_PASSWORD_FILE" \
    -in "$work_dir/backup.tar.gz" \
    -out "$work_dir/grader-$stamp.tar.gz.enc"

coscmd upload "$work_dir/grader-$stamp.tar.gz.enc" "$BACKUP_COS_DESTINATION/daily/"
if test "$(date -u +%u)" = 7; then
    coscmd upload "$work_dir/grader-$stamp.tar.gz.enc" "$BACKUP_COS_DESTINATION/weekly/"
fi
touch /var/lib/grader-backup/last-success
