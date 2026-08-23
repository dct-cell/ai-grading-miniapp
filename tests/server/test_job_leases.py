from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from server.domain.states import JobState, OrderState
from server.models import GradingJob, Worker
from server.services.leases import ACK_SECONDS, LEASE_SECONDS, LeaseService
from tests.server.conftest import (
    authenticate,
    create_quote,
    register_worker,
    worker_headers,
)


def count(session: Session, model) -> int:
    return session.scalar(select(func.count()).select_from(model))


def queue_order(client: TestClient, *, pages: int = 2, note: str = "") -> str:
    """Drive the verified Phase 02 intake path to produce one queued job."""
    quote = create_quote(client, pages=pages, note=note)
    prepay = client.post(
        "/api/v1/payments/prepay", json={"quote_id": quote["id"]}
    ).json()
    response = client.post(
        "/callbacks/fake/pay",
        json={"fake_transaction_id": prepay["prepay_id"], "status": "SUCCESS"},
    )
    assert response.status_code == 204, response.text
    return quote["id"]


@pytest.fixture
def worker_client(client: TestClient) -> TestClient:
    authenticate(client)
    return client


@pytest.fixture
def worker_id(worker_client: TestClient) -> str:
    return register_worker(worker_client, installation_id="install-lease-a")["worker_id"]


@pytest.fixture
def lease_service(session_factory: sessionmaker[Session]) -> LeaseService:
    return LeaseService(session_factory)


def lease(client: TestClient, worker_id: str, *, wait: int | None = 0):
    headers = worker_headers(worker_id)
    if wait is not None:
        headers["Prefer"] = f"wait={wait}"
    return client.post("/worker/v1/jobs/lease", headers=headers)


def test_lease_returns_204_when_no_job_is_queued(
    worker_client: TestClient,
    worker_id: str,
) -> None:
    response = lease(worker_client, worker_id)

    assert response.status_code == 204
    assert response.content == b""


def test_lease_returns_the_task_bundle_for_a_queued_job(
    worker_client: TestClient,
    worker_id: str,
    session_factory: sessionmaker[Session],
) -> None:
    queue_order(worker_client, pages=3, note="第二题请核对引理")

    response = lease(worker_client, worker_id)

    assert response.status_code == 200, response.text
    body = response.json()
    with session_factory() as session:
        job = session.scalars(select(GradingJob)).one()

    assert body["job_id"] == job.id
    assert body["order_id"] == job.order_id
    assert body["round_number"] == 1
    assert body["lease_version"] == 1
    assert body["grading_standard"] == "imo"
    assert body["note"] == "第二题请核对引理"
    assert body["page_count"] == 3
    assert body["source_file"]["download_token"]
    assert body["reference_file"] is None
    assert body["lease_seconds"] == LEASE_SECONDS
    assert body["ack_seconds"] == ACK_SECONDS


def test_lease_includes_the_reference_file_when_one_was_paid_for(
    worker_client: TestClient,
    worker_id: str,
) -> None:
    quote = create_quote(worker_client, reference_pages=1)
    prepay = worker_client.post(
        "/api/v1/payments/prepay", json={"quote_id": quote["id"]}
    ).json()
    worker_client.post(
        "/callbacks/fake/pay",
        json={"fake_transaction_id": prepay["prepay_id"], "status": "SUCCESS"},
    )

    body = lease(worker_client, worker_id).json()

    assert body["reference_file"] is not None
    assert body["reference_file"]["download_token"]
    assert body["source_file"]["file_id"] != body["reference_file"]["file_id"]


def test_lease_marks_the_job_leased_with_server_side_deadlines(
    worker_client: TestClient,
    worker_id: str,
    session_factory: sessionmaker[Session],
) -> None:
    queue_order(worker_client)

    lease(worker_client, worker_id)

    now = datetime.now(timezone.utc)
    with session_factory() as session:
        job = session.scalars(select(GradingJob)).one()
        worker = session.get(Worker, worker_id)

    assert job.state == JobState.LEASED
    assert job.worker_id == worker_id
    assert job.lease_version == 1
    assert job.attempt_count == 1
    assert abs((job.ack_deadline - (now + timedelta(seconds=ACK_SECONDS))).total_seconds()) < 5
    assert abs(
        (job.lease_expires_at - (now + timedelta(seconds=LEASE_SECONDS))).total_seconds()
    ) < 5
    assert worker.current_job_id == job.id


def test_lease_moves_the_order_into_running(
    worker_client: TestClient,
    worker_id: str,
    session_factory: sessionmaker[Session],
) -> None:
    queue_order(worker_client)

    lease(worker_client, worker_id)

    with session_factory() as session:
        from server.models import Order

        assert session.scalars(select(Order)).one().state == OrderState.V1_RUNNING


def test_worker_with_active_job_cannot_lease_second_job(
    worker_client: TestClient,
    worker_id: str,
) -> None:
    queue_order(worker_client)
    queue_order(worker_client)

    first = lease(worker_client, worker_id)
    second = lease(worker_client, worker_id)

    assert first.status_code == 200
    assert first.json()["job_id"] != ""
    assert second.status_code == 204


@pytest.mark.parametrize(
    "held_state",
    [JobState.LEASED, JobState.RUNNING, JobState.UPLOADING],
)
def test_a_worker_holding_any_active_state_cannot_lease_again(
    worker_client: TestClient,
    worker_id: str,
    session_factory: sessionmaker[Session],
    held_state: str,
) -> None:
    queue_order(worker_client)
    queue_order(worker_client)
    held_job_id = lease(worker_client, worker_id).json()["job_id"]
    with session_factory() as session:
        job = session.get(GradingJob, held_job_id)
        job.state = held_state
        session.add(job)
        session.commit()

    response = lease(worker_client, worker_id)

    assert response.status_code == 204
    with session_factory() as session:
        leased = session.scalars(
            select(GradingJob).where(GradingJob.worker_id == worker_id)
        ).all()
    assert len(leased) == 1


def test_two_workers_lease_two_distinct_jobs(
    worker_client: TestClient,
    session_factory: sessionmaker[Session],
) -> None:
    first_worker = register_worker(worker_client, installation_id="install-1")["worker_id"]
    second_worker = register_worker(worker_client, installation_id="install-2")["worker_id"]
    queue_order(worker_client)
    queue_order(worker_client)

    first = lease(worker_client, first_worker).json()
    second = lease(worker_client, second_worker).json()

    assert first["job_id"] != second["job_id"]
    assert first["lease_version"] == 1
    assert second["lease_version"] == 1
    with session_factory() as session:
        owners = {
            job.id: job.worker_id for job in session.scalars(select(GradingJob)).all()
        }
    assert owners[first["job_id"]] == first_worker
    assert owners[second["job_id"]] == second_worker


def test_a_third_worker_finds_no_job_when_the_queue_is_drained(
    worker_client: TestClient,
) -> None:
    workers = [
        register_worker(worker_client, installation_id=f"install-{index}")["worker_id"]
        for index in range(3)
    ]
    queue_order(worker_client)
    queue_order(worker_client)

    claims = [lease(worker_client, identifier) for identifier in workers]

    statuses = [response.status_code for response in claims]
    assert statuses.count(200) == 2
    assert statuses.count(204) == 1
    claimed = {
        response.json()["job_id"] for response in claims if response.status_code == 200
    }
    assert len(claimed) == 2


def test_claim_order_is_oldest_queued_first(
    worker_client: TestClient,
    session_factory: sessionmaker[Session],
) -> None:
    first_worker = register_worker(worker_client, installation_id="install-old")["worker_id"]
    queue_order(worker_client)
    queue_order(worker_client)
    with session_factory() as session:
        jobs = session.scalars(select(GradingJob)).all()
        oldest = jobs[0]
        oldest.queued_at = datetime.now(timezone.utc) - timedelta(hours=3)
        jobs[1].queued_at = datetime.now(timezone.utc)
        session.add_all(jobs)
        session.commit()
        oldest_id = oldest.id

    assert lease(worker_client, first_worker).json()["job_id"] == oldest_id


def test_lease_never_claims_a_job_that_is_not_queued(
    worker_client: TestClient,
    worker_id: str,
    session_factory: sessionmaker[Session],
) -> None:
    queue_order(worker_client)
    with session_factory() as session:
        job = session.scalars(select(GradingJob)).one()
        job.state = JobState.WORKER_EXCEPTION
        session.add(job)
        session.commit()

    assert lease(worker_client, worker_id).status_code == 204


def test_lease_requires_worker_authentication(client: TestClient) -> None:
    authenticate(client)
    register_worker(client)
    queue_order(client)

    assert client.post("/worker/v1/jobs/lease").status_code == 401


def test_miniapp_token_cannot_lease_a_job(client: TestClient) -> None:
    body = client.post("/api/v1/auth/login", json={"code": "test-parent-1"}).json()
    worker_id = register_worker(client)["worker_id"]

    response = client.post(
        "/worker/v1/jobs/lease",
        headers={
            "Authorization": f"Bearer {body['access_token']}",
            "X-Worker-ID": worker_id,
            "Prefer": "wait=0",
        },
    )

    assert response.status_code == 401


def test_disabled_worker_cannot_lease_a_job(
    worker_client: TestClient,
    worker_id: str,
    session_factory: sessionmaker[Session],
) -> None:
    queue_order(worker_client)
    with session_factory() as session:
        worker = session.get(Worker, worker_id)
        worker.status = "disabled"
        session.add(worker)
        session.commit()

    assert lease(worker_client, worker_id).status_code == 403
    with session_factory() as session:
        assert session.scalars(select(GradingJob)).one().state == JobState.QUEUED


def test_long_poll_is_capped_at_the_protocol_maximum(
    worker_client: TestClient,
    worker_id: str,
) -> None:
    from server.api import worker_jobs

    assert worker_jobs.MAX_LONG_POLL_SECONDS == 25
    assert worker_jobs.parse_wait_seconds("wait=0") == 0
    assert worker_jobs.parse_wait_seconds("wait=10") == 10
    assert worker_jobs.parse_wait_seconds("wait=600") == 25
    assert worker_jobs.parse_wait_seconds(None) == 25
    assert worker_jobs.parse_wait_seconds("garbage") == 25
    assert worker_jobs.parse_wait_seconds("wait=-5") == 0


def test_lease_version_increases_strictly_on_every_claim(
    lease_service: LeaseService,
    worker_client: TestClient,
    session_factory: sessionmaker[Session],
) -> None:
    """lease_version is the fencing token; a reclaim must invalidate the old one."""
    first_worker = register_worker(worker_client, installation_id="install-fence-1")[
        "worker_id"
    ]
    second_worker = register_worker(worker_client, installation_id="install-fence-2")[
        "worker_id"
    ]
    queue_order(worker_client)

    first = lease_service.try_lease(first_worker)
    lease_service.release_unacknowledged(
        now=datetime.now(timezone.utc) + timedelta(seconds=ACK_SECONDS + 1)
    )
    second = lease_service.try_lease(second_worker)

    assert first.lease_version == 1
    assert second.lease_version == 2
    assert second.job_id == first.job_id
    with session_factory() as session:
        assert session.get(GradingJob, first.job_id).lease_version == 2


def test_ack_timeout_returns_unstarted_job_to_queue(
    lease_service: LeaseService,
    worker_client: TestClient,
    worker_id: str,
    session_factory: sessionmaker[Session],
) -> None:
    queue_order(worker_client)
    leased = lease_service.try_lease(worker_id)

    lease_service.release_unacknowledged(
        now=datetime.now(timezone.utc) + timedelta(seconds=ACK_SECONDS + 1)
    )

    with session_factory() as session:
        job = session.get(GradingJob, leased.job_id)
        worker = session.get(Worker, worker_id)
    assert job.state == JobState.QUEUED
    assert job.worker_id is None
    assert job.ack_deadline is None
    assert job.lease_expires_at is None
    assert worker.current_job_id is None


def test_ack_timeout_does_not_touch_a_job_inside_its_ack_window(
    lease_service: LeaseService,
    worker_client: TestClient,
    worker_id: str,
    session_factory: sessionmaker[Session],
) -> None:
    queue_order(worker_client)
    leased = lease_service.try_lease(worker_id)

    lease_service.release_unacknowledged(
        now=datetime.now(timezone.utc) + timedelta(seconds=ACK_SECONDS - 5)
    )

    with session_factory() as session:
        assert session.get(GradingJob, leased.job_id).state == JobState.LEASED


def test_try_lease_returns_none_when_the_queue_is_empty(
    lease_service: LeaseService,
    worker_id: str,
) -> None:
    assert lease_service.try_lease(worker_id) is None


def test_claim_uses_a_row_lock_that_skips_locked_rows() -> None:
    """The MySQL queue claim must not serialise every polling Worker."""
    import inspect as inspect_module

    from server.services import leases

    source = inspect_module.getsource(leases.LeaseService._claim_statement)

    assert "with_for_update" in source
    assert "skip_locked=True" in source
