#!/usr/bin/env bash
set -euo pipefail

root=/srv/grader
test -L "$root/previous"
target="$(readlink -f "$root/previous")"
test -d "$target"
ln -sfn "$target" "$root/current.next"
mv -Tf "$root/current.next" "$root/current"
sudo systemctl restart grader-api.service grader-scheduler.service
curl --fail --silent --show-error http://127.0.0.1:8101/health/ready >/dev/null
