# Production operations

The supported initial topology is one Nginx, one Uvicorn process on
`127.0.0.1:8101`, one Scheduler, one MySQL database and one outbound-only Mac
Worker. Install service files from `systemd/`, the Nginx vhost from `nginx/`,
and keep all secrets under `/etc/grader` with mode `0600`.

Before enabling traffic:

1. Create the `grader` user, `/srv/grader-data`, `/var/lib/grader-backup` and
   `/var/log/grader` owned by that user.
2. Populate `/etc/grader/grader.env` from `.env.example`; provide real WeChat
   merchant key/public key files outside the repository.
3. Install the Python environment at `/srv/grader/venv`, run
   `scripts/verify-environment.sh`, migrate, then enable the API/Scheduler.
4. Configure COS lifecycle rules to retain 7 daily and 4 weekly encrypted
   objects. Run `restore-verify.sh` monthly against an isolated database.
5. Render the launchd plist placeholders on the Mac, store its Worker `.env`
   outside the checkout (`GRADER_WORKER_ENV_FILE` points to it), load it with
   `launchctl bootstrap`, and verify the Worker is online before accepting
   payments.

The Worker supports `GRADER_WORKER_MAX_CONCURRENT_JOBS=1..10`. One local
supervisor registers one stable server identity per slot, so the Server's
one-job-per-worker fencing model is unchanged. Start a new Mac mini at `4`, run
representative Codex/XeLaTeX load tests, then raise to `6`, `8`, or `10` only
when memory pressure, thermal load, Codex rate limits, and report latency stay
healthy.

`deploy.sh` atomically changes `/srv/grader/current`; `rollback.sh` restores the
previous code release. Database migrations remain forward-only, so a release
with an incompatible migration needs its documented data rollback rather than
blindly downgrading Alembic.
