from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from server.domain.states import JobState, OrderState
from server.models import FileObject, GradingJob, GradingRound, Order, Worker
from server.services.results import RESULT_STAGING_DIRECTORY, ResultKind
from tests.server.conftest import (
    authenticate,
    make_pdf_bytes,
    register_worker,
    result_json_bytes_for_job,
    worker_headers,
)
from tests.server.test_job_leases import queue_order


RESULT_JSON = b'{"score": 21, "problems": []}'


def count(session: Session, model) -> int:
    return session.scalar(select(func.count()).select_from(model))


@pytest.fixture
def worker_client(client: TestClient) -> TestClient:
    authenticate(client)
    return client


@pytest.fixture
def worker_a(worker_client: TestClient) -> str:
    return register_worker(worker_client, installation_id="install-result-a")["worker_id"]


@pytest.fixture
def worker_b(worker_client: TestClient) -> str:
    return register_worker(worker_client, installation_id="install-result-b")["worker_id"]


@pytest.fixture
def uploading(worker_client: TestClient, worker_a: str) -> dict:
    """A job owned by worker_a that has reached UPLOADING with fresh tokens."""
    queue_order(worker_client, pages=2)
    leased = worker_client.post(
        "/worker/v1/jobs/lease",
        headers={**worker_headers(worker_a), "Prefer": "wait=0"},
    ).json()
    assert (
        worker_client.post(
            f"/worker/v1/jobs/{leased['job_id']}/ack",
            json={"lease_version": leased["lease_version"]},
            headers=worker_headers(worker_a),
        ).status_code
        == 200
    )
    tokens = worker_client.post(
        f"/worker/v1/jobs/{leased['job_id']}/result/uploads",
        json={"lease_version": leased["lease_version"]},
        headers=worker_headers(worker_a),
    )
    assert tokens.status_code == 200, tokens.text
    return {**leased, "tokens": tokens.json()}


def upload(
    client: TestClient,
    worker_id: str,
    job: dict,
    kind: str,
    payload: bytes,
    *,
    token: str | None = None,
    sha256: str | None = None,
):
    used_token = token or job["tokens"][kind]["upload_token"]
    digest = sha256 or hashlib.sha256(payload).hexdigest()
    return client.put(
        f"/worker/v1/jobs/{job['job_id']}/result/{kind}",
        content=payload,
        headers={
            **worker_headers(worker_id),
            "X-Upload-Token": used_token,
            "X-Content-SHA256": digest,
            "Content-Type": "application/octet-stream",
        },
    )


def upload_both(client: TestClient, worker_id: str, job: dict) -> dict:
    result_json = result_json_bytes_for_job(job)
    json_body = upload(client, worker_id, job, ResultKind.JSON, result_json)
    result_pages = 1 if job["service_tier"] == "summary_report" else job["page_count"] + 1
    pdf_body = upload(
        client, worker_id, job, ResultKind.PDF, make_pdf_bytes(result_pages)
    )
    assert json_body.status_code == 201, json_body.text
    assert pdf_body.status_code == 201, pdf_body.text
    return {
        "result_json_file_id": json_body.json()["file_id"],
        "result_pdf_file_id": pdf_body.json()["file_id"],
    }


def commit(
    client: TestClient,
    worker_id: str,
    job: dict,
    files: dict,
    *,
    lease_version: int | None = None,
):
    return client.post(
        f"/worker/v1/jobs/{job['job_id']}/result/commit",
        json={
            "lease_version": job["lease_version"] if lease_version is None else lease_version,
            **files,
        },
        headers=worker_headers(worker_id),
    )


def test_requesting_upload_tokens_moves_the_job_to_uploading(
    worker_client: TestClient,
    worker_a: str,
    uploading: dict,
    session_factory: sessionmaker[Session],
) -> None:
    with session_factory() as session:
        assert session.get(GradingJob, uploading["job_id"]).state == JobState.UPLOADING


def test_upload_tokens_are_bound_to_kind_and_size(uploading: dict) -> None:
    """Each grant is bound to the job, holder, fence, kind and size limit."""
    from server.services.results import _decode_upload_token

    tokens = uploading["tokens"]
    assert set(tokens) == {ResultKind.JSON, ResultKind.PDF}
    assert tokens[ResultKind.JSON]["upload_token"] != tokens[ResultKind.PDF]["upload_token"]

    for kind in (ResultKind.JSON, ResultKind.PDF):
        claims = _decode_upload_token(tokens[kind]["upload_token"], "s" * 32)
        assert claims["job_id"] == uploading["job_id"]
        assert claims["lease_version"] == uploading["lease_version"]
        assert claims["kind"] == str(kind)
        assert claims["max_bytes"] == tokens[kind]["max_bytes"] > 0
        assert claims["worker_id"]


def test_a_token_cannot_be_replayed(
    worker_client: TestClient,
    worker_a: str,
    uploading: dict,
    session_factory: sessionmaker[Session],
) -> None:
    """Upload tokens are single-use."""
    first = upload(worker_client, worker_a, uploading, ResultKind.JSON, RESULT_JSON)
    assert first.status_code == 201

    replay = upload(worker_client, worker_a, uploading, ResultKind.JSON, RESULT_JSON)

    assert replay.status_code == 409
    with session_factory() as session:
        staged = session.scalars(
            select(FileObject).where(FileObject.kind == ResultKind.JSON)
        ).all()
    assert len(staged) == 1


def test_a_token_for_one_kind_cannot_upload_another(
    worker_client: TestClient,
    worker_a: str,
    uploading: dict,
) -> None:
    response = upload(
        worker_client,
        worker_a,
        uploading,
        ResultKind.PDF,
        make_pdf_bytes(3),
        token=uploading["tokens"][ResultKind.JSON]["upload_token"],
    )

    assert response.status_code == 403


def test_another_workers_token_is_refused(
    worker_client: TestClient,
    worker_b: str,
    uploading: dict,
    session_factory: sessionmaker[Session],
) -> None:
    """The token is bound to its holder, so worker_b cannot redeem it.

    403 specifically: the token itself fails its worker binding, rather than
    merely losing a lease check.
    """
    response = upload(worker_client, worker_b, uploading, ResultKind.JSON, RESULT_JSON)

    assert response.status_code == 403
    with session_factory() as session:
        assert count(session, FileObject) == 1  # only the paid source PDF


def test_a_forged_token_is_refused(
    worker_client: TestClient,
    worker_a: str,
    uploading: dict,
) -> None:
    response = upload(
        worker_client,
        worker_a,
        uploading,
        ResultKind.JSON,
        RESULT_JSON,
        token="forged.upload.token",
    )

    assert response.status_code == 403


def test_a_tampered_token_payload_is_refused(
    worker_client: TestClient,
    worker_a: str,
    uploading: dict,
) -> None:
    original = uploading["tokens"][ResultKind.JSON]["upload_token"]
    payload, _, signature = original.rpartition(".")

    response = upload(
        worker_client,
        worker_a,
        uploading,
        ResultKind.JSON,
        RESULT_JSON,
        token=f"{payload}x.{signature}",
    )

    assert response.status_code == 403


def test_a_sha256_mismatch_leaves_no_row_and_no_file(
    worker_client: TestClient,
    worker_a: str,
    uploading: dict,
    session_factory: sessionmaker[Session],
    settings,
) -> None:
    response = upload(
        worker_client,
        worker_a,
        uploading,
        ResultKind.JSON,
        RESULT_JSON,
        sha256="0" * 64,
    )

    assert response.status_code == 400
    with session_factory() as session:
        assert count(session, FileObject) == 1
    staging = settings.data_dir / RESULT_STAGING_DIRECTORY
    assert not any(path.is_file() for path in staging.rglob("*"))


def test_a_rejected_upload_can_be_retried_with_the_correct_digest(
    worker_client: TestClient,
    worker_a: str,
    uploading: dict,
) -> None:
    """A digest mismatch must not burn the token; the Worker retries."""
    assert (
        upload(
            worker_client,
            worker_a,
            uploading,
            ResultKind.JSON,
            RESULT_JSON,
            sha256="0" * 64,
        ).status_code
        == 400
    )

    retry = upload(worker_client, worker_a, uploading, ResultKind.JSON, RESULT_JSON)

    assert retry.status_code == 201


def test_an_unreadable_result_pdf_is_rejected(
    worker_client: TestClient,
    worker_a: str,
    uploading: dict,
    session_factory: sessionmaker[Session],
    settings,
) -> None:
    response = upload(
        worker_client, worker_a, uploading, ResultKind.PDF, b"%PDF-1.7truncated"
    )

    assert response.status_code == 400
    with session_factory() as session:
        assert count(session, FileObject) == 1
    staging = settings.data_dir / RESULT_STAGING_DIRECTORY
    assert not any(path.is_file() for path in staging.rglob("*"))


def test_a_payload_over_the_token_limit_is_rejected(
    worker_client: TestClient,
    worker_a: str,
    uploading: dict,
    session_factory: sessionmaker[Session],
) -> None:
    limit = uploading["tokens"][ResultKind.JSON]["max_bytes"]

    response = upload(
        worker_client, worker_a, uploading, ResultKind.JSON, b"0" * (limit + 1)
    )

    assert response.status_code == 400
    with session_factory() as session:
        assert count(session, FileObject) == 1


def test_staged_uploads_land_under_the_job_and_lease_version(
    worker_client: TestClient,
    worker_a: str,
    uploading: dict,
    session_factory: sessionmaker[Session],
    settings,
) -> None:
    files = upload_both(worker_client, worker_a, uploading)

    with session_factory() as session:
        staged = session.get(FileObject, files["result_json_file_id"])
    expected_prefix = (
        f"{RESULT_STAGING_DIRECTORY}/{uploading['job_id']}/{uploading['lease_version']}/"
    )
    assert staged.relative_path.startswith(expected_prefix)
    assert (settings.data_dir / staged.relative_path).is_file()
    expected_json = result_json_bytes_for_job(uploading)
    assert staged.sha256 == hashlib.sha256(expected_json).hexdigest()
    assert staged.size_bytes == len(expected_json)


def test_commit_delivers_the_order_and_binds_the_result_files(
    worker_client: TestClient,
    worker_a: str,
    uploading: dict,
    session_factory: sessionmaker[Session],
    settings,
) -> None:
    files = upload_both(worker_client, worker_a, uploading)

    response = commit(worker_client, worker_a, uploading, files)

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "committed"
    now = datetime.now(timezone.utc)
    with session_factory() as session:
        job = session.get(GradingJob, uploading["job_id"])
        order = session.get(Order, uploading["order_id"])
        round_record = session.scalars(select(GradingRound)).one()
        worker = session.get(Worker, worker_a)
        json_file = session.get(FileObject, files["result_json_file_id"])
        pdf_file = session.get(FileObject, files["result_pdf_file_id"])

    assert job.state == JobState.SUCCEEDED
    assert order.state == OrderState.V1_DELIVERED
    assert round_record.result_json_file_id == files["result_json_file_id"]
    assert round_record.result_pdf_file_id == files["result_pdf_file_id"]
    assert round_record.delivered_at is not None
    expected_deadline = now + timedelta(seconds=settings.acceptance_ttl_seconds)
    assert abs((order.acceptance_deadline - expected_deadline).total_seconds()) < 10
    assert worker.current_job_id is None
    for record in (json_file, pdf_file):
        assert record.relative_path.startswith(f"orders/")
        assert RESULT_STAGING_DIRECTORY not in record.relative_path
        assert (settings.data_dir / record.relative_path).is_file()


def test_committed_files_live_under_the_order_and_round(
    worker_client: TestClient,
    worker_a: str,
    uploading: dict,
    session_factory: sessionmaker[Session],
) -> None:
    files = upload_both(worker_client, worker_a, uploading)
    commit(worker_client, worker_a, uploading, files)

    with session_factory() as session:
        record = session.get(FileObject, files["result_pdf_file_id"])

    parts = record.relative_path.split("/")
    assert parts[0] == "orders"
    assert len(parts[1]) == 4 and parts[1].isdigit()
    assert len(parts[2]) == 2 and parts[2].isdigit()
    assert parts[3] == uploading["order_id"]
    assert parts[4] == str(uploading["round_number"])


def test_commit_moves_the_bytes_and_leaves_no_staging_file(
    worker_client: TestClient,
    worker_a: str,
    uploading: dict,
    settings,
) -> None:
    files = upload_both(worker_client, worker_a, uploading)

    commit(worker_client, worker_a, uploading, files)

    staging = settings.data_dir / RESULT_STAGING_DIRECTORY
    assert not any(path.is_file() for path in staging.rglob("*"))
    del files


def test_duplicate_commit_is_idempotent(
    worker_client: TestClient,
    worker_a: str,
    uploading: dict,
    session_factory: sessionmaker[Session],
) -> None:
    files = upload_both(worker_client, worker_a, uploading)
    first = commit(worker_client, worker_a, uploading, files)
    assert first.status_code == 200

    response = commit(worker_client, worker_a, uploading, files)

    assert response.status_code == 200
    assert response.json()["status"] == "already_committed"
    with session_factory() as session:
        round_record = session.scalars(select(GradingRound)).one()
        order = session.get(Order, uploading["order_id"])
        delivered_files = session.scalars(
            select(FileObject).where(
                FileObject.kind.in_({ResultKind.JSON, ResultKind.PDF})
            )
        ).all()
    assert order.state == OrderState.V1_DELIVERED
    assert round_record.result_json_file_id == files["result_json_file_id"]
    assert len(delivered_files) == 2


def test_expired_lease_cannot_commit_result(
    worker_client: TestClient,
    worker_a: str,
    uploading: dict,
    session_factory: sessionmaker[Session],
) -> None:
    files = upload_both(worker_client, worker_a, uploading)

    response = commit(
        worker_client,
        worker_a,
        uploading,
        files,
        lease_version=uploading["lease_version"] + 1,
    )

    assert response.status_code == 409
    with session_factory() as session:
        assert session.get(GradingJob, uploading["job_id"]).state == JobState.UPLOADING
        assert session.scalars(select(GradingRound)).one().delivered_at is None


def test_a_stale_lease_version_cannot_commit(
    worker_client: TestClient,
    worker_a: str,
    uploading: dict,
    session_factory: sessionmaker[Session],
) -> None:
    files = upload_both(worker_client, worker_a, uploading)

    response = commit(
        worker_client,
        worker_a,
        uploading,
        files,
        lease_version=uploading["lease_version"] - 1,
    )

    assert response.status_code == 409
    with session_factory() as session:
        assert session.get(Order, uploading["order_id"]).state == OrderState.V1_RUNNING


def test_another_worker_cannot_commit_someone_elses_job(
    worker_client: TestClient,
    worker_a: str,
    worker_b: str,
    uploading: dict,
    session_factory: sessionmaker[Session],
) -> None:
    files = upload_both(worker_client, worker_a, uploading)

    response = commit(worker_client, worker_b, uploading, files)

    assert response.status_code == 409
    with session_factory() as session:
        assert session.get(GradingJob, uploading["job_id"]).state == JobState.UPLOADING
        assert session.scalars(select(GradingRound)).one().result_pdf_file_id is None


def test_commit_rejects_a_file_staged_for_another_lease_version(
    worker_client: TestClient,
    worker_a: str,
    uploading: dict,
    session_factory: sessionmaker[Session],
) -> None:
    """A result produced under an older fence must not be delivered."""
    files = upload_both(worker_client, worker_a, uploading)
    with session_factory() as session:
        record = session.get(FileObject, files["result_pdf_file_id"])
        record.relative_path = record.relative_path.replace(
            f"/{uploading['lease_version']}/", "/99/"
        )
        session.add(record)
        session.commit()

    response = commit(worker_client, worker_a, uploading, files)

    assert response.status_code == 409
    with session_factory() as session:
        assert session.scalars(select(GradingRound)).one().delivered_at is None


def test_commit_rejects_an_unknown_file_id(
    worker_client: TestClient,
    worker_a: str,
    uploading: dict,
    session_factory: sessionmaker[Session],
) -> None:
    files = upload_both(worker_client, worker_a, uploading)
    files["result_json_file_id"] = "00000000-0000-0000-0000-000000000000"

    response = commit(worker_client, worker_a, uploading, files)

    assert response.status_code == 409
    with session_factory() as session:
        assert session.get(GradingJob, uploading["job_id"]).state == JobState.UPLOADING


def test_commit_without_uploads_is_refused(
    worker_client: TestClient,
    worker_a: str,
    uploading: dict,
    session_factory: sessionmaker[Session],
) -> None:
    response = commit(
        worker_client,
        worker_a,
        uploading,
        {
            "result_json_file_id": "00000000-0000-0000-0000-000000000001",
            "result_pdf_file_id": "00000000-0000-0000-0000-000000000002",
        },
    )

    assert response.status_code == 409
    with session_factory() as session:
        assert session.scalars(select(GradingRound)).one().delivered_at is None


def test_a_failed_commit_leaves_no_half_state_and_no_orphan_final_file(
    worker_client: TestClient,
    worker_a: str,
    uploading: dict,
    session_factory: sessionmaker[Session],
    settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A rolled-back commit must leave neither database nor disk changed.

    The move happens inside the commit path, so if the transaction fails the
    service has to undo the move as well; otherwise a FileObject row would name
    a final path that no committed order ever references.
    """
    from sqlalchemy.exc import OperationalError
    from sqlalchemy.orm import Session as SqlSession

    files = upload_both(worker_client, worker_a, uploading)
    with session_factory() as session:
        before_paths = {
            record.id: record.relative_path
            for record in session.scalars(select(FileObject)).all()
        }
    original_commit = SqlSession.commit
    calls = {"count": 0}

    def failing_commit(self) -> None:
        calls["count"] += 1
        raise OperationalError("commit", {}, Exception("database is locked"))

    monkeypatch.setattr(SqlSession, "commit", failing_commit)
    response = commit(worker_client, worker_a, uploading, files)
    monkeypatch.setattr(SqlSession, "commit", original_commit)

    assert calls["count"] >= 1
    assert response.status_code >= 400
    with session_factory() as session:
        job = session.get(GradingJob, uploading["job_id"])
        order = session.get(Order, uploading["order_id"])
        round_record = session.scalars(select(GradingRound)).one()
        after_paths = {
            record.id: record.relative_path
            for record in session.scalars(select(FileObject)).all()
        }

    assert job.state == JobState.UPLOADING
    assert order.state == OrderState.V1_RUNNING
    assert order.acceptance_deadline is None
    assert round_record.delivered_at is None
    assert round_record.result_json_file_id is None
    assert round_record.result_pdf_file_id is None
    assert after_paths == before_paths
    # No final artefact may survive without a committed order behind it.
    orders_root = settings.data_dir / "orders"
    assert not any(path.is_file() for path in orders_root.rglob("*"))
    for relative_path in before_paths.values():
        assert (settings.data_dir / relative_path).is_file()


def test_the_order_is_only_user_visible_after_the_commit_succeeds(
    worker_client: TestClient,
    worker_a: str,
    uploading: dict,
) -> None:
    files = upload_both(worker_client, worker_a, uploading)
    before = worker_client.get(f"/api/v1/orders/{uploading['order_id']}").json()

    commit(worker_client, worker_a, uploading, files)
    after = worker_client.get(f"/api/v1/orders/{uploading['order_id']}").json()

    assert before["state"] == OrderState.V1_RUNNING
    assert after["state"] == OrderState.V1_DELIVERED


def test_commit_records_a_worker_event(
    worker_client: TestClient,
    worker_a: str,
    uploading: dict,
    session_factory: sessionmaker[Session],
) -> None:
    from server.models import WorkerEvent

    files = upload_both(worker_client, worker_a, uploading)
    commit(worker_client, worker_a, uploading, files)

    with session_factory() as session:
        events = session.scalars(
            select(WorkerEvent).where(WorkerEvent.job_id == uploading["job_id"])
        ).all()

    assert "result_committed" in {event.event_type for event in events}


def test_result_routes_require_worker_authentication(
    worker_client: TestClient,
    uploading: dict,
) -> None:
    job_id = uploading["job_id"]

    tokens = worker_client.post(
        f"/worker/v1/jobs/{job_id}/result/uploads", json={"lease_version": 1}
    )
    committed = worker_client.post(
        f"/worker/v1/jobs/{job_id}/result/commit",
        json={
            "lease_version": 1,
            "result_json_file_id": "a",
            "result_pdf_file_id": "b",
        },
    )

    assert tokens.status_code == 401
    assert committed.status_code == 401


def test_upload_tokens_are_not_returned_to_the_mini_program(
    worker_client: TestClient,
    worker_a: str,
    uploading: dict,
) -> None:
    """Task tokens must never leak through a user-facing response."""
    detail = worker_client.get(f"/api/v1/orders/{uploading['order_id']}")

    assert detail.status_code == 200
    for kind in (ResultKind.JSON, ResultKind.PDF):
        assert uploading["tokens"][kind]["upload_token"] not in detail.text


def test_upload_token_errors_never_echo_the_signing_secret(
    worker_client: TestClient,
    worker_a: str,
    uploading: dict,
    settings,
) -> None:
    response = upload(
        worker_client, worker_a, uploading, ResultKind.JSON, RESULT_JSON, token="bad.token"
    )

    assert response.status_code == 403
    assert settings.session_secret not in response.text
    assert settings.worker_shared_key not in response.text


def test_a_leased_but_unacked_job_cannot_request_upload_tokens(
    worker_client: TestClient,
    worker_a: str,
) -> None:
    queue_order(worker_client)
    leased = worker_client.post(
        "/worker/v1/jobs/lease",
        headers={**worker_headers(worker_a), "Prefer": "wait=0"},
    ).json()

    response = worker_client.post(
        f"/worker/v1/jobs/{leased['job_id']}/result/uploads",
        json={"lease_version": leased["lease_version"]},
        headers=worker_headers(worker_a),
    )

    assert response.status_code == 409
