"""Regression tests for defects found in the Phase 03 security review."""

from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from server.domain.states import JobState, OrderState
from server.models import FileObject, GradingJob, Order, Worker
from server.services.leases import LeaseService
from server.services.results import (
    RESULT_STAGING_DIRECTORY,
    ResultKind,
    UploadNotAuthorized,
    _decode_upload_token,
    issue_upload_token,
)
from tests.server.conftest import authenticate, register_worker, worker_headers
from tests.server.test_job_leases import queue_order
from tests.server.test_worker_results import RESULT_JSON, upload, upload_both


SECRET = "s" * 32


@pytest.fixture
def worker_client(client: TestClient) -> TestClient:
    authenticate(client)
    return client


@pytest.fixture
def worker_a(worker_client: TestClient) -> str:
    return register_worker(worker_client, installation_id="install-review-a")["worker_id"]


@pytest.fixture
def lease_service(session_factory: sessionmaker[Session]) -> LeaseService:
    return LeaseService(session_factory)


@pytest.mark.parametrize("suffix", ["é", "中", "\u200b"])
def test_a_non_ascii_padded_upload_token_is_rejected(suffix: str) -> None:
    """The token must be canonical, or the same grant has many valid strings.

    Dropping undecodable characters before the HMAC check makes the signature
    malleable: any future revocation list or audit keyed on the token string
    could be bypassed by appending padding.
    """
    token = issue_upload_token(
        job_id="job-1",
        worker_id="worker-1",
        lease_version=1,
        kind=ResultKind.JSON,
        max_bytes=1024,
        secret=SECRET,
    )
    payload, _, signature = token.rpartition(".")

    with pytest.raises(UploadNotAuthorized):
        _decode_upload_token(f"{payload}{suffix}.{signature}", SECRET)


def test_a_canonical_upload_token_still_decodes() -> None:
    token = issue_upload_token(
        job_id="job-1",
        worker_id="worker-1",
        lease_version=3,
        kind=ResultKind.PDF,
        max_bytes=2048,
        secret=SECRET,
    )

    claims = _decode_upload_token(token, SECRET)

    assert claims["job_id"] == "job-1"
    assert claims["lease_version"] == 3
    assert claims["kind"] == str(ResultKind.PDF)
    assert claims["max_bytes"] == 2048


def test_registration_surfaces_an_unrelated_integrity_error(
    client: TestClient,
) -> None:
    """An IntegrityError unrelated to installation_id must not become a 500.

    The recovery path only makes sense when a concurrent registration won the
    unique constraint. With no winner to adopt, the original error has to
    propagate; a bare `raise` outside the except block would instead produce
    RuntimeError('No active exception to reraise').
    """
    from sqlalchemy.exc import IntegrityError

    from server.services import workers as worker_service

    request = worker_service.RegistrationRequest(
        installation_id="install-broken",
        device_name="d",
        platform="linux",
        architecture="x86_64",
        worker_version="3.0.0",
    )

    with client.app.state.session_factory() as session:
        # A violation on some other constraint surfaces when the savepoint
        # flushes, and no winner row will ever exist for it.
        def failing_flush(*args, **kwargs) -> None:
            raise IntegrityError("insert", {}, Exception("capabilities cannot be null"))

        session.flush = failing_flush
        with pytest.raises(IntegrityError):
            worker_service.register_worker(session, request)


def test_requeueing_never_overwrites_a_job_that_just_acked(
    lease_service: LeaseService,
    worker_client: TestClient,
    worker_a: str,
    session_factory: sessionmaker[Session],
) -> None:
    """A job that ACKed while the recycler was deciding must stay RUNNING.

    The recycler must re-check state under a lock; otherwise it writes QUEUED
    over a started job and the work runs twice.
    """
    queue_order(worker_client)
    leased = lease_service.try_lease(worker_a)
    expired = datetime.now(timezone.utc) + timedelta(seconds=31)

    # Interleave deterministically: the job reaches RUNNING after the recycler
    # would have read it as LEASED but before it writes.
    lease_service.acknowledge(
        job_id=leased.job_id, worker_id=worker_a, lease_version=leased.lease_version
    )
    lease_service.release_unacknowledged(now=expired)

    with session_factory() as session:
        job = session.get(GradingJob, leased.job_id)
    assert job.state == JobState.RUNNING
    assert job.worker_id == worker_a


def test_expiry_never_overwrites_an_already_delivered_job(
    lease_service: LeaseService,
    worker_client: TestClient,
    worker_a: str,
    session_factory: sessionmaker[Session],
) -> None:
    """A delivered order must never be marked WORKER_EXCEPTION afterwards."""
    queue_order(worker_client)
    leased = worker_client.post(
        "/worker/v1/jobs/lease",
        headers={**worker_headers(worker_a), "Prefer": "wait=0"},
    ).json()
    worker_client.post(
        f"/worker/v1/jobs/{leased['job_id']}/ack",
        json={"lease_version": leased["lease_version"]},
        headers=worker_headers(worker_a),
    )
    tokens = worker_client.post(
        f"/worker/v1/jobs/{leased['job_id']}/result/uploads",
        json={"lease_version": leased["lease_version"]},
        headers=worker_headers(worker_a),
    ).json()
    job = {**leased, "tokens": tokens}
    files = upload_both(worker_client, worker_a, job)
    assert (
        worker_client.post(
            f"/worker/v1/jobs/{job['job_id']}/result/commit",
            json={"lease_version": job["lease_version"], **files},
            headers=worker_headers(worker_a),
        ).status_code
        == 200
    )

    # The lease clock has long since passed; expiry must ignore a finished job.
    lease_service.expire_started_leases(
        now=datetime.now(timezone.utc) + timedelta(hours=1)
    )

    with session_factory() as session:
        stored_job = session.get(GradingJob, job["job_id"])
        order = session.get(Order, job["order_id"])
    assert stored_job.state == JobState.SUCCEEDED
    assert order.state == OrderState.V1_DELIVERED


def test_the_recyclers_lock_the_rows_they_rewrite() -> None:
    """Both recyclers must lock candidate rows on a backend that supports it.

    Asserted by compiling the statement they issue for MySQL rather than by
    grepping the source, so the check tracks real emitted SQL.
    """
    from sqlalchemy import select
    from sqlalchemy.dialects import mysql

    from server.services.leases import STARTED_JOB_STATES, LeaseService

    class FakeBind:
        dialect = mysql.dialect()

    class FakeSession:
        def __init__(self) -> None:
            self.compiled: str | None = None

        def get_bind(self):
            return FakeBind()

        def scalars(self, statement):
            self.compiled = str(statement.compile(dialect=mysql.dialect()))

            class Empty:
                @staticmethod
                def all():
                    return []

            return Empty()

    for statement in (
        select(GradingJob).where(GradingJob.state == JobState.LEASED),
        select(GradingJob).where(GradingJob.state.in_(STARTED_JOB_STATES)),
    ):
        session = FakeSession()
        LeaseService(lambda: None)._locked_candidates(session, statement)
        assert "FOR UPDATE" in session.compiled


def test_the_recyclers_skip_a_row_whose_state_changed_under_them(
    lease_service: LeaseService,
    worker_client: TestClient,
    worker_a: str,
    session_factory: sessionmaker[Session],
) -> None:
    """Re-checking under the lock is what prevents the blind overwrite.

    The candidate query is forced to return a job that no longer qualifies,
    which is exactly what a concurrent ACK or commit produces.
    """
    queue_order(worker_client)
    leased = lease_service.try_lease(worker_a)
    lease_service.acknowledge(
        job_id=leased.job_id, worker_id=worker_a, lease_version=leased.lease_version
    )

    def stale_candidates(self, session, statement):
        return session.scalars(select(GradingJob)).all()

    original = LeaseService._locked_candidates
    LeaseService._locked_candidates = stale_candidates
    try:
        released = lease_service.release_unacknowledged(
            now=datetime.now(timezone.utc) + timedelta(hours=1)
        )
    finally:
        LeaseService._locked_candidates = original

    assert released == 0
    with session_factory() as session:
        assert session.get(GradingJob, leased.job_id).state == JobState.RUNNING


def test_a_staged_result_path_is_unique_in_the_database(
    worker_client: TestClient,
    worker_a: str,
    session_factory: sessionmaker[Session],
) -> None:
    """A database constraint, not just a pre-check, enforces single use.

    The check-then-insert in stage_upload is a TOCTOU window on MySQL, so the
    schema has to refuse a second row for the same stored object.
    """
    from sqlalchemy.exc import IntegrityError

    queue_order(worker_client)
    leased = worker_client.post(
        "/worker/v1/jobs/lease",
        headers={**worker_headers(worker_a), "Prefer": "wait=0"},
    ).json()

    with session_factory() as session:
        existing = session.scalars(select(FileObject)).first()
        duplicate = FileObject(
            owner_user_id=existing.owner_user_id,
            kind=existing.kind,
            relative_path=existing.relative_path,
            sha256=existing.sha256,
            size_bytes=existing.size_bytes,
            state=existing.state,
            expires_at=existing.expires_at,
        )
        session.add(duplicate)
        with pytest.raises(IntegrityError):
            session.commit()
    del leased


def test_a_crashed_commit_keeps_the_staged_source_recoverable(
    worker_client: TestClient,
    worker_a: str,
    session_factory: sessionmaker[Session],
    settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed commit must leave the staged bytes so the Worker can retry.

    Relocating the staged file before the transaction commits destroys the only
    copy: if the commit then fails, the retry has nothing left to deliver and
    the job is stuck in UPLOADING forever.
    """
    from sqlalchemy.exc import OperationalError
    from sqlalchemy.orm import Session as SqlSession

    leased = worker_client.post(
        "/worker/v1/jobs/lease",
        headers={**worker_headers(worker_a), "Prefer": "wait=0"},
    )
    queue_order(worker_client)
    leased = worker_client.post(
        "/worker/v1/jobs/lease",
        headers={**worker_headers(worker_a), "Prefer": "wait=0"},
    ).json()
    worker_client.post(
        f"/worker/v1/jobs/{leased['job_id']}/ack",
        json={"lease_version": leased["lease_version"]},
        headers=worker_headers(worker_a),
    )
    tokens = worker_client.post(
        f"/worker/v1/jobs/{leased['job_id']}/result/uploads",
        json={"lease_version": leased["lease_version"]},
        headers=worker_headers(worker_a),
    ).json()
    job = {**leased, "tokens": tokens}
    files = upload_both(worker_client, worker_a, job)
    with session_factory() as session:
        staged_paths = [
            session.get(FileObject, files[key]).relative_path
            for key in ("result_json_file_id", "result_pdf_file_id")
        ]

    original_commit = SqlSession.commit

    def failing_commit(self) -> None:
        raise OperationalError("commit", {}, Exception("database is locked"))

    monkeypatch.setattr(SqlSession, "commit", failing_commit)
    response = worker_client.post(
        f"/worker/v1/jobs/{job['job_id']}/result/commit",
        json={"lease_version": job["lease_version"], **files},
        headers=worker_headers(worker_a),
    )
    monkeypatch.setattr(SqlSession, "commit", original_commit)
    assert response.status_code >= 400

    # The staged bytes survive, so the retry below can still deliver them.
    for relative_path in staged_paths:
        assert (settings.data_dir / relative_path).is_file()
        assert RESULT_STAGING_DIRECTORY in relative_path

    retry = worker_client.post(
        f"/worker/v1/jobs/{job['job_id']}/result/commit",
        json={"lease_version": job["lease_version"], **files},
        headers=worker_headers(worker_a),
    )

    assert retry.status_code == 200, retry.text
    assert retry.json()["status"] == "committed"
    with session_factory() as session:
        assert session.get(Order, job["order_id"]).state == OrderState.V1_DELIVERED
        for key in ("result_json_file_id", "result_pdf_file_id"):
            record = session.get(FileObject, files[key])
            assert (settings.data_dir / record.relative_path).is_file()
    staging = settings.data_dir / RESULT_STAGING_DIRECTORY
    assert not any(path.is_file() for path in staging.rglob("*"))
