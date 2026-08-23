# Phase 03 Worker Control Plane Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Allow any enabled macOS, Linux or Windows Worker to authenticate, register, lease one job, renew ownership, upload a result and commit it without duplicate execution or stale writes.

**Architecture:** Workers make outbound HTTPS requests only. The server uses one shared bearer secret plus a distinct worker ID, MySQL row locks for queue claims, 20-second renewals, 120-second rolling leases, and a monotonically increasing lease version as the fencing token.

**Tech Stack:** FastAPI, SQLAlchemy/MySQL row locks, HTTPX, Pydantic, asyncio, SHA-256, pytest

---

### Task 1: Authenticate and register Workers

**Files:**
- Create: server/api/worker_dependencies.py
- Create: server/api/workers.py
- Create: server/services/workers.py
- Modify: server/main.py
- Test: tests/server/test_worker_auth.py

- [ ] **Step 1: Write failing authentication tests**

    def test_worker_requires_bearer_key_and_worker_id(client) -> None:
        assert client.post("/worker/v1/heartbeat").status_code == 401

    def test_disabled_worker_is_forbidden(worker_client, worker) -> None:
        worker.status = "disabled"
        worker_client.session.commit()
        assert worker_client.post("/worker/v1/heartbeat").status_code == 403

- [ ] **Step 2: Confirm failure**

    .venv/bin/python -m pytest tests/server/test_worker_auth.py -q

Expected: missing routes.

- [ ] **Step 3: Implement constant-time shared-key verification**

    def verify_shared_key(provided: str, expected: str) -> bool:
        provided_hash = hashlib.sha256(provided.encode()).digest()
        expected_hash = hashlib.sha256(expected.encode()).digest()
        return hmac.compare_digest(provided_hash, expected_hash)

Read Authorization: Bearer and X-Worker-ID. Registration uses the bearer key but no worker ID; it accepts device_name, platform, architecture, worker_version, codex_version, tex_version and capabilities.

- [ ] **Step 4: Implement registration response**

    class WorkerRegistrationResponse(BaseModel):
        worker_id: str
        heartbeat_interval_seconds: int = 20
        lease_seconds: int = 120
        long_poll_seconds: int = 25
        minimum_worker_version: str

A repeated registration with the same generated installation_id returns the same worker_id. The ZIP must not contain installation_id or the shared key; the installer writes them into the protected local config.

- [ ] **Step 5: Run and commit**

    .venv/bin/python -m pytest tests/server/test_worker_auth.py -q
    git add server/api server/services/workers.py server/main.py tests/server/test_worker_auth.py
    git commit -m "feat: authenticate and register workers"

### Task 2: Claim one job per Worker

**Files:**
- Create: server/services/leases.py
- Create: server/schemas/worker_jobs.py
- Modify: server/api/workers.py
- Test: tests/server/test_job_leases.py
- Test: tests/integration/test_mysql_job_claim.py

- [ ] **Step 1: Write failing lease tests**

    def test_worker_with_active_job_cannot_lease_second_job(
        worker_client, queued_jobs
    ) -> None:
        first = worker_client.post("/worker/v1/jobs/lease").json()
        second = worker_client.post(
            "/worker/v1/jobs/lease", headers={"Prefer": "wait=0"}
        )
        assert first["job_id"] != ""
        assert second.status_code == 204

    def test_ack_timeout_returns_unstarted_job_to_queue(clock, lease_service, job) -> None:
        lease = lease_service.try_lease("worker-a")
        clock.advance(seconds=31)
        lease_service.release_unacknowledged()
        assert lease_service.get(job.id).state == "queued"

- [ ] **Step 2: Confirm failure**

    .venv/bin/python -m pytest tests/server/test_job_leases.py -q

Expected: LeaseService is missing.

- [ ] **Step 3: Implement atomic MySQL claim**

Inside one transaction, first reject a Worker that already has LEASED/RUNNING/UPLOADING work. Then execute:

    statement = (
        select(GradingJob)
        .where(GradingJob.state == JobState.QUEUED)
        .order_by(GradingJob.queued_at, GradingJob.id)
        .with_for_update(skip_locked=True)
        .limit(1)
    )

Set state LEASED, worker_id, lease_version = lease_version + 1, ack_deadline = now + 30 seconds, lease_expires_at = now + 120 seconds. Return a TaskBundle containing IDs, round, grading standard, note, page count, file IDs and short-lived download tokens.

- [ ] **Step 4: Implement 25-second long poll**

    deadline = time.monotonic() + wait_seconds
    while time.monotonic() < deadline:
        bundle = await anyio.to_thread.run_sync(lease_service.try_lease, worker_id)
        if bundle is not None:
            return bundle
        await asyncio.sleep(1)
    return Response(status_code=204)

Cap wait_seconds at 25. Prefer: wait=0 is used by tests and diagnostics.

- [ ] **Step 5: Prove concurrent claims on MySQL**

Start three registered Workers and three queued jobs. Use a thread barrier so all calls claim simultaneously. Assert three distinct job IDs and no fourth claim. Run only when GRADER_TEST_MYSQL_URL is present.

- [ ] **Step 6: Run and commit**

    .venv/bin/python -m pytest tests/server/test_job_leases.py -q
    .venv/bin/python -m pytest tests/integration/test_mysql_job_claim.py -q
    git add server/services/leases.py server/schemas/worker_jobs.py server/api/workers.py tests
    git commit -m "feat: lease one job per worker"

Expected: with GRADER_TEST_MYSQL_URL supplied by the staging or CI secret store, one claim succeeds per Worker, all job IDs are distinct, and the fourth claim returns 204.

### Task 3: ACK, heartbeat and lease renewal

**Files:**
- Modify: server/services/leases.py
- Modify: server/api/workers.py
- Create: server/schemas/heartbeats.py
- Test: tests/server/test_lease_renewal.py

- [ ] **Step 1: Write fencing tests**

    def test_wrong_worker_cannot_ack(worker_b_client, lease_for_worker_a) -> None:
        response = worker_b_client.post(
            f"/worker/v1/jobs/{lease_for_worker_a.job_id}/ack",
            json={"lease_version": lease_for_worker_a.lease_version},
        )
        assert response.status_code == 409

    def test_renewal_extends_from_server_time(worker_client, active_lease, clock) -> None:
        clock.advance(seconds=20)
        response = worker_client.post(
            f"/worker/v1/jobs/{active_lease.job_id}/renew",
            json={"lease_version": active_lease.lease_version, "phase": "grading"},
        )
        assert parse(response.json()["lease_expires_at"]) == clock.now + timedelta(seconds=120)

- [ ] **Step 2: Confirm failure**

    .venv/bin/python -m pytest tests/server/test_lease_renewal.py -q

Expected: routes are missing.

- [ ] **Step 3: Implement ACK and renewal**

ACK changes LEASED to RUNNING only when worker_id, lease_version, state and ack_deadline all match. Renewal accepts only RUNNING or UPLOADING and sets lease_expires_at using server time. Heartbeat updates Worker.last_heartbeat_at, current_job_id, runtime metrics and active phase; it may carry the same renewal payload to reduce requests.

- [ ] **Step 4: Implement expiry policy**

The scheduler:
- marks a Worker suspected_offline after 60 seconds without heartbeat;
- returns only unacknowledged LEASED jobs to QUEUED after 30 seconds;
- marks RUNNING/UPLOADING jobs WORKER_EXCEPTION after lease expiry;
- never automatically requeues a started job.

- [ ] **Step 5: Run and commit**

    .venv/bin/python -m pytest tests/server/test_lease_renewal.py -q
    git add server/services/leases.py server/api/workers.py server/schemas/heartbeats.py tests/server/test_lease_renewal.py
    git commit -m "feat: renew and expire worker leases"

### Task 4: Stage and commit results

**Files:**
- Create: server/services/results.py
- Create: server/api/worker_results.py
- Create: server/schemas/results.py
- Modify: server/main.py
- Test: tests/server/test_worker_results.py

- [ ] **Step 1: Write stale-result tests**

    def test_expired_lease_cannot_commit_result(worker_client, expired_lease) -> None:
        response = worker_client.post(
            f"/worker/v1/jobs/{expired_lease.job_id}/result/commit",
            json={
                "lease_version": expired_lease.lease_version,
                "result_json_file_id": "json-1",
                "result_pdf_file_id": "pdf-1",
            },
        )
        assert response.status_code == 409

    def test_duplicate_commit_is_idempotent(worker_client, committed_result) -> None:
        response = worker_client.post(committed_result.url, json=committed_result.payload)
        assert response.status_code == 200
        assert response.json()["status"] == "already_committed"

- [ ] **Step 2: Confirm failure**

    .venv/bin/python -m pytest tests/server/test_worker_results.py -q

Expected: routes are missing.

- [ ] **Step 3: Implement staged uploads**

Issue single-use upload tokens bound to job_id, worker_id, lease_version, file kind and maximum size. Store uploads under result-staging/job_id/lease_version. Verify SHA-256 and PDF readability before creating FileObject rows.

- [ ] **Step 4: Implement transactional commit**

Lock the job. Verify worker ID, lease version and state UPLOADING. Atomically move the verified staged files into orders/YYYY/MM/order_id/round_number on the same filesystem. Then, in one database transaction:
- bind JSON/PDF FileObjects to the GradingRound;
- set round delivered_at;
- set job SUCCEEDED;
- set order to V1_DELIVERED or V2_DELIVERED;
- set acceptance_deadline to now + three days;
- clear Worker.current_job_id;
- write WorkerEvent.

The FileObject rows must reference the final relative paths. The order becomes user-visible only after the database commit. If the database transaction rolls back, remove the newly moved orphan files; a scheduled reconciliation test must prove that no final file is left bound to an uncommitted result.

- [ ] **Step 5: Run and commit**

    .venv/bin/python -m pytest tests/server/test_worker_results.py -q
    git add server/services/results.py server/api/worker_results.py server/schemas/results.py server/main.py tests/server/test_worker_results.py
    git commit -m "feat: fence and commit grading results"

### Task 5: Build the platform-neutral Worker daemon with FakeGrader

**Files:**
- Create: worker/__init__.py
- Create: worker/config.py
- Create: worker/client.py
- Create: worker/runtime/daemon.py
- Create: worker/runtime/fake_grader.py
- Create: worker/cli.py
- Test: tests/worker/test_daemon.py

- [ ] **Step 1: Write the failing daemon flow test**

    async def test_daemon_processes_exactly_one_lease(fake_server, tmp_path) -> None:
        daemon = WorkerDaemon(
            client=fake_server.client,
            runtime=FakeGrader(),
            workspace_root=tmp_path,
        )
        await daemon.run_one_poll()
        assert fake_server.calls == ["lease", "download", "ack", "upload", "commit"]
        assert not list(tmp_path.iterdir())

- [ ] **Step 2: Confirm failure**

    .venv/bin/python -m pytest tests/worker/test_daemon.py -q

Expected: WorkerDaemon is missing.

- [ ] **Step 3: Implement the daemon sequence**

    async def run_one_poll(self) -> None:
        lease = await self.client.lease()
        if lease is None:
            return
        workspace = self.workspace_root / lease.job_id / str(lease.lease_version)
        workspace.mkdir(parents=True)
        try:
            bundle = await self.client.download_bundle(lease, workspace)
            await self.client.ack(lease)
            async with LeaseRenewer(self.client, lease, interval_seconds=20):
                result = await self.runtime.run(workspace, bundle)
                uploads = await self.client.upload_result(lease, result)
                await self.client.commit_result(lease, uploads)
        finally:
            shutil.rmtree(workspace, ignore_errors=True)

- [ ] **Step 4: Add CLI commands**

Expose register, doctor, run, run-once, status and drain. Never print the shared key or raw task tokens.

- [ ] **Step 5: Run phase gate and commit**

    .venv/bin/python -m pytest tests/worker tests/server -q
    .venv/bin/python -m pytest -q
    git add worker tests/worker
    git commit -m "feat: add outbound worker daemon"
