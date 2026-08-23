"""ETA as exposed through order detail.

The pure estimator is covered in test_eta.py. These tests pin down the parts
that depend on live fleet and queue state: which Workers count as capacity,
which orders count as queue length, and which orders get a countdown at all.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from server.domain.states import JobState
from server.models import GradingJob, Worker
from server.services.orders import order_eta
from server.services.workers import WorkerStatus
from tests.server.conftest import (
    pay_for_new_order,
    register_worker,
    worker_headers,
)


def test_queued_order_reports_an_eta_range(
    authenticated_client: TestClient,
    session_factory: sessionmaker[Session],
) -> None:
    order_id = pay_for_new_order(authenticated_client, pages=3)
    register_worker(authenticated_client, installation_id="install-eta-1")

    detail = authenticated_client.get(f"/api/v1/orders/{order_id}").json()

    assert detail["eta"] is not None
    assert detail["eta"]["earliest_minutes"] <= detail["eta"]["latest_minutes"]
    assert detail["eta"]["earliest_at"] is not None


def test_offline_workers_are_excluded_from_capacity(
    authenticated_client: TestClient,
    session_factory: sessionmaker[Session],
) -> None:
    """A Worker that stopped heartbeating must not be counted as capacity.

    Counting it would promise a turnaround nobody is working towards.
    """
    order_id = pay_for_new_order(authenticated_client, pages=3)
    worker_id = register_worker(
        authenticated_client, installation_id="install-eta-offline"
    )["worker_id"]

    with session_factory() as session:
        online_eta = order_eta(session=session, order_id=order_id)
        worker = session.get(Worker, worker_id)
        worker.status = WorkerStatus.SUSPECTED_OFFLINE
        session.add(worker)
        session.commit()
        offline_eta = order_eta(session=session, order_id=order_id)

    assert online_eta is not None
    assert offline_eta is None, "noready Worker means no honest estimate"


def test_disabled_workers_are_excluded_from_capacity(
    authenticated_client: TestClient,
    session_factory: sessionmaker[Session],
) -> None:
    order_id = pay_for_new_order(authenticated_client, pages=3)
    worker_id = register_worker(
        authenticated_client, installation_id="install-eta-disabled"
    )["worker_id"]

    with session_factory() as session:
        worker = session.get(Worker, worker_id)
        worker.status = WorkerStatus.DISABLED
        session.add(worker)
        session.commit()
        eta = order_eta(session=session, order_id=order_id)

    assert eta is None


def test_more_workers_shorten_the_estimate(
    authenticated_client: TestClient,
    session_factory: sessionmaker[Session],
) -> None:
    """The estimate must recalculate when the fleet grows."""
    for index in range(4):
        pay_for_new_order(authenticated_client, pages=4)
    last_order = pay_for_new_order(authenticated_client, pages=4)
    register_worker(authenticated_client, installation_id="install-eta-a")

    with session_factory() as session:
        with_one = order_eta(session=session, order_id=last_order)

    register_worker(authenticated_client, installation_id="install-eta-b")
    with session_factory() as session:
        with_two = order_eta(session=session, order_id=last_order)

    assert with_one is not None and with_two is not None
    assert with_two.latest_minutes < with_one.latest_minutes


def test_worker_exception_orders_get_no_countdown(
    authenticated_client: TestClient,
    session_factory: sessionmaker[Session],
) -> None:
    """A failed order is not waiting in line, so it must show no countdown."""
    order_id = pay_for_new_order(authenticated_client, pages=3)
    register_worker(authenticated_client, installation_id="install-eta-failed")

    with session_factory() as session:
        job = session.scalar(
            select(GradingJob).where(GradingJob.order_id == order_id)
        )
        job.state = JobState.WORKER_EXCEPTION
        session.add(job)
        session.commit()
        eta = order_eta(session=session, order_id=order_id)

    assert eta is None
    detail = authenticated_client.get(f"/api/v1/orders/{order_id}").json()
    assert detail["eta"] is None


def test_failed_orders_do_not_inflate_the_queue_for_others(
    authenticated_client: TestClient,
    session_factory: sessionmaker[Session],
) -> None:
    """A worker_exception job is not pending work, so it must not delay others."""
    failed_order = pay_for_new_order(authenticated_client, pages=10)
    waiting_order = pay_for_new_order(authenticated_client, pages=2)
    register_worker(authenticated_client, installation_id="install-eta-queue")

    with session_factory() as session:
        before = order_eta(session=session, order_id=waiting_order)
        job = session.scalar(
            select(GradingJob).where(GradingJob.order_id == failed_order)
        )
        job.state = JobState.WORKER_EXCEPTION
        session.add(job)
        session.commit()
        after = order_eta(session=session, order_id=waiting_order)

    assert before is not None and after is not None
    assert after.latest_minutes < before.latest_minutes


def test_delivered_orders_have_no_eta(
    authenticated_client: TestClient,
    session_factory: sessionmaker[Session],
) -> None:
    """Nothing is pending once the result has landed."""
    from tests.server.conftest import deliver_v1_order

    order_id = deliver_v1_order(authenticated_client)["order_id"]

    with session_factory() as session:
        assert order_eta(session=session, order_id=order_id) is None
    detail = authenticated_client.get(f"/api/v1/orders/{order_id}").json()
    assert detail["eta"] is None


def test_running_job_counts_its_remaining_time(
    authenticated_client: TestClient,
    session_factory: sessionmaker[Session],
) -> None:
    """A busy Worker delays what it picks up next, so the queue waits longer."""
    running_order = pay_for_new_order(authenticated_client, pages=5)
    waiting_order = pay_for_new_order(authenticated_client, pages=1)
    worker_id = register_worker(
        authenticated_client, installation_id="install-eta-busy"
    )["worker_id"]

    with session_factory() as session:
        idle_estimate = order_eta(session=session, order_id=waiting_order)

    leased = authenticated_client.post(
        "/worker/v1/jobs/lease",
        headers={**worker_headers(worker_id), "Prefer": "wait=0"},
    ).json()
    authenticated_client.post(
        f"/worker/v1/jobs/{leased['job_id']}/ack",
        json={"lease_version": leased["lease_version"]},
        headers=worker_headers(worker_id),
    )

    with session_factory() as session:
        busy_estimate = order_eta(session=session, order_id=waiting_order)

    assert idle_estimate is not None and busy_estimate is not None
    assert leased["order_id"] == running_order
    # The onlyready Worker is now mid-job, so the waiting order cannot start
    # any earlier than it did when the Worker was idle.
    assert busy_estimate.earliest_minutes >= idle_estimate.earliest_minutes
