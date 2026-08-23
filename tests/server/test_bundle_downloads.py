"""Tests for the worker bundle download endpoint.

Phase 04 adds the one approved server-side exception to the Phase 03
contract: a GET endpoint that streams source/reference PDFs to a worker
that holds an active lease. The endpoint authorises by the worker
credential plus a single-use download token issued with the lease; the
token binds the download to one lease version so a recycled lease
immediately invalidates older tokens.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from server.models import GradingJob
from server.services.leases import LeaseService
from tests.server.conftest import (
    authenticate,
    create_quote,
    make_pdf_bytes,
    register_worker,
    worker_headers,
)


@pytest.fixture
def worker_client(client: TestClient) -> TestClient:
    authenticate(client)
    return client


@pytest.fixture
def worker_id(worker_client: TestClient) -> str:
    return register_worker(
        worker_client, installation_id="install-bundle-a"
    )["worker_id"]


def queue_order_with_reference(
    client: TestClient, *, pages: int = 2, reference_pages: int = 1, note: str = ""
) -> str:
    quote = create_quote(
        client,
        pages=pages,
        reference_pages=reference_pages,
        note=note,
    )
    prepay = client.post(
        "/api/v1/payments/prepay", json={"quote_id": quote["id"]}
    ).json()
    response = client.post(
        "/callbacks/fake/pay",
        json={"fake_transaction_id": prepay["prepay_id"], "status": "SUCCESS"},
    )
    assert response.status_code == 204, response.text
    return quote["id"]


def lease(client: TestClient, worker_id: str) -> dict:
    headers = worker_headers(worker_id)
    headers["Prefer"] = "wait=0"
    response = client.post("/worker/v1/jobs/lease", headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


def test_download_source_pdf_returns_the_stored_bytes(
    client: TestClient,
    worker_id: str,
    session_factory: sessionmaker[Session],
) -> None:
    queue_order_with_reference(client, pages=2, reference_pages=1)
    bundle = lease(client, worker_id)

    headers = worker_headers(worker_id)
    headers["X-Download-Token"] = bundle["source_file"]["download_token"]
    response = client.get(
        f"/worker/v1/jobs/{bundle['job_id']}/bundle/source",
        headers=headers,
    )

    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == "application/pdf"
    assert len(response.content) > 0
    # The bytes match what the order intake stored.
    assert response.content[:5] == b"%PDF-"


def test_download_reference_pdf_returns_the_stored_bytes(
    client: TestClient,
    worker_id: str,
) -> None:
    queue_order_with_reference(client, pages=2, reference_pages=1)
    bundle = lease(client, worker_id)

    headers = worker_headers(worker_id)
    headers["X-Download-Token"] = bundle["reference_file"]["download_token"]
    response = client.get(
        f"/worker/v1/jobs/{bundle['job_id']}/bundle/reference",
        headers=headers,
    )

    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == "application/pdf"
    assert response.content[:5] == b"%PDF-"


def test_download_rejects_unknown_kind(
    client: TestClient,
    worker_id: str,
) -> None:
    queue_order_with_reference(client)
    bundle = lease(client, worker_id)

    headers = worker_headers(worker_id)
    headers["X-Download-Token"] = bundle["source_file"]["download_token"]
    response = client.get(
        f"/worker/v1/jobs/{bundle['job_id']}/bundle/mark-scheme",
        headers=headers,
    )

    assert response.status_code == 400, response.text


def test_download_rejects_missing_token(
    client: TestClient,
    worker_id: str,
) -> None:
    queue_order_with_reference(client)
    bundle = lease(client, worker_id)

    response = client.get(
        f"/worker/v1/jobs/{bundle['job_id']}/bundle/source",
        headers=worker_headers(worker_id),
    )

    assert response.status_code == 403, response.text


def test_download_rejects_wrong_token(
    client: TestClient,
    worker_id: str,
) -> None:
    queue_order_with_reference(client)
    bundle = lease(client, worker_id)

    headers = worker_headers(worker_id)
    headers["X-Download-Token"] = "wrong-token"
    response = client.get(
        f"/worker/v1/jobs/{bundle['job_id']}/bundle/source",
        headers=headers,
    )

    assert response.status_code == 403, response.text


def test_download_rejects_worker_without_lease(
    client: TestClient,
    worker_id: str,
) -> None:
    queue_order_with_reference(client)
    bundle = lease(client, worker_id)

    # A different worker that did not lease the job.
    other_worker = register_worker(
        client, installation_id="install-other"
    )["worker_id"]

    headers = worker_headers(other_worker)
    headers["X-Download-Token"] = bundle["source_file"]["download_token"]
    response = client.get(
        f"/worker/v1/jobs/{bundle['job_id']}/bundle/source",
        headers=headers,
    )

    assert response.status_code == 409, response.text


def test_download_rejects_reference_kind_when_no_reference(
    client: TestClient,
    worker_id: str,
) -> None:
    queue_order_with_reference(client, pages=2, reference_pages=None)
    bundle = lease(client, worker_id)

    # Source token is valid but reference does not exist on this order.
    headers = worker_headers(worker_id)
    headers["X-Download-Token"] = bundle["source_file"]["download_token"]
    response = client.get(
        f"/worker/v1/jobs/{bundle['job_id']}/bundle/reference",
        headers=headers,
    )

    assert response.status_code == 404, response.text


def test_download_invalidates_after_lease_recycled(
    client: TestClient,
    worker_id: str,
    session_factory: sessionmaker[Session],
) -> None:
    """A token issued for an older lease_version must stop working once
    the lease is recycled (expired and re-leased).

    The recycling happens when the lease expires and another worker
    claims the same job. The first worker's download_token must then
    be rejected with 409 so it cannot keep reading student data.
    """
    queue_order_with_reference(client)
    bundle = lease(client, worker_id)
    stale_token = bundle["source_file"]["download_token"]

    # Force the ack deadline to expire so the recycler can release the
    # lease back to the queue for the next worker.
    with session_factory() as session:
        job = session.get(GradingJob, bundle["job_id"])
        job.ack_deadline = datetime.now(timezone.utc) - timedelta(seconds=1)
        job.lease_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        session.commit()

    # Recycle the unacknowledged lease so the job becomes claimable again.
    LeaseService(session_factory).release_unacknowledged()

    other_worker = register_worker(
        client, installation_id="install-recycle"
    )["worker_id"]
    new_bundle = lease(client, other_worker)
    assert new_bundle["lease_version"] > bundle["lease_version"]

    # The first worker's token must now be rejected.
    headers = worker_headers(worker_id)
    headers["X-Download-Token"] = stale_token
    response = client.get(
        f"/worker/v1/jobs/{bundle['job_id']}/bundle/source",
        headers=headers,
    )
    assert response.status_code == 409, response.text

    # The new worker's token works.
    headers = worker_headers(other_worker)
    headers["X-Download-Token"] = new_bundle["source_file"]["download_token"]
    response = client.get(
        f"/worker/v1/jobs/{bundle['job_id']}/bundle/source",
        headers=headers,
    )
    assert response.status_code == 200, response.text


def test_download_rejects_unauthenticated_request(
    client: TestClient,
    worker_id: str,
) -> None:
    queue_order_with_reference(client)
    bundle = lease(client, worker_id)

    response = client.get(
        f"/worker/v1/jobs/{bundle['job_id']}/bundle/source",
        headers={"X-Download-Token": bundle["source_file"]["download_token"]},
    )

    assert response.status_code == 401, response.text


def test_download_rejects_unknown_job(
    client: TestClient,
    worker_id: str,
) -> None:
    headers = worker_headers(worker_id)
    headers["X-Download-Token"] = "any-token"
    response = client.get(
        "/worker/v1/jobs/no-such-job/bundle/source",
        headers=headers,
    )
    assert response.status_code == 404, response.text
