"""Admin Worker controls and the aftersales queue.

Two invariants dominate this file:

*Draining and disabling never cancel work in flight.* They stop the *next* lease
being handed out. A running job keeps its lease, keeps its ``current_job_id`` and
is allowed to deliver, because a half-finished grading run that gets killed has
already cost the user real money and real time.

*Approving a refund reuses RefundService.* There is exactly one code path that
moves money, so its idempotency — one settled refund per payment, keyed on
``external_refund_id`` — holds for admin decisions too. A second path here would
be a route to double-refunding a real user.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from server.domain.states import JobState, OrderState
from server.models import AuditLog, GradingJob, Order, Refund, Worker
from server.services.refunds import RefundState
from server.services.workers import WorkerStatus
from tests.server.conftest import (
    admin_headers,
    admin_login,
    authenticate,
    create_admin,
    deliver_v1_order,
    make_refund_request,
    pay_for_new_order,
    register_worker,
    worker_headers,
)


DRAINING = "draining"


@pytest.fixture
def admin_id(session_factory: sessionmaker[Session]) -> str:
    return create_admin(session_factory)


@pytest.fixture
def admin_client(client: TestClient, admin_id: str) -> TestClient:
    admin = TestClient(client.app)
    csrf = admin_login(admin)
    admin.headers.update(admin_headers(csrf))
    return admin


@pytest.fixture
def busy_worker(client: TestClient, session_factory: sessionmaker[Session]) -> dict:
    """A Worker holding one leased job, mid-flight."""
    authenticate(client)
    order_id = pay_for_new_order(client, pages=2)
    worker_id = register_worker(client, installation_id="install-busy")["worker_id"]

    leased = client.post(
        "/worker/v1/jobs/lease",
        headers={**worker_headers(worker_id), "Prefer": "wait=0"},
    )
    assert leased.status_code == 200, leased.text
    job = leased.json()
    client.post(
        f"/worker/v1/jobs/{job['job_id']}/ack",
        json={"lease_version": job["lease_version"]},
        headers=worker_headers(worker_id),
    )
    with session_factory() as session:
        assert session.get(Worker, worker_id).current_job_id == job["job_id"]
    return {
        "worker_id": worker_id,
        "job_id": job["job_id"],
        "lease_version": job["lease_version"],
        "order_id": order_id,
    }


class TestWorkerListing:
    def test_listing_reports_the_operational_facts(
        self,
        client: TestClient,
        admin_client: TestClient,
    ) -> None:
        worker_id = register_worker(client, installation_id="install-list")["worker_id"]

        body = admin_client.get("/admin/api/v1/workers").json()

        row = next(item for item in body["items"] if item["worker_id"] == worker_id)
        assert row["status"] == WorkerStatus.ONLINE
        assert row["platform"]
        assert row["architecture"]
        assert row["worker_version"]
        assert row["last_heartbeat_at"]
        assert row["current_job_id"] is None

    def test_listing_never_exposes_the_shared_key_or_installation_secret(
        self,
        client: TestClient,
        admin_client: TestClient,
    ) -> None:
        from tests.server.conftest import SHARED_KEY

        register_worker(client, installation_id="install-secret")["worker_id"]

        raw = admin_client.get("/admin/api/v1/workers").text

        assert SHARED_KEY not in raw

    def test_a_miniapp_token_cannot_list_workers(
        self,
        authenticated_client: TestClient,
    ) -> None:
        assert authenticated_client.get("/admin/api/v1/workers").status_code == 401


class TestDrainAndDisable:
    def test_drain_keeps_current_job_but_blocks_next_lease(
        self,
        admin_client: TestClient,
        busy_worker: dict,
    ) -> None:
        response = admin_client.post(
            f"/admin/api/v1/workers/{busy_worker['worker_id']}/drain"
        )

        assert response.status_code == 200
        assert response.json()["status"] == DRAINING
        assert response.json()["current_job_id"] == busy_worker["job_id"]

    def test_a_draining_worker_receives_no_further_lease(
        self,
        client: TestClient,
        admin_client: TestClient,
        session_factory: sessionmaker[Session],
    ) -> None:
        authenticate(client)
        pay_for_new_order(client, pages=2)
        worker_id = register_worker(client, installation_id="install-drain")["worker_id"]
        admin_client.post(f"/admin/api/v1/workers/{worker_id}/drain")

        leased = client.post(
            "/worker/v1/jobs/lease",
            headers={**worker_headers(worker_id), "Prefer": "wait=0"},
        )

        assert leased.status_code == 204
        with session_factory() as session:
            job = session.scalars(select(GradingJob)).one()
            assert job.state == JobState.QUEUED, "the job must stay claimable"

    def test_a_running_job_is_not_cancelled_by_draining(
        self,
        client: TestClient,
        admin_client: TestClient,
        busy_worker: dict,
        session_factory: sessionmaker[Session],
    ) -> None:
        admin_client.post(f"/admin/api/v1/workers/{busy_worker['worker_id']}/drain")

        with session_factory() as session:
            job = session.get(GradingJob, busy_worker["job_id"])
            assert job.state == JobState.RUNNING
            assert job.worker_id == busy_worker["worker_id"]

        # And the Worker may still renew and deliver that job.
        renewed = client.post(
            f"/worker/v1/jobs/{busy_worker['job_id']}/renew",
            json={"lease_version": busy_worker["lease_version"]},
            headers=worker_headers(busy_worker["worker_id"]),
        )
        assert renewed.status_code == 200

    def test_disable_blocks_leases_without_cancelling_the_current_job(
        self,
        admin_client: TestClient,
        busy_worker: dict,
        session_factory: sessionmaker[Session],
    ) -> None:
        response = admin_client.post(
            f"/admin/api/v1/workers/{busy_worker['worker_id']}/disable"
        )

        assert response.status_code == 200
        assert response.json()["status"] == WorkerStatus.DISABLED
        assert response.json()["current_job_id"] == busy_worker["job_id"]
        with session_factory() as session:
            assert session.get(GradingJob, busy_worker["job_id"]).state == (
                JobState.RUNNING
            )

    def test_a_disabled_worker_cannot_lease(
        self,
        client: TestClient,
        admin_client: TestClient,
    ) -> None:
        authenticate(client)
        pay_for_new_order(client, pages=2)
        worker_id = register_worker(client, installation_id="install-disable")["worker_id"]
        admin_client.post(f"/admin/api/v1/workers/{worker_id}/disable")

        leased = client.post(
            "/worker/v1/jobs/lease",
            headers={**worker_headers(worker_id), "Prefer": "wait=0"},
        )

        assert leased.status_code == 403

    def test_enable_returns_a_worker_to_service(
        self,
        client: TestClient,
        admin_client: TestClient,
    ) -> None:
        authenticate(client)
        pay_for_new_order(client, pages=2)
        worker_id = register_worker(client, installation_id="install-enable")["worker_id"]
        admin_client.post(f"/admin/api/v1/workers/{worker_id}/disable")

        response = admin_client.post(f"/admin/api/v1/workers/{worker_id}/enable")

        assert response.status_code == 200
        assert response.json()["status"] == WorkerStatus.ONLINE
        leased = client.post(
            "/worker/v1/jobs/lease",
            headers={**worker_headers(worker_id), "Prefer": "wait=0"},
        )
        assert leased.status_code == 200

    def test_draining_a_disabled_worker_does_not_downgrade_the_hard_stop(
        self,
        client: TestClient,
        admin_client: TestClient,
    ) -> None:
        """Drain is a planned wind-down; disable is a hard stop.

        Letting drain overwrite `disabled` would silently weaken an operator's
        deliberate hard stop into "will take work again once re-enabled". Both
        still withhold leases, so this is a clarity rather than an authorisation
        problem — but the panel would misreport why the Worker is idle.
        """
        worker_id = register_worker(client, installation_id="install-hardstop")[
            "worker_id"
        ]
        admin_client.post(f"/admin/api/v1/workers/{worker_id}/disable")

        response = admin_client.post(f"/admin/api/v1/workers/{worker_id}/drain")

        assert response.status_code == 409
        assert response.json()["detail"]
        listed = admin_client.get("/admin/api/v1/workers").json()["items"]
        assert listed[0]["status"] == WorkerStatus.DISABLED

    def test_every_worker_control_writes_an_audit_row(
        self,
        client: TestClient,
        admin_client: TestClient,
        admin_id: str,
        session_factory: sessionmaker[Session],
    ) -> None:
        worker_id = register_worker(client, installation_id="install-audit")["worker_id"]

        admin_client.post(f"/admin/api/v1/workers/{worker_id}/drain")
        admin_client.post(f"/admin/api/v1/workers/{worker_id}/disable")
        admin_client.post(f"/admin/api/v1/workers/{worker_id}/enable")

        with session_factory() as session:
            entries = session.scalars(
                select(AuditLog).where(AuditLog.target_type == "worker")
            ).all()
            actions = {entry.action for entry in entries}
            assert actions == {"worker.drain", "worker.disable", "worker.enable"}
            for entry in entries:
                assert entry.actor_type == "admin"
                assert entry.actor_id == admin_id
                assert entry.target_id == worker_id

    def test_worker_controls_require_a_csrf_token(
        self,
        client: TestClient,
        admin_id: str,
    ) -> None:
        worker_id = register_worker(client, installation_id="install-csrf")["worker_id"]
        naked = TestClient(client.app)
        admin_login(naked)

        response = naked.post(f"/admin/api/v1/workers/{worker_id}/drain")

        assert response.status_code == 403

    def test_an_unknown_worker_is_a_404(self, admin_client: TestClient) -> None:
        response = admin_client.post(
            "/admin/api/v1/workers/00000000-0000-0000-0000-000000000000/drain"
        )
        assert response.status_code == 404


class TestAftersalesQueue:
    def test_the_queue_lists_manual_cases_awaiting_a_decision(
        self,
        authenticated_client: TestClient,
        admin_client: TestClient,
    ) -> None:
        refund = make_refund_request(authenticated_client, pages=11)

        body = admin_client.get("/admin/api/v1/aftersales").json()

        row = next(item for item in body["items"] if item["refund_id"] == refund["refund_id"])
        assert row["state"] == RefundState.PENDING
        assert row["source"] == "user"
        assert row["order_id"] == refund["order_id"]
        assert row["amount_cents"] > 0

    def test_the_queue_can_be_filtered_by_state(
        self,
        authenticated_client: TestClient,
        admin_client: TestClient,
    ) -> None:
        make_refund_request(authenticated_client, pages=11)

        refunded = admin_client.get(
            "/admin/api/v1/aftersales", params={"state": RefundState.REFUNDED}
        ).json()

        assert refunded["items"] == []

    def test_the_queue_never_leaks_file_paths(
        self,
        authenticated_client: TestClient,
        admin_client: TestClient,
    ) -> None:
        make_refund_request(authenticated_client, pages=11)

        raw = admin_client.get("/admin/api/v1/aftersales").text

        assert "relative_path" not in raw

    def test_approving_from_the_queue_settles_the_refund_once(
        self,
        authenticated_client: TestClient,
        admin_client: TestClient,
        session_factory: sessionmaker[Session],
    ) -> None:
        """Reuses RefundService, so its idempotency applies here too.

        A repeated approval reports the settled state again rather than
        erroring: ``execute`` short-circuits an already-refunded row without
        touching the gateway. What must never happen is a *second* refund row —
        a second ``external_refund_id`` is one the provider cannot deduplicate,
        which is a real double payment.
        """
        refund = make_refund_request(authenticated_client, pages=11)

        first = admin_client.post(
            f"/admin/api/v1/refunds/{refund['refund_id']}/approve"
        )
        second = admin_client.post(
            f"/admin/api/v1/refunds/{refund['refund_id']}/approve"
        )

        assert first.status_code == 200
        assert second.status_code == 200
        assert second.json()["state"] == RefundState.REFUNDED
        with session_factory() as session:
            rows = session.scalars(select(Refund)).all()
            assert len(rows) == 1, "a second refund row would double-refund the user"
            assert rows[0].state == RefundState.REFUNDED
            order = session.get(Order, refund["order_id"])
            assert order.state == OrderState.REFUNDED
            assert order.downloads_revoked_at is not None

    def test_rejecting_requires_a_reason(
        self,
        authenticated_client: TestClient,
        admin_client: TestClient,
    ) -> None:
        refund = make_refund_request(authenticated_client, pages=11)

        response = admin_client.post(
            f"/admin/api/v1/refunds/{refund['refund_id']}/reject", json={}
        )

        assert response.status_code == 422

    def test_rejecting_keeps_the_download_right(
        self,
        authenticated_client: TestClient,
        admin_client: TestClient,
        session_factory: sessionmaker[Session],
    ) -> None:
        refund = make_refund_request(authenticated_client, pages=11)

        response = admin_client.post(
            f"/admin/api/v1/refunds/{refund['refund_id']}/reject",
            json={"reason": "答卷与评分标准一致，未发现批改错误"},
        )

        assert response.status_code == 200
        with session_factory() as session:
            order = session.get(Order, refund["order_id"])
            assert order.state == OrderState.ACCEPTED
            assert order.downloads_revoked_at is None
