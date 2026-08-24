#!/usr/bin/env bash
set -euo pipefail

failures=()
curl --fail --silent --max-time 10 http://127.0.0.1:8101/health/ready >/dev/null \
    || failures+=(api_not_ready)
systemctl is-active --quiet grader-scheduler.service \
    || failures+=(scheduler_not_running)
test -f /var/lib/grader-backup/last-success \
    || failures+=(backup_missing)
if test -f /var/lib/grader-backup/last-success; then
    age="$(( $(date +%s) - $(stat -c %Y /var/lib/grader-backup/last-success) ))"
    test "$age" -le 93600 || failures+=(backup_stale)
fi
disk_percent="$(df --output=pcent /srv/grader-data | tail -1 | tr -dc '0-9')"
test "$disk_percent" -lt 85 || failures+=(disk_high)

if test -n "${MONITOR_MYSQL_DATABASE:-}"; then
    mysql_args=(
        --batch --skip-column-names
        --host="${MONITOR_MYSQL_HOST:-127.0.0.1}"
        --user="${MONITOR_MYSQL_USER:?}"
        "$MONITOR_MYSQL_DATABASE"
    )
    worker_exceptions="$(MYSQL_PWD="${MONITOR_MYSQL_PASSWORD:?}" mysql "${mysql_args[@]}" -e "SELECT COUNT(*) FROM grading_jobs WHERE state='worker_exception'")"
    queued_jobs="$(MYSQL_PWD="$MONITOR_MYSQL_PASSWORD" mysql "${mysql_args[@]}" -e "SELECT COUNT(*) FROM grading_jobs WHERE state='queued'")"
    online_workers="$(MYSQL_PWD="$MONITOR_MYSQL_PASSWORD" mysql "${mysql_args[@]}" -e "SELECT COUNT(*) FROM workers WHERE status='online'")"
    oldest_wait="$(MYSQL_PWD="$MONITOR_MYSQL_PASSWORD" mysql "${mysql_args[@]}" -e "SELECT COALESCE(MAX(TIMESTAMPDIFF(SECOND, queued_at, UTC_TIMESTAMP())),0) FROM grading_jobs WHERE state='queued'")"
    test "$worker_exceptions" -eq 0 || failures+=(worker_exception)
    if test "$queued_jobs" -gt 0; then
        test "$online_workers" -gt 0 || failures+=(no_online_worker)
        test "$oldest_wait" -le "${MAX_QUEUE_WAIT_SECONDS:-7200}" || failures+=(queue_stale)
    fi
fi

if test "${#failures[@]}" -gt 0; then
    message="grader alert: ${failures[*]}"
    logger -p daemon.err "$message"
    if test -n "${ALERT_WEBHOOK_URL:-}"; then
        curl --fail --silent --max-time 10 \
            -H 'Content-Type: application/json' \
            --data "{\"text\":\"$message\"}" \
            "$ALERT_WEBHOOK_URL" >/dev/null || true
    fi
    exit 1
fi
