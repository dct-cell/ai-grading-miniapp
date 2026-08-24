from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from server.domain.states import JobState, OrderState
from server.models import GradingJob, Order, Worker, WorkerEvent
from server.services.leases import LeaseService
from server.services.workers import (
    ACK_SECONDS,
    LEASE_SECONDS,
    OFFLINE_AFTER_SECONDS,
    WorkerStatus,
)
from tests.server.conftest import authenticate, register_worker, worker_headers
from tests.server.test_job_leases import queue_order


@pytest.fixture
def worker_client(client: TestClient) -> TestClient:
    authenticate(client)
    return client


@pytest.fixture
def worker_a(worker_client: TestClient) -> str:
    return register_worker(worker_client, installation_id="install-renew-a")["worker_id"]


@pytest.fixture
def worker_b(worker_client: TestClient) -> str:
    return register_worker(worker_client, installation_id="install-renew-b")["worker_id"]


@pytest.fixture
def lease_service(session_factory: sessionmaker[Session]) -> LeaseService:
    return LeaseService(session_factory)


@pytest.fixture
def leased(worker_client: TestClient, worker_a: str) -> dict:
    queue_order(worker_client)
    response = worker_client.post(
        "/worker/v1/jobs/lease", headers={**worker_headers(worker_a), "Prefer": "wait=0"}
    )
    assert response.status_code == 200, response.text
    return response.json()


def ack(client: TestClient, worker_id: str, job_id: str, lease_version: int):
    return client.post(
        f"/worker/v1/jobs/{job_id}/ack",
        json={"lease_version": lease_version},
        headers=worker_headers(worker_id),
    )


def renew(
    client: TestClient,
    worker_id: str,
    job_id: str,
    lease_version: int,
    phase: str = "grading",
):
    return client.post(
        f"/worker/v1/jobs/{job_id}/renew",
        json={"lease_version": lease_version, "phase": phase},
        headers=worker_headers(worker_id),
    )


@pytest.fixture
def running(worker_client: TestClient, worker_a: str, leased: dict) -> dict:
    assert ack(worker_client, worker_a, leased["job_id"], leased["lease_version"]).status_code == 200
    return leased


def test_ack_moves_a_leased_job_to_running(
    worker_client: TestClient,
    worker_a: str,
    leased: dict,
    session_factory: sessionmaker[Session],
) -> None:
    response = ack(worker_client, worker_a, leased["job_id"], leased["lease_version"])

    assert response.status_code == 200
    body = response.json()
    assert body["job_id"] == leased["job_id"]
    assert body["state"] == JobState.RUNNING
    assert body["lease_version"] == leased["lease_version"]
    with session_factory() as session:
        job = session.get(GradingJob, leased["job_id"])
    assert job.state == JobState.RUNNING
    assert job.ack_deadline is None


def test_ack_response_loss_can_be_retried_idempotently(
    worker_client: TestClient,
    worker_a: str,
    leased: dict,
) -> None:
    first = ack(worker_client, worker_a, leased["job_id"], leased["lease_version"])
    second = ack(worker_client, worker_a, leased["job_id"], leased["lease_version"])

    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json()["state"] == JobState.RUNNING


def test_worker_failure_is_immediate_and_idempotent(
    worker_client: TestClient,
    worker_a: str,
    running: dict,
    session_factory: sessionmaker[Session],
) -> None:
    payload = {
        "lease_version": running["lease_version"],
        "code": "runtime_invalid_json",
        "message": "grading output did not match the schema",
    }
    first = worker_client.post(
        f"/worker/v1/jobs/{running['job_id']}/fail",
        json=payload,
        headers=worker_headers(worker_a),
    )
    second = worker_client.post(
        f"/worker/v1/jobs/{running['job_id']}/fail",
        json=payload,
        headers=worker_headers(worker_a),
    )

    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    assert second.json()["state"] == JobState.WORKER_EXCEPTION
    with session_factory() as session:
        job = session.get(GradingJob, running["job_id"])
        order = session.get(Order, job.order_id)
        worker = session.get(Worker, worker_a)
        events = session.scalars(
            select(WorkerEvent).where(
                WorkerEvent.job_id == job.id,
                WorkerEvent.event_type == "job_failed",
            )
        ).all()
    assert job.state == JobState.WORKER_EXCEPTION
    assert order.state == OrderState.V1_RUNNING
    assert worker.current_job_id is None
    assert len(events) == 1
    assert events[0].details["code"] == "runtime_invalid_json"


def test_wrong_worker_cannot_ack(
    worker_client: TestClient,
    worker_b: str,
    leased: dict,
    session_factory: sessionmaker[Session],
) -> None:
    response = ack(worker_client, worker_b, leased["job_id"], leased["lease_version"])

    assert response.status_code == 409
    with session_factory() as session:
        assert session.get(GradingJob, leased["job_id"]).state == JobState.LEASED


def test_stale_lease_version_cannot_ack(
    worker_client: TestClient,
    worker_a: str,
    leased: dict,
    session_factory: sessionmaker[Session],
) -> None:
    response = ack(worker_client, worker_a, leased["job_id"], leased["lease_version"] - 1)

    assert response.status_code == 409
    with session_factory() as session:
        assert session.get(GradingJob, leased["job_id"]).state == JobState.LEASED


def test_future_lease_version_cannot_ack(
    worker_client: TestClient,
    worker_a: str,
    leased: dict,
) -> None:
    response = ack(worker_client, worker_a, leased["job_id"], leased["lease_version"] + 5)

    assert response.status_code == 409


def test_ack_after_the_deadline_is_rejected(
    worker_client: TestClient,
    worker_a: str,
    leased: dict,
    session_factory: sessionmaker[Session],
) -> None:
    with session_factory() as session:
        job = session.get(GradingJob, leased["job_id"])
        job.ack_deadline = datetime.now(timezone.utc) - timedelta(seconds=1)
        session.add(job)
        session.commit()

    response = ack(worker_client, worker_a, leased["job_id"], leased["lease_version"])

    assert response.status_code == 409
    with session_factory() as session:
        assert session.get(GradingJob, leased["job_id"]).state == JobState.LEASED


def test_ack_is_idempotent_once_the_job_is_already_running(
    worker_client: TestClient,
    worker_a: str,
    running: dict,
) -> None:
    response = ack(worker_client, worker_a, running["job_id"], running["lease_version"])

    assert response.status_code == 200
    assert response.json()["state"] == JobState.RUNNING


def test_ack_rejects_an_unknown_job(worker_client: TestClient, worker_a: str) -> None:
    response = ack(worker_client, worker_a, "00000000-0000-0000-0000-000000000000", 1)

    assert response.status_code == 409


def test_renewal_extends_from_server_time(
    worker_client: TestClient,
    worker_a: str,
    running: dict,
    session_factory: sessionmaker[Session],
) -> None:
    response = renew(worker_client, worker_a, running["job_id"], running["lease_version"])

    assert response.status_code == 200
    expected = datetime.now(timezone.utc) + timedelta(seconds=LEASE_SECONDS)
    returned = datetime.fromisoformat(response.json()["lease_expires_at"])
    assert abs((returned - expected).total_seconds()) < 5
    with session_factory() as session:
        assert abs(
            (
                session.get(GradingJob, running["job_id"]).lease_expires_at - expected
            ).total_seconds()
        ) < 5


def test_renewal_ignores_a_client_supplied_expiry(
    worker_client: TestClient,
    worker_a: str,
    running: dict,
    session_factory: sessionmaker[Session],
) -> None:
    """Only server time may set the lease deadline."""
    forged = datetime.now(timezone.utc) + timedelta(days=30)

    response = worker_client.post(
        f"/worker/v1/jobs/{running['job_id']}/renew",
        json={
            "lease_version": running["lease_version"],
            "phase": "grading",
            "lease_expires_at": forged.isoformat(),
        },
        headers=worker_headers(worker_a),
    )

    assert response.status_code == 200
    with session_factory() as session:
        stored = session.get(GradingJob, running["job_id"]).lease_expires_at
    assert stored < datetime.now(timezone.utc) + timedelta(seconds=LEASE_SECONDS + 5)


def test_wrong_worker_cannot_renew(
    worker_client: TestClient,
    worker_b: str,
    running: dict,
    session_factory: sessionmaker[Session],
) -> None:
    with session_factory() as session:
        before = session.get(GradingJob, running["job_id"]).lease_expires_at

    response = renew(worker_client, worker_b, running["job_id"], running["lease_version"])

    assert response.status_code == 409
    with session_factory() as session:
        assert session.get(GradingJob, running["job_id"]).lease_expires_at == before


def test_stale_lease_version_cannot_renew(
    worker_client: TestClient,
    worker_a: str,
    running: dict,
) -> None:
    response = renew(
        worker_client, worker_a, running["job_id"], running["lease_version"] - 1
    )

    assert response.status_code == 409


def test_a_leased_job_cannot_be_renewed_before_ack(
    worker_client: TestClient,
    worker_a: str,
    leased: dict,
) -> None:
    """Renewal accepts only RUNNING or UPLOADING."""
    response = renew(worker_client, worker_a, leased["job_id"], leased["lease_version"])

    assert response.status_code == 409


def test_an_uploading_job_can_be_renewed(
    worker_client: TestClient,
    worker_a: str,
    running: dict,
    session_factory: sessionmaker[Session],
) -> None:
    with session_factory() as session:
        job = session.get(GradingJob, running["job_id"])
        job.state = JobState.UPLOADING
        session.add(job)
        session.commit()

    response = renew(worker_client, worker_a, running["job_id"], running["lease_version"])

    assert response.status_code == 200


@pytest.mark.parametrize("terminal", [JobState.SUCCEEDED, JobState.WORKER_EXCEPTION])
def test_a_terminal_job_cannot_be_renewed(
    worker_client: TestClient,
    worker_a: str,
    running: dict,
    session_factory: sessionmaker[Session],
    terminal: str,
) -> None:
    with session_factory() as session:
        job = session.get(GradingJob, running["job_id"])
        job.state = terminal
        session.add(job)
        session.commit()

    response = renew(worker_client, worker_a, running["job_id"], running["lease_version"])

    assert response.status_code == 409


def test_heartbeat_reports_the_currently_held_job(
    worker_client: TestClient,
    worker_a: str,
    running: dict,
) -> None:
    response = worker_client.post(
        "/worker/v1/heartbeat",
        json={"phase": "grading", "metrics": {"cpu": 12}},
        headers=worker_headers(worker_a),
    )

    assert response.status_code == 200
    assert response.json()["current_job_id"] == running["job_id"]


def test_heartbeat_can_carry_a_lease_renewal(
    worker_client: TestClient,
    worker_a: str,
    running: dict,
    session_factory: sessionmaker[Session],
) -> None:
    """One request may refresh liveness and the lease to halve the traffic."""
    with session_factory() as session:
        job = session.get(GradingJob, running["job_id"])
        job.lease_expires_at = datetime.now(timezone.utc) + timedelta(seconds=5)
        session.add(job)
        session.commit()

    response = worker_client.post(
        "/worker/v1/heartbeat",
        json={
            "phase": "grading",
            "job_id": running["job_id"],
            "lease_version": running["lease_version"],
        },
        headers=worker_headers(worker_a),
    )

    assert response.status_code == 200
    assert response.json()["lease_expires_at"] is not None
    with session_factory() as session:
        renewed = session.get(GradingJob, running["job_id"]).lease_expires_at
    assert renewed > datetime.now(timezone.utc) + timedelta(seconds=LEASE_SECONDS - 10)


def test_heartbeat_renewal_for_another_workers_job_is_rejected(
    worker_client: TestClient,
    worker_b: str,
    running: dict,
    session_factory: sessionmaker[Session],
) -> None:
    with session_factory() as session:
        before = session.get(GradingJob, running["job_id"]).lease_expires_at

    response = worker_client.post(
        "/worker/v1/heartbeat",
        json={
            "job_id": running["job_id"],
            "lease_version": running["lease_version"],
        },
        headers=worker_headers(worker_b),
    )

    assert response.status_code == 409
    with session_factory() as session:
        assert session.get(GradingJob, running["job_id"]).lease_expires_at == before


def test_renew_requires_worker_authentication(
    worker_client: TestClient,
    running: dict,
) -> None:
    response = worker_client.post(
        f"/worker/v1/jobs/{running['job_id']}/renew",
        json={"lease_version": running["lease_version"]},
    )

    assert response.status_code == 401


def test_ack_requires_worker_authentication(
    worker_client: TestClient,
    leased: dict,
) -> None:
    response = worker_client.post(
        f"/worker/v1/jobs/{leased['job_id']}/ack",
        json={"lease_version": leased["lease_version"]},
    )

    assert response.status_code == 401


def test_a_worker_missing_heartbeats_is_marked_suspected_offline(
    lease_service: LeaseService,
    worker_a: str,
    session_factory: sessionmaker[Session],
) -> None:
    with session_factory() as session:
        worker = session.get(Worker, worker_a)
        worker.last_heartbeat_at = datetime.now(timezone.utc) - timedelta(
            seconds=OFFLINE_AFTER_SECONDS + 1
        )
        session.add(worker)
        session.commit()

    assert lease_service.mark_suspected_offline() == 1

    with session_factory() as session:
        assert session.get(Worker, worker_a).status == WorkerStatus.SUSPECTED_OFFLINE


def test_a_recently_seen_worker_stays_online(
    lease_service: LeaseService,
    worker_a: str,
    session_factory: sessionmaker[Session],
) -> None:
    with session_factory() as session:
        worker = session.get(Worker, worker_a)
        worker.last_heartbeat_at = datetime.now(timezone.utc) - timedelta(
            seconds=OFFLINE_AFTER_SECONDS - 10
        )
        session.add(worker)
        session.commit()

    assert lease_service.mark_suspected_offline() == 0

    with session_factory() as session:
        assert session.get(Worker, worker_a).status == WorkerStatus.ONLINE


def test_an_idle_long_poll_refreshes_worker_liveness(
    worker_client: TestClient,
    lease_service: LeaseService,
    worker_a: str,
    session_factory: sessionmaker[Session],
) -> None:
    with session_factory() as session:
        worker = session.get(Worker, worker_a)
        worker.last_heartbeat_at = datetime.now(timezone.utc) - timedelta(
            seconds=OFFLINE_AFTER_SECONDS + 1
        )
        session.add(worker)
        session.commit()
    lease_service.mark_suspected_offline()

    response = worker_client.post(
        "/worker/v1/jobs/lease",
        headers={**worker_headers(worker_a), "Prefer": "wait=0"},
    )

    assert response.status_code == 204
    with session_factory() as session:
        worker = session.get(Worker, worker_a)
    assert worker.status == WorkerStatus.ONLINE
    assert worker.last_heartbeat_at > datetime.now(timezone.utc) - timedelta(seconds=5)


def test_marking_offline_never_touches_the_held_job(
    lease_service: LeaseService,
    worker_a: str,
    running: dict,
    session_factory: sessionmaker[Session],
) -> None:
    with session_factory() as session:
        worker = session.get(Worker, worker_a)
        worker.last_heartbeat_at = datetime.now(timezone.utc) - timedelta(
            seconds=OFFLINE_AFTER_SECONDS + 1
        )
        session.add(worker)
        session.commit()

    lease_service.mark_suspected_offline()

    with session_factory() as session:
        job = session.get(GradingJob, running["job_id"])
    assert job.state == JobState.RUNNING
    assert job.worker_id == worker_a


@pytest.mark.parametrize("started", [JobState.RUNNING, JobState.UPLOADING])
def test_expired_lease_on_a_started_job_becomes_worker_exception(
    lease_service: LeaseService,
    worker_a: str,
    running: dict,
    session_factory: sessionmaker[Session],
    started: str,
) -> None:
    """A started job is never silently re-run; it fails loudly instead."""
    with session_factory() as session:
        job = session.get(GradingJob, running["job_id"])
        job.state = started
        job.lease_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        session.add(job)
        session.commit()

    assert lease_service.expire_started_leases() == 1

    with session_factory() as session:
        job = session.get(GradingJob, running["job_id"])
        worker = session.get(Worker, worker_a)
    assert job.state == JobState.WORKER_EXCEPTION
    assert worker.current_job_id is None


def test_a_started_job_is_never_automatically_requeued(
    lease_service: LeaseService,
    running: dict,
    session_factory: sessionmaker[Session],
) -> None:
    with session_factory() as session:
        job = session.get(GradingJob, running["job_id"])
        job.lease_expires_at = datetime.now(timezone.utc) - timedelta(seconds=600)
        job.ack_deadline = datetime.now(timezone.utc) - timedelta(seconds=600)
        session.add(job)
        session.commit()

    lease_service.release_unacknowledged()
    lease_service.expire_started_leases()

    with session_factory() as session:
        job = session.get(GradingJob, running["job_id"])
    assert job.state != JobState.QUEUED
    assert job.state == JobState.WORKER_EXCEPTION


def test_an_unexpired_started_lease_is_left_alone(
    lease_service: LeaseService,
    running: dict,
    session_factory: sessionmaker[Session],
) -> None:
    assert lease_service.expire_started_leases() == 0

    with session_factory() as session:
        assert session.get(GradingJob, running["job_id"]).state == JobState.RUNNING


def test_requeued_job_returns_the_order_to_queued(
    lease_service: LeaseService,
    worker_client: TestClient,
    worker_a: str,
    leased: dict,
    session_factory: sessionmaker[Session],
) -> None:
    lease_service.release_unacknowledged(
        now=datetime.now(timezone.utc) + timedelta(seconds=ACK_SECONDS + 1)
    )

    with session_factory() as session:
        job = session.get(GradingJob, leased["job_id"])
        order = session.get(Order, job.order_id)
    assert job.state == JobState.QUEUED
    assert order.state == OrderState.V1_QUEUED


def test_a_requeued_job_can_be_leased_by_another_worker_with_a_new_fence(
    lease_service: LeaseService,
    worker_client: TestClient,
    worker_b: str,
    leased: dict,
    session_factory: sessionmaker[Session],
) -> None:
    lease_service.release_unacknowledged(
        now=datetime.now(timezone.utc) + timedelta(seconds=ACK_SECONDS + 1)
    )

    response = worker_client.post(
        "/worker/v1/jobs/lease",
        headers={**worker_headers(worker_b), "Prefer": "wait=0"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["job_id"] == leased["job_id"]
    assert body["lease_version"] == leased["lease_version"] + 1
    # The original holder's fencing token is now stale and must be refused.
    assert ack(worker_client, worker_b, leased["job_id"], leased["lease_version"]).status_code == 409


def test_lifecycle_events_are_recorded_for_audit(
    worker_client: TestClient,
    worker_a: str,
    running: dict,
    session_factory: sessionmaker[Session],
) -> None:
    renew(worker_client, worker_a, running["job_id"], running["lease_version"])

    with session_factory() as session:
        events = session.scalars(
            select(WorkerEvent).where(WorkerEvent.job_id == running["job_id"])
        ).all()

    assert {event.event_type for event in events} >= {"leased", "acked"}
    for event in events:
        assert event.worker_id == worker_a


def test_expiry_helpers_never_leak_secrets_into_events(
    lease_service: LeaseService,
    running: dict,
    session_factory: sessionmaker[Session],
    settings,
) -> None:
    with session_factory() as session:
        job = session.get(GradingJob, running["job_id"])
        job.lease_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        session.add(job)
        session.commit()
    lease_service.expire_started_leases()

    with session_factory() as session:
        events = session.scalars(select(WorkerEvent)).all()

    for event in events:
        rendered = repr(event.details)
        assert settings.worker_shared_key not in rendered
        assert settings.session_secret not in rendered
