# Phase 08 Deployment, Packaging and WeChat Cutover Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deploy isolated staging and production environments on the purchased Linux server, back them up to private COS, package native Workers for three operating systems, and prepare a credentials-only cutover to real WeChat login/payment.

**Architecture:** Nginx is the only public process after domain cutover. Separate systemd units, Unix users, databases, directories, secrets and COS prefixes isolate staging from production on one host. Versioned per-environment release symlinks make rollback atomic; Workers are versioned ZIPs with a shared Python core and native service adapters. Phase 09 applies these artifacts to the verified `grader-prod` host and keeps staging tunnel-only until the domain gate.

**Tech Stack:** Ubuntu 24.04 LTS, Nginx, systemd, MySQL 8, Python virtual environments, restic with Tencent COS S3 endpoint, Bash, PowerShell, LaunchAgent, Codex CLI, XeLaTeX

---

### Task 1: Create versioned server releases

**Files:**
- Create: ops/server/install.sh
- Create: ops/server/release.sh
- Create: ops/systemd/grader-api@.service
- Create: ops/systemd/grader-scheduler@.service
- Create: ops/env/staging.env.example
- Create: ops/env/production.env.example
- Test: tests/ops/test_server_units.py

- [ ] **Step 1: Write failing unit-file assertions**

    def test_api_unit_runs_as_low_privilege_user() -> None:
        unit = Path("ops/systemd/grader-api@.service").read_text()
        assert "User=grader-%i" in unit
        assert "Group=grader-%i" in unit
        assert "EnvironmentFile=/etc/grader/%i.env" in unit
        assert "NoNewPrivileges=true" in unit
        assert "Restart=always" in unit

- [ ] **Step 2: Confirm failure**

    .venv/bin/python -m pytest tests/ops/test_server_units.py -q

Expected: unit files are missing.

- [ ] **Step 3: Implement release layout**

install.sh creates:
- /opt/grader/releases
- /opt/grader/current-staging and /opt/grader/current-production symlinks
- /etc/grader with mode 0750
- /srv/grader-data/staging and production
- /var/log/grader
- low-privilege grader-staging and grader-production users

release.sh accepts an explicit environment, version and archive path, extracts into a new release, creates its venv, installs the wheel, runs alembic upgrade head for the selected environment, runs health preflight, atomically switches only current-staging or current-production, and restarts only that environment. It retains the previous two releases per environment.

- [ ] **Step 4: Implement systemd units**

The API unit binds 127.0.0.1:8101 for staging and 127.0.0.1:8102 for production through environment variables. Scheduler has one instance per environment and uses the database advisory lock. Both set PrivateTmp=true, ProtectSystem=strict, ReadWritePaths to their own data/log directories, and a 60-second stop timeout.

- [ ] **Step 5: Run and commit**

    .venv/bin/python -m pytest tests/ops/test_server_units.py -q
    git add ops/server ops/systemd ops/env tests/ops/test_server_units.py
    git commit -m "ops: add versioned server deployment"

### Task 2: Configure Nginx and network boundaries

**Files:**
- Create: ops/nginx/grader.conf
- Create: tests/ops/test_nginx_config.py

- [ ] **Step 1: Write config assertions**

    def test_nginx_never_exposes_mysql_or_file_root() -> None:
        config = Path("ops/nginx/grader.conf").read_text()
        assert "proxy_pass http://127.0.0.1:8102" in config
        assert "location /_protected_files/" in config
        assert "internal;" in config
        assert "client_max_body_size 27m" in config

- [ ] **Step 2: Implement Nginx routes**

Production domain proxies /api, /worker, /callbacks and /health to 8102 and serves the built Admin under /admin. A separate staging host proxies to 8101 and is IP allowlisted or password-protected. Use X-Accel-Redirect to an internal alias for authorized downloads; do not expose the physical path in public URLs.

- [ ] **Step 3: Validate syntax**

    sudo nginx -t -c /absolute/path/to/ops/nginx/grader.conf

Expected: syntax is ok and test is successful.

- [ ] **Step 4: Commit**

    git add ops/nginx tests/ops/test_nginx_config.py
    git commit -m "ops: add secure nginx boundary"

### Task 3: Configure MySQL isolation

**Files:**
- Create: ops/mysql/bootstrap.sql
- Create: ops/scripts/check-db-isolation.sh
- Test: tests/ops/test_mysql_bootstrap.py

- [ ] **Step 1: Write least-privilege assertions**

    def test_mysql_bootstrap_creates_separate_databases_and_users() -> None:
        sql = Path("ops/mysql/bootstrap.sql").read_text()
        assert "CREATE DATABASE grader_staging" in sql
        assert "CREATE DATABASE grader_production" in sql
        assert "grader_staging.*" in sql
        assert "grader_production.*" in sql
        assert "*.*" not in sql

- [ ] **Step 2: Implement bootstrap.sql**

Create utf8mb4 databases and separate local-only users. Grant each user only SELECT, INSERT, UPDATE, DELETE, CREATE, ALTER, INDEX and REFERENCES on its own database. MySQL binds 127.0.0.1 and firewall port 3306 remains closed.

- [ ] **Step 3: Run integration check**

    ops/scripts/check-db-isolation.sh

Expected: staging user cannot select from grader_production and production user cannot select from grader_staging.

- [ ] **Step 4: Commit**

    git add ops/mysql ops/scripts/check-db-isolation.sh tests/ops/test_mysql_bootstrap.py
    git commit -m "ops: isolate staging and production mysql"

### Task 4: Back up MySQL and active files to COS

**Files:**
- Create: ops/backup/backup.sh
- Create: ops/backup/restore.sh
- Create: ops/systemd/grader-backup.service
- Create: ops/systemd/grader-backup.timer
- Create: ops/backup/README.md
- Test: tests/ops/test_backup_scripts.py

- [ ] **Step 1: Write safety assertions**

    def test_backup_requires_encrypted_repository() -> None:
        script = Path("ops/backup/backup.sh").read_text()
        assert "RESTIC_PASSWORD_FILE" in script
        assert "mysqldump" in script
        assert "restic backup" in script
        assert "restic forget" in script
        assert "--keep-within 35d" in script

- [ ] **Step 2: Implement backup.sh**

For each environment:
1. take a transaction-consistent mysqldump into a mode-0600 temporary directory;
2. run restic backup for the dump and that environment's /srv/grader-data directory;
3. use a private COS bucket through its same-region S3 endpoint;
4. run restic check;
5. run forget --keep-daily 7 --keep-weekly 4 --keep-within 35d --prune;
6. delete the temporary dump with an explicit resolved path;
7. emit one structured success/failure event without secrets.

Use separate repository prefixes and passwords for staging and production. COS credentials may read/write only those prefixes. Do not enable public bucket access.

- [ ] **Step 3: Implement restore.sh**

Require --environment, --snapshot and --target. Refuse /, home directories, /srv/grader-data itself and non-empty targets unless --force is passed. Restore to a temporary directory, verify restic and SHA-256 manifests, then print the exact MySQL import and file-switch commands without executing production cutover automatically.

- [ ] **Step 4: Run a disposable restore drill**

    ops/backup/backup.sh staging
    ops/backup/restore.sh --environment staging --snapshot latest --target /tmp/grader-restore-drill
    .venv/bin/python ops/scripts/verify-restored-order.py /tmp/grader-restore-drill

Expected: one known order has its source PDF, result JSON/PDF and matching database rows.

- [ ] **Step 5: Commit**

    git add ops/backup ops/systemd/grader-backup.service ops/systemd/grader-backup.timer tests/ops/test_backup_scripts.py
    git commit -m "ops: add encrypted cos backups and restore drill"

### Task 5: Package native Workers

**Files:**
- Create: worker/packaging/build.py
- Create: worker/packaging/manifest.json
- Create: worker/packaging/macos/install.sh
- Create: worker/packaging/macos/com.grader.worker.plist
- Create: worker/packaging/linux/install.sh
- Create: worker/packaging/linux/grader-worker.service
- Create: worker/packaging/windows/install.ps1
- Create: worker/packaging/windows/register-task.ps1
- Create: tests/worker/test_packages.py

- [ ] **Step 1: Write package inventory test**

    @pytest.mark.parametrize("platform_name", ["macos-arm64", "linux-amd64", "windows-x64"])
    def test_zip_contains_runtime_but_no_credentials(tmp_path, platform_name) -> None:
        archive = build_package(platform_name, "1.0.0", tmp_path)
        names = zip_names(archive)
        assert "VERSION" in names
        assert "CHECKSUMS.sha256" in names
        assert any(name.endswith("grader-worker") or name.endswith("grader-worker.exe") for name in names)
        assert not any(name.endswith(".env") or "auth.json" in name for name in names)

- [ ] **Step 2: Implement deterministic ZIP build**

Package the Worker core, app grading modules, olympiad-grader skill, schemas, fonts, lockfile, doctor, uninstaller, service template, VERSION and checksums. Build filenames:
- grader-worker-1.0.0-macos-arm64.zip
- grader-worker-1.0.0-linux-amd64.zip
- grader-worker-1.0.0-windows-x64.zip

Do not embed shared key, worker ID, Codex auth, server Admin code or WeChat credentials.

- [ ] **Step 3: Implement installers**

macOS creates an isolated venv and user LaunchAgent. Linux creates a dedicated grader-worker user and systemd service. Windows uses native Python, PowerShell and Task Scheduler at user logon; it does not require WSL. All installers prompt for server URL, device name and shared key, write mode-0600 or user-ACL config, run doctor, register, and start only after doctor passes.

- [ ] **Step 4: Implement upgrade rollback**

Installer drain-stops the old version, installs beside it, reuses protected config, runs doctor and golden PDF, then switches current. Failure leaves the old version active. It never modifies system Python packages.

- [ ] **Step 5: Run package gates on all three hosts**

On each OS:
1. fresh install;
2. doctor;
3. fake grading;
4. real demo grading;
5. service restart;
6. upgrade;
7. rollback;
8. uninstall preserving config only after explicit choice.

- [ ] **Step 6: Commit**

    .venv/bin/python -m pytest tests/worker/test_packages.py -q
    git add worker/packaging tests/worker/test_packages.py
    git commit -m "build: package native workers"

### Task 6: Implement production WeChat adapters

**Files:**
- Create: server/adapters/wechat_auth.py
- Create: server/adapters/wechat_pay.py
- Create: server/adapters/wechat_signatures.py
- Test: tests/server/test_wechat_auth.py
- Test: tests/server/test_wechat_pay.py

- [ ] **Step 1: Write fixture-based signature tests**

Use checked-in synthetic keys and payloads that contain no merchant secrets. Verify valid callback signatures, reject modified body/timestamp/nonce, decrypt resource ciphertext, and confirm duplicate notifications remain idempotent.

- [ ] **Step 2: Implement WeChatAuthProvider**

Exchange wx.login code through code2Session using server-side AppID and AppSecret. Set strict timeouts, reject missing openid, never log code, AppSecret or session_key, and map provider errors to stable public codes.

- [ ] **Step 3: Implement WeChatPayGateway**

Implement JSAPI/mini-program prepay, query order, full refund, query refund, payment notification and refund notification using API v3 signatures. The mini-program receives only the client payment parameters. Server callback/query remains authoritative.

- [ ] **Step 4: Add adapter selection**

Use fake adapters in development/staging by explicit environment setting and production adapters only when all required credentials validate at startup. Production must refuse to start with FakePaymentGateway enabled.

- [ ] **Step 5: Run and commit**

    .venv/bin/python -m pytest tests/server/test_wechat_auth.py tests/server/test_wechat_pay.py -q
    git add server/adapters tests/server/test_wechat_auth.py tests/server/test_wechat_pay.py
    git commit -m "feat: add production wechat adapters"

### Task 7: Run production-readiness gate

**Files:**
- Create: ops/scripts/smoke-staging.sh
- Create: ops/scripts/production-readiness.sh
- Create: docs/operations/runbook.md

- [ ] **Step 1: Verify staging**

Run full fake flow, three concurrent Workers, a lease failure, V1 refund, V1-to-V2 review, V2 refund, file revocation and backup restore.

- [ ] **Step 2: Verify production prerequisites without taking payments**

production-readiness.sh checks DNS, ICP-configured HTTPS domain, certificate chain, MySQL isolation, COS backup, AppID/auth exchange, merchant/AppID binding, callback reachability, Nginx limits, disk alarms and disabled fake endpoints.

- [ ] **Step 3: Document rollback**

runbook.md must include commands to drain Workers, stop only production, switch the previous release symlink, restore the previous database backup into a new database, change the environment URL, start, verify and reopen traffic. Never overwrite the only database during a restore.

- [ ] **Step 4: Run all tests**

    .venv/bin/python -m pytest -q
    cd admin && npm test && npm run build
    cd ../miniapp && npm test

Expected: all commands pass.

- [ ] **Step 5: Commit**

    git add ops/scripts docs/operations/runbook.md
    git commit -m "ops: add production readiness and rollback"
