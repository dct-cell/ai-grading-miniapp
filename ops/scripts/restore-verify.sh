#!/usr/bin/env bash
set -euo pipefail

encrypted_backup="${1:?usage: restore-verify.sh BACKUP.enc TEMP_DATABASE}"
temporary_database="${2:?usage: restore-verify.sh BACKUP.enc TEMP_DATABASE}"
[[ "$temporary_database" =~ ^[A-Za-z0-9_]+$ ]]
: "${BACKUP_ENCRYPTION_PASSWORD_FILE:?}"
: "${BACKUP_MYSQL_HOST:?}"
: "${BACKUP_MYSQL_USER:?}"
: "${BACKUP_MYSQL_PASSWORD:?}"

work_dir="$(mktemp -d /var/lib/grader-backup/restore.XXXXXX)"
trap 'rm -rf "$work_dir"' EXIT
openssl enc -d -aes-256-cbc -pbkdf2 \
    -pass "file:$BACKUP_ENCRYPTION_PASSWORD_FILE" \
    -in "$encrypted_backup" -out "$work_dir/backup.tar.gz"
tar --extract --gzip --file="$work_dir/backup.tar.gz" -C "$work_dir"
MYSQL_PWD="$BACKUP_MYSQL_PASSWORD" mysql \
    --host="$BACKUP_MYSQL_HOST" --user="$BACKUP_MYSQL_USER" \
    -e "CREATE DATABASE IF NOT EXISTS \`$temporary_database\`"
MYSQL_PWD="$BACKUP_MYSQL_PASSWORD" mysql \
    --host="$BACKUP_MYSQL_HOST" --user="$BACKUP_MYSQL_USER" \
    "$temporary_database" <"$work_dir/mysql.sql"
MYSQL_PWD="$BACKUP_MYSQL_PASSWORD" mysql \
    --host="$BACKUP_MYSQL_HOST" --user="$BACKUP_MYSQL_USER" \
    --batch --skip-column-names "$temporary_database" \
    -e "SELECT COUNT(*) >= 0 FROM orders"
tar --list --file="$work_dir/grader-data.tar" >/dev/null
