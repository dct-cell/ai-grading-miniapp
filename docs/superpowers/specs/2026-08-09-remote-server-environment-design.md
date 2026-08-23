# Remote Server Environment Design

## Scope

This design covers the first real Tencent Cloud CVM host for the grading service. It prepares isolated staging and production foundations while exposing no application port before the domain, ICP-related configuration, TLS certificate, and mini-program request-domain setup are ready.

The host is accessed only through the local SSH alias `grader-prod`. Plans and operational commands must not use password authentication, copy private keys to the server, or replace the alias with an inline password or raw-IP login.

This design does not install Codex CLI, XeLaTeX, grading skills, or Worker runtimes. Those belong on Worker hosts, not the mainland application/database server.

## Verified baseline

The following facts were verified read-only through `ssh grader-prod` on 2026-08-09:

- SSH alias: `grader-prod`
- Public address: `119.45.4.159`
- Login user: `ubuntu`
- Authentication: `IdentitiesOnly yes` with `~/.ssh/grader_prod_ed25519`
- Operating system: Ubuntu 24.04.4 LTS, x86-64, Tencent Cloud KVM
- Capacity: 2 vCPU, 7.4 GiB RAM, 1.9 GiB swap
- Root disk: 100 GB ext4, approximately 89 GB available
- Time zone: `Asia/Shanghai`; clock synchronized
- Python: 3.12.3
- Existing public listener: SSH on TCP 22 only
- Effective SSH policy: public-key authentication enabled, password authentication disabled, root login disabled
- Privilege: `ubuntu` has non-interactive sudo
- UFW: installed but inactive
- Automatic security updates: installed, enabled, and active
- APT source: Tencent Cloud Ubuntu mirror in `/etc/apt/sources.list.d/ubuntu.sources`
- Nginx, MySQL, restic and certbot: not installed
- `/opt/grader`, `/srv/grader-data`, `/etc/grader`, and `/var/log/grader`: absent

`/etc/ssh/sshd_config.d/50-cloud-init.conf` contains `PasswordAuthentication yes`, but `/etc/ssh/sshd_config.d/00-key-only.conf` is evaluated first and the verified effective setting is `passwordauthentication no`. Every bootstrap run must verify the effective value with `sshd -T`; it must not rely on file text alone.

## Chosen deployment model

Use one host with two isolated application environments:

| Concern | Staging | Production |
|---|---|---|
| OS user | `grader-staging` | `grader-production` |
| Database | `grader_staging` | `grader_production` |
| Database user | `grader_staging`@`localhost` | `grader_production`@`localhost` |
| Data root | `/srv/grader-data/staging` | `/srv/grader-data/production` |
| Environment file | `/etc/grader/staging.env` | `/etc/grader/production.env` |
| API bind | `127.0.0.1:8101` | `127.0.0.1:8102` |
| Initial state | enabled after application exists | installed but disabled/stopped |
| Initial access | SSH local forwarding only | no access |

Release directories under `/opt/grader/releases` are root-owned and read-only to both service users. Each environment has its own `current-staging` or `current-production` symlink so one environment can roll back without switching the other.

Sharing one Unix user would let staging read production secrets and files. Separate service users are therefore required even on this small host.

## Network boundary before a domain exists

Tencent Cloud security groups and UFW form two independent inbound filters:

- TCP 22 remains permitted for operator SSH access.
- TCP 3306 is never public.
- TCP 8101 and 8102 bind only to loopback and are never added to UFW or the cloud security group.
- TCP 80 and 443 remain closed until the domain cutover gate.
- Nginx may be installed and syntax-tested, but it remains stopped and disabled before domain cutover.

Staging is accessed from the operator Mac with:

```bash
ssh -N -L 18101:127.0.0.1:8101 grader-prod
```

The browser and API tests then use `http://127.0.0.1:18101`. This preserves end-to-end server, database, Admin, file and Worker API testing without creating a temporary public HTTP service.

UFW activation is a guarded operation: add the OpenSSH rule first, validate the candidate rules, enable UFW, and prove a second independent `ssh grader-prod` connection before closing the original session. A Tencent Cloud console/VNC recovery path and a recent system-disk snapshot must be available before this step.

## Installed server components

The host receives only application-server dependencies:

- `python3-venv` and `python3-pip`
- `nginx`
- `mysql-server`
- `restic`
- `jq`, `curl`, `git`, `ca-certificates`, `openssl`, `ufw`

Use Ubuntu 24.04 packages from the configured Tencent mirror. Do not add a third-party PPA for the MVP. Certbot is deferred until a real domain resolves to the host.

MySQL binds to `127.0.0.1`, uses two databases and two local-only least-privilege users, and starts with a conservative 1 GiB InnoDB buffer pool. Application passwords are generated with `openssl rand -hex 32`, stored only in root-owned environment files, and never passed as command-line arguments or committed.

## Deployment sequencing

Remote preparation is intentionally split into checkpoints:

1. Capture an immutable inventory and create a Tencent Cloud disk snapshot.
2. Verify SSH recovery and enable the host firewall without changing the working SSH policy.
3. Install pinned Ubuntu package families and create isolated users/directories.
4. Install and isolate MySQL.
5. Verify Python virtual-environment creation and reserve loopback ports.
6. After Phase 01 produces a runnable server, deploy staging and test it through the SSH tunnel.
7. Keep production stopped until the domain readiness gate.
8. After DNS, ICP-related configuration and mini-program credentials are ready, open 80/443, obtain TLS, enable Nginx, and then enable production.

The plan must stop after every checkpoint if verification fails. It must never continue from a partially failed firewall, SSH, MySQL, or permission change.

## Observability and capacity

The 2 vCPU/8 GB/100 GB host is sufficient for the application, MySQL and Nginx because Codex and LaTeX run on Worker machines. Initial limits are:

- staging API: one process, small connection pool
- production API: two processes only after production launch
- MySQL buffer pool: 1 GiB initially
- disk alert threshold: 75 percent warning, 85 percent critical
- data and journal retention sized so the root disk cannot be consumed silently

The initial health checks use systemd and journald locally. External alert delivery is outside this bootstrap and can later consume the same health script.

## Failure handling and rollback

- Before package or configuration changes, capture package state and copies of files that will change.
- Validate configuration before restarting a service: `sshd -t`, `nginx -t`, `mysqld --validate-config`, and `systemd-analyze verify` where applicable.
- Never edit the active SSH connection path and enable UFW in the same unchecked step.
- Database creation is additive; rollback drops only empty newly created databases/users after explicit verification.
- Production remains stopped, so staging failure cannot expose a half-configured public application.
- Domain cutover has a separate rollback: close 80/443, disable Nginx, stop production, and return to SSH-tunnel-only staging.

## Acceptance criteria

The remote foundation is ready when all of the following are true:

- `ssh -o BatchMode=yes grader-prod true` succeeds and password/root SSH remain disabled.
- UFW is active with SSH allowed and no public database/application ports.
- only SSH is publicly reachable before domain cutover.
- two service users cannot read each other's secrets or data directories.
- two MySQL users cannot read each other's databases.
- staging can create a Python 3.12 virtual environment and later bind only `127.0.0.1:8101`.
- production units are disabled and stopped.
- an SSH tunnel can reach staging health after the application is deployed.
- verification scripts are idempotent and a second bootstrap run makes no destructive changes.
- no Codex, LaTeX, Worker credential, WeChat secret, or private SSH key exists on this server as part of bootstrap.
