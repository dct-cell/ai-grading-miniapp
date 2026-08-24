#!/usr/bin/env bash
set -euo pipefail

release_dir="${1:?usage: deploy.sh /srv/grader/releases/<release>}"
root=/srv/grader
current="$root/current"
previous="$root/previous"

test -d "$release_dir"
test "$release_dir" != "$root"
"$release_dir/ops/scripts/verify-environment.sh" "$release_dir" /etc/grader/grader.env

cd "$release_dir"
/srv/grader/venv/bin/alembic upgrade head

if test -L "$current"; then
    ln -sfn "$(readlink -f "$current")" "$previous"
fi
ln -sfn "$release_dir" "$current.next"
mv -Tf "$current.next" "$current"

sudo systemctl restart grader-api.service grader-scheduler.service
curl --fail --silent --show-error http://127.0.0.1:8101/health/ready >/dev/null
