#!/usr/bin/env bash
set -euo pipefail

repo_dir="${1:-/srv/grader/current}"
env_file="${2:-/etc/grader/grader.env}"

test -d "$repo_dir"
test -r "$env_file"
test -x /srv/grader/venv/bin/python
test -d /srv/grader-data
test "$(stat -c '%U' /srv/grader-data)" = grader

set -a
source "$env_file"
set +a

test "${GRADER_ENVIRONMENT:-}" = production
test "${GRADER_DATABASE_URL:-}" != ""
test "${GRADER_WECHAT_APP_ID:-}" != ""
test -r "${GRADER_WECHAT_PAY_PRIVATE_KEY_PATH:-/missing}"
test -r "${GRADER_WECHAT_PAY_PUBLIC_KEY_PATH:-/missing}"

cd "$repo_dir"
/srv/grader/venv/bin/python -c 'from server.config import ServerSettings; s=ServerSettings(); s.require_wechat_production_settings()'
