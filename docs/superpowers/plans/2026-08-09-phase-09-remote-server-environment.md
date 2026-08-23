# Phase 09 Remote Server Environment Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Bootstrap the verified Tencent Cloud Ubuntu host as an isolated staging/production application and MySQL server, expose staging only through an SSH tunnel until a domain is ready, and leave production installed but stopped.

**Architecture:** All remote operations use the key-only alias `ssh grader-prod`. Two Unix users, two data roots, two environment files, two loopback API ports and two local-only MySQL users isolate staging from production; UFW and the Tencent security group expose SSH only before domain cutover. Tasks 1–4 may run on the empty host immediately, Task 5 waits for a runnable Phase 01 artifact, and Task 7 waits for the real domain and credentials.

**Tech Stack:** Ubuntu 24.04 LTS, OpenSSH, UFW, Python 3.12 virtual environments, MySQL 8.0, systemd, Nginx, restic, Bash, pytest

---

> **Current status:** Planning only. The remote host was inspected with read-only commands; none of the installation, upload, firewall, database, service or cutover commands below has been executed.

## Verified target and boundaries

- SSH alias `grader-prod`; remote identity `ubuntu`; public address `119.45.4.159`.
- 2 vCPU, 7.4 GiB RAM, 1.9 GiB swap and a 100 GB ext4 root disk.
- Effective SSH policy is key-only with password and root login disabled.
- Before domain cutover, only TCP 22 is public. Staging binds `127.0.0.1:8101`; production reserves `127.0.0.1:8102` but remains stopped.
- Never replace the alias with password login, `sshpass`, copied private-key material or an inline raw-IP login.
- Do not install Codex CLI, XeLaTeX, Worker runtime, WeChat credentials or private SSH keys on this host.

### Task 1: Add a read-only host inventory and connection guard

**Files:**
- Create: `ops/remote/inventory.sh`
- Create: `ops/remote/grader-prod.env.example`
- Create: `docs/operations/grader-prod-baseline.md`
- Test: `tests/ops/test_remote_inventory.py`

- [ ] **Step 1: Write the failing safety test**

```python
from pathlib import Path


def test_inventory_uses_alias_and_is_read_only() -> None:
    script = Path("ops/remote/inventory.sh").read_text()
    assert 'SSH_ALIAS="${1:-grader-prod}"' in script
    assert "BatchMode=yes" in script
    assert "StrictHostKeyChecking=yes" in script
    for forbidden in ("apt-get", "systemctl restart", "ufw ", "sed -i", "tee "):
        assert forbidden not in script


def test_example_contains_no_credentials() -> None:
    example = Path("ops/remote/grader-prod.env.example").read_text()
    assert "GRADER_SSH_ALIAS=grader-prod" in example
    assert "GRADER_PUBLIC_IP=119.45.4.159" in example
    assert "PRIVATE KEY" not in example
    assert "PASSWORD=" not in example
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `.venv/bin/python -m pytest tests/ops/test_remote_inventory.py -q`

Expected: FAIL because the files do not exist.

- [ ] **Step 3: Implement `inventory.sh`**

```bash
#!/usr/bin/env bash
set -euo pipefail
SSH_ALIAS="${1:-grader-prod}"
exec ssh -o BatchMode=yes -o ConnectTimeout=10 -o StrictHostKeyChecking=yes "$SSH_ALIAS" '
set -eu
id
hostnamectl
cat /etc/os-release
uname -a
lscpu
free -h
lsblk -o NAME,SIZE,FSTYPE,MOUNTPOINTS
df -hT /
swapon --show
timedatectl
ss -lntup || ss -lntp
sudo -n ufw status verbose
sudo -n sshd -T | grep -E "^(passwordauthentication|pubkeyauthentication|permitrootlogin) "
python3 --version
for name in nginx mysql mysqld restic certbot; do command -v "$name" || true; done
'
```

The script prints only to stdout. The example environment file contains the alias, address, expected Ubuntu release and loopback ports, but no credential or private-key path.

- [ ] **Step 4: Record the baseline and commit**

```bash
chmod +x ops/remote/inventory.sh
ops/remote/inventory.sh grader-prod > /tmp/grader-prod-inventory.txt
.venv/bin/python -m pytest tests/ops/test_remote_inventory.py -q
git add ops/remote/inventory.sh ops/remote/grader-prod.env.example docs/operations/grader-prod-baseline.md tests/ops/test_remote_inventory.py
git commit -m "ops: inventory grader production host"
```

`grader-prod-baseline.md` records the verified facts from the design spec and the inventory command. Do not commit full `id`, interface addresses, host keys or later secrets.

### Task 2: Enable UFW without risking SSH lockout

**Files:**
- Create: `ops/remote/configure-firewall.sh`
- Create: `ops/remote/verify-ssh-boundary.sh`
- Create: `docs/operations/tencent-security-group.md`
- Test: `tests/ops/test_remote_firewall.py`

- [ ] **Step 1: Write the ordering and boundary tests**

```python
from pathlib import Path


def test_ssh_allow_precedes_enable() -> None:
    script = Path("ops/remote/configure-firewall.sh").read_text()
    assert script.index("ufw allow OpenSSH") < script.index("ufw --force enable")
    for port in ("3306", "8101", "8102", "80/tcp", "443/tcp"):
        assert f"allow {port}" not in script
    assert "sshd -t" in script
    assert "passwordauthentication no" in script
    assert "permitrootlogin no" in script
```

- [ ] **Step 2: Confirm failure**

Run: `.venv/bin/python -m pytest tests/ops/test_remote_firewall.py -q`

Expected: FAIL because the scripts are missing.

- [ ] **Step 3: Implement the guarded firewall sequence**

The root-only script must execute in this order and must not edit either existing sshd drop-in:

```bash
sshd -t
effective_sshd="$(sshd -T)"
grep -qx 'passwordauthentication no' <<<"$effective_sshd"
grep -qx 'pubkeyauthentication yes' <<<"$effective_sshd"
grep -qx 'permitrootlogin no' <<<"$effective_sshd"
ufw default deny incoming
ufw default allow outgoing
ufw allow OpenSSH
ufw --force enable
ufw status verbose
```

`verify-ssh-boundary.sh` checks the effective sshd values, active UFW, the OpenSSH rule and absence of public 3306/8101/8102 listeners.

- [ ] **Step 4: Document and execute the recovery gate**

Before execution, `docs/operations/tencent-security-group.md` requires a Tencent disk snapshot, working console/VNC recovery, security-group TCP 22 access, and closed 80/443/3306/8101/8102. Then run:

```bash
ssh -o BatchMode=yes grader-prod 'sudo -n true'
ssh grader-prod 'sudo -n bash -s' < ops/remote/configure-firewall.sh
ssh -o BatchMode=yes -o ConnectTimeout=10 grader-prod true
ops/remote/verify-ssh-boundary.sh grader-prod
```

Expected: the second SSH connection and boundary verification pass. Keep the first session open until then. On failure, use Tencent console recovery to run `sudo ufw disable` and stop the plan.

- [ ] **Step 5: Run and commit**

```bash
chmod +x ops/remote/configure-firewall.sh ops/remote/verify-ssh-boundary.sh
.venv/bin/python -m pytest tests/ops/test_remote_firewall.py -q
git add ops/remote/configure-firewall.sh ops/remote/verify-ssh-boundary.sh docs/operations/tencent-security-group.md tests/ops/test_remote_firewall.py
git commit -m "ops: guard remote ssh and firewall boundary"
```

### Task 3: Install server-only packages and create isolated identities

**Files:**
- Create: `ops/remote/bootstrap-ubuntu.sh`
- Create: `ops/remote/verify-layout.sh`
- Test: `tests/ops/test_remote_bootstrap.py`

- [ ] **Step 1: Write the failing bootstrap test**

```python
from pathlib import Path


def test_bootstrap_targets_ubuntu_and_two_users() -> None:
    script = Path("ops/remote/bootstrap-ubuntu.sh").read_text()
    assert 'VERSION_ID="24.04"' in script
    assert "grader-staging" in script
    assert "grader-production" in script
    assert "/srv/grader-data/staging" in script
    assert "/srv/grader-data/production" in script
    assert "systemctl disable --now nginx" in script


def test_application_host_has_no_worker_stack() -> None:
    script = Path("ops/remote/bootstrap-ubuntu.sh").read_text().lower()
    for forbidden in ("codex", "texlive", "xelatex", "docker", "rabbitmq", "redis-server"):
        assert forbidden not in script
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/python -m pytest tests/ops/test_remote_bootstrap.py -q`

Expected: FAIL because bootstrap is absent.

- [ ] **Step 3: Implement the idempotent Ubuntu bootstrap**

The root-only script verifies Ubuntu `VERSION_ID="24.04"`, then runs:

```bash
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y --no-install-recommends \
  ca-certificates curl git jq openssl rsync ufw \
  python3 python3-pip python3-venv \
  mysql-server nginx restic
systemctl disable --now nginx

for service_user in grader-staging grader-production; do
  if ! id "$service_user" >/dev/null 2>&1; then
    useradd --system --home-dir /nonexistent --no-create-home \
      --shell /usr/sbin/nologin "$service_user"
  fi
done

install -d -o root -g root -m 0755 /opt/grader/releases /etc/grader
install -d -o grader-staging -g grader-staging -m 0750 \
  /srv/grader-data/staging /var/log/grader/staging
install -d -o grader-production -g grader-production -m 0750 \
  /srv/grader-data/production /var/log/grader/production
```

It does not run `dist-upgrade`, reboot, create a dangling release symlink or change SSH configuration.

- [ ] **Step 4: Verify layout and isolation**

`verify-layout.sh grader-prod` asserts both users use `/usr/sbin/nologin`, matching directories are mode 0750, each user is denied the other data directory, Nginx is stopped/disabled, no application port is public, Python is 3.12, and a disposable `python3 -m venv` succeeds.

- [ ] **Step 5: Execute twice and commit**

```bash
ssh grader-prod 'sudo -n bash -s' < ops/remote/bootstrap-ubuntu.sh
ssh grader-prod 'sudo -n bash -s' < ops/remote/bootstrap-ubuntu.sh
ops/remote/verify-layout.sh grader-prod
.venv/bin/python -m pytest tests/ops/test_remote_bootstrap.py -q
git add ops/remote/bootstrap-ubuntu.sh ops/remote/verify-layout.sh tests/ops/test_remote_bootstrap.py
git commit -m "ops: bootstrap isolated ubuntu service users"
```

Expected: both runs pass; the second run does not rotate credentials, start Nginx or change permissions unexpectedly.

### Task 4: Install and isolate MySQL for both environments

**Files:**
- Create: `ops/mysql/grader.cnf`
- Create: `ops/remote/configure-mysql.sh`
- Create: `ops/remote/verify-mysql-isolation.sh`
- Test: `tests/ops/test_remote_mysql.py`

- [ ] **Step 1: Write the failing configuration tests**

```python
from pathlib import Path


def test_mysql_is_loopback_and_memory_bounded() -> None:
    config = Path("ops/mysql/grader.cnf").read_text()
    assert "bind-address = 127.0.0.1" in config
    assert "mysqlx-bind-address = 127.0.0.1" in config
    assert "innodb_buffer_pool_size = 1G" in config
    assert "max_connections = 100" in config
    assert "skip_name_resolve = ON" in config


def test_users_are_local_and_scoped() -> None:
    script = Path("ops/remote/configure-mysql.sh").read_text()
    assert "'grader_staging'@'127.0.0.1'" in script
    assert "'grader_production'@'127.0.0.1'" in script
    assert "GRANT ALL" not in script.upper()
    assert "openssl rand -hex 32" in script
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/python -m pytest tests/ops/test_remote_mysql.py -q`

Expected: FAIL because the files do not exist.

- [ ] **Step 3: Add conservative MySQL settings**

`ops/mysql/grader.cnf` contains:

```ini
[mysqld]
bind-address = 127.0.0.1
mysqlx-bind-address = 127.0.0.1
skip_name_resolve = ON
character-set-server = utf8mb4
collation-server = utf8mb4_0900_ai_ci
innodb_buffer_pool_size = 1G
max_connections = 100
slow_query_log = ON
long_query_time = 1
```

- [ ] **Step 4: Implement idempotent databases and secret files**

`configure-mysql.sh` installs the config, runs `mysqld --validate-config`, and creates `grader_staging` and `grader_production`. It generates `openssl rand -hex 32` only when `/etc/grader/{environment}.env` is absent, then creates/updates the matching user at `127.0.0.1` and grants only `SELECT, INSERT, UPDATE, DELETE, CREATE, ALTER, INDEX, REFERENCES` on its database.

Write `GRADER_DATABASE_PASSWORD` and the SQLAlchemy URL through a mode-0600 temporary file, then atomically install it as `root:grader-{environment}` mode 0640. Hexadecimal passwords avoid SQL/URL escaping ambiguity. Reruns reuse the existing password and never print it.

- [ ] **Step 5: Prove isolation**

`verify-mysql-isolation.sh grader-prod` proves each Unix identity can create/drop a test table in its own database and receives access denied from the other database. It also asserts MySQL listens only at `127.0.0.1:3306` and UFW has no 3306 allow rule.

- [ ] **Step 6: Execute and commit**

```bash
ssh grader-prod 'sudo -n bash -s' < ops/remote/configure-mysql.sh
ops/remote/verify-mysql-isolation.sh grader-prod
.venv/bin/python -m pytest tests/ops/test_remote_mysql.py -q
git add ops/mysql/grader.cnf ops/remote/configure-mysql.sh ops/remote/verify-mysql-isolation.sh tests/ops/test_remote_mysql.py
git commit -m "ops: isolate remote mysql environments"
```

### Task 5: Deploy and access staging without a domain

**Depends on:** Phase 01 health API and Phase 08 release/systemd artifacts.

> **Deferred execution gate:** This task is documentation for a future deployment. Do not upload an archive, install units, start staging, or open a tunnel while the repository is still in planning/foundation work.

**Files:**
- Create: `ops/remote/install-service-units.sh`
- Create: `ops/remote/deploy-staging.sh`
- Create: `ops/remote/open-staging-tunnel.sh`
- Test: `tests/ops/test_remote_staging.py`

- [ ] **Step 1: Write unit and tunnel safety tests**

```python
from pathlib import Path


def test_only_staging_is_enabled() -> None:
    script = Path("ops/remote/install-service-units.sh").read_text()
    assert "enable --now grader-api@staging" in script
    assert "enable --now grader-scheduler@staging" in script
    assert "disable --now grader-api@production" in script
    assert "disable --now grader-scheduler@production" in script


def test_tunnel_targets_loopback() -> None:
    script = Path("ops/remote/open-staging-tunnel.sh").read_text()
    assert "18101:127.0.0.1:8101" in script
    assert "grader-prod" in script
    assert "119.45.4.159" not in script
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/python -m pytest tests/ops/test_remote_staging.py -q`

Expected: FAIL because the scripts do not exist.

- [ ] **Step 3: Install hardened service units**

`install-service-units.sh` copies the reviewed Phase 08 units and requires these settings:

```ini
User=grader-%i
Group=grader-%i
EnvironmentFile=/etc/grader/%i.env
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ReadWritePaths=/srv/grader-data/%i /var/log/grader/%i
```

It runs `systemd-analyze verify`, reloads systemd, enables only staging API/scheduler, and explicitly disables/stops production API/scheduler. Staging binds `127.0.0.1:8101`; production is configured for `127.0.0.1:8102` but remains stopped.

- [ ] **Step 4: Deploy an immutable staging release**

`deploy-staging.sh` requires `--archive` and `--version`, uploads into a mode-0700 temporary directory, verifies the supplied SHA-256, and invokes `ops/server/release.sh staging`. It runs Alembic against `grader_staging`, waits for `http://127.0.0.1:8101/health/ready`, and deletes only the resolved temporary upload path.

On failure it preserves the previous `current-staging` target and restarts that release. It never touches `current-production`, the production database or production units.

- [ ] **Step 5: Implement the SSH tunnel**

```bash
#!/usr/bin/env bash
set -euo pipefail
exec ssh -N \
  -o BatchMode=yes \
  -o ExitOnForwardFailure=yes \
  -L 18101:127.0.0.1:8101 \
  grader-prod
```

With the tunnel running, verify from another local terminal:

```bash
curl --fail --silent --show-error http://127.0.0.1:18101/health/live
curl --fail --silent --show-error http://127.0.0.1:18101/health/ready
```

- [ ] **Step 6: Prove production is dormant**

```bash
ssh grader-prod 'systemctl is-enabled grader-api@production.service || true; systemctl is-active grader-api@production.service || true; ss -lnt | grep -E ":(8101|8102) " || true'
```

Expected: staging listens at `127.0.0.1:8101`; production is disabled/inactive; nothing listens on 8102.

- [ ] **Step 7: Run and commit**

```bash
chmod +x ops/remote/install-service-units.sh ops/remote/deploy-staging.sh ops/remote/open-staging-tunnel.sh
.venv/bin/python -m pytest tests/ops/test_remote_staging.py -q
git add ops/remote/install-service-units.sh ops/remote/deploy-staging.sh ops/remote/open-staging-tunnel.sh tests/ops/test_remote_staging.py
git commit -m "ops: deploy staging behind ssh tunnel"
```

### Task 6: Add host capacity and service verification

**Files:**
- Create: `ops/remote/check-capacity.sh`
- Create: `ops/systemd/grader-host-check.service`
- Create: `ops/systemd/grader-host-check.timer`
- Test: `tests/ops/test_host_check.py`

- [ ] **Step 1: Write threshold and unit tests**

```python
from pathlib import Path


def test_capacity_thresholds_are_explicit() -> None:
    script = Path("ops/remote/check-capacity.sh").read_text()
    assert 'DISK_WARN="${DISK_WARN:-75}"' in script
    assert 'DISK_CRITICAL="${DISK_CRITICAL:-85}"' in script
    assert "MemAvailable" in script
    assert "grader-api@production" in script


def test_timer_is_periodic_without_restart_loop() -> None:
    timer = Path("ops/systemd/grader-host-check.timer").read_text()
    service = Path("ops/systemd/grader-host-check.service").read_text()
    assert "OnCalendar=*:0/5" in timer
    assert "Type=oneshot" in service
    assert "Restart=" not in service
```

- [ ] **Step 2: Implement structured capacity checks**

The script emits one secret-free JSON line and exits `0` when healthy, `1` when disk is 75–84 percent or available memory is below 1 GiB, and `2` when disk is at least 85 percent, available memory is below 512 MiB, MySQL/staging health fails, production runs unexpectedly, or 3306/8101/8102 has a non-loopback listener.

It must not print process environments, database URLs, user file names or secret paths.

- [ ] **Step 3: Install and verify the timer**

```bash
scp ops/remote/check-capacity.sh ops/systemd/grader-host-check.service ops/systemd/grader-host-check.timer grader-prod:/tmp/
ssh grader-prod 'sudo -n install -m 0755 /tmp/check-capacity.sh /usr/local/sbin/grader-host-check; sudo -n install -m 0644 /tmp/grader-host-check.service /tmp/grader-host-check.timer /etc/systemd/system/; sudo -n systemctl daemon-reload; sudo -n systemctl enable --now grader-host-check.timer; sudo -n systemctl start grader-host-check.service; sudo -n journalctl -u grader-host-check.service -n 1 --no-pager'
```

Expected: one JSON health event and an active timer. External notification delivery is outside this bootstrap.

- [ ] **Step 4: Run and commit**

```bash
.venv/bin/python -m pytest tests/ops/test_host_check.py -q
git add ops/remote/check-capacity.sh ops/systemd/grader-host-check.service ops/systemd/grader-host-check.timer tests/ops/test_host_check.py
git commit -m "ops: monitor grader host capacity"
```

### Task 7: Prepare the deferred domain and HTTPS cutover

> **Deferred execution gate:** Do not run this task until a real domain resolves to `119.45.4.159`, the required filing/configuration is complete, mini-program request domains can be registered, and production credentials exist.

**Files:**
- Create: `ops/remote/enable-domain.sh`
- Create: `ops/remote/disable-domain.sh`
- Create: `docs/operations/domain-cutover.md`
- Test: `tests/ops/test_domain_cutover.py`

- [ ] **Step 1: Write precondition and rollback tests**

```python
from pathlib import Path


def test_cutover_requires_domain_and_approval() -> None:
    script = Path("ops/remote/enable-domain.sh").read_text()
    assert "--domain" in script
    assert "/etc/grader/domain-cutover.approved" in script
    assert "119.45.4.159" in script
    assert "getent ahostsv4" in script


def test_disable_closes_http_but_keeps_ssh() -> None:
    script = Path("ops/remote/disable-domain.sh").read_text()
    assert "ufw delete allow 80/tcp" in script
    assert "ufw delete allow 443/tcp" in script
    assert "ufw delete allow OpenSSH" not in script
```

- [ ] **Step 2: Implement fail-closed preflight**

`enable-domain.sh --domain "$GRADER_DOMAIN"` refuses to proceed unless the domain A record includes `119.45.4.159`; the root-owned mode-0600 approval marker exists; Fake authentication/payment are disabled in production; production MySQL isolation and backup checks pass; production health passes on loopback; `nginx -t` passes; and TCP 22 remains allowed.

- [ ] **Step 3: Implement controlled enable and rollback**

Only after preflight, the future script installs Ubuntu `certbot` packages, allows UFW 80/443, pauses for the operator to open only 80/443 in the Tencent security group, obtains TLS, starts Nginx, and runs external HTTPS checks. Production traffic is enabled only after Phase 08 readiness passes.

Any failure after opening ports invokes `disable-domain.sh`, which stops/disables Nginx and removes only the 80/443 UFW rules. SSH, databases, files and staging tunnel access remain intact.

- [ ] **Step 4: Document human gates**

`domain-cutover.md` has unchecked items for DNS, filing/configuration, TLS, AppID, merchant binding, callback URL, mini-program request/download domains, COS backup, restore drill, production readiness and rollback owner. No item is preselected.

- [ ] **Step 5: Test and commit without remote execution**

```bash
.venv/bin/python -m pytest tests/ops/test_domain_cutover.py -q
bash -n ops/remote/enable-domain.sh ops/remote/disable-domain.sh
git add ops/remote/enable-domain.sh ops/remote/disable-domain.sh docs/operations/domain-cutover.md tests/ops/test_domain_cutover.py
git commit -m "ops: prepare domain and tls cutover"
```

Expected: local tests pass; no remote port, package, DNS or service changes occur.

### Task 8: Run the remote-foundation acceptance gate

**Files:**
- Create: `ops/remote/verify-host.sh`
- Create: `docs/operations/remote-environment-runbook.md`
- Test: `tests/ops/test_verify_host.py`

- [ ] **Step 1: Write the verifier contract test**

```python
from pathlib import Path


def test_verifier_covers_isolation_boundaries() -> None:
    script = Path("ops/remote/verify-host.sh").read_text()
    for check in (
        "passwordauthentication no",
        "permitrootlogin no",
        "ufw status",
        "grader-staging",
        "grader-production",
        "127.0.0.1:3306",
        "127.0.0.1:8101",
        "grader-api@production",
    ):
        assert check in script
    assert "codex" in script.lower()
    assert "xelatex" in script.lower()
```

- [ ] **Step 2: Implement one read-only acceptance command**

`verify-host.sh grader-prod --mode pre-domain` composes the earlier read-only checks and proves key-only SSH, active UFW, no public application/database ports, cross-user and cross-database denial, expected CPU/RAM/swap/disk, automatic security updates, stopped Nginx, dormant production, optional staging loopback health, and absence of Codex/XeLaTeX.

It prints `PASS` or `FAIL` per check and one final line. It must not install, upload, restart, enable, disable, delete or rewrite anything.

- [ ] **Step 3: Write the operational runbook**

The runbook contains future commands for inventory, UFW console recovery, staging deployment/rollback, opening/closing the tunnel, MySQL verification, journal inspection, production dormancy and the Phase 08 handoff. Database restore/drop, production enablement and public-port opening require separate explicit operator approval.

- [ ] **Step 4: Run the future acceptance gate after Tasks 1–6 are implemented**

```bash
bash -n ops/remote/*.sh
.venv/bin/python -m pytest tests/ops -q
ops/remote/verify-host.sh grader-prod --mode pre-domain
```

Expected final line: `PASS remote foundation pre-domain`.

- [ ] **Step 5: Run the repository gate and commit**

```bash
.venv/bin/python -m pytest -q
git status --short
git add ops/remote docs/operations/remote-environment-runbook.md tests/ops/test_verify_host.py
git commit -m "ops: verify remote server foundation"
```
