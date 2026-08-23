"""Admin overview metrics and order search.

This is a management plane, so the mini-program's ownership filter deliberately
does *not* apply: an admin searches across every user's orders. What must still
hold is that the response never discloses where files live on disk, and that the
overview's numbers cannot contradict each other.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from server.domain.states import JobState, OrderState
from server.models import GradingJob, Order, QuoteSession, Worker
from tests.server.conftest import (
    ADMIN_PASSWORD,
    admin_headers,
    admin_login,
    authenticate,
    create_admin,
    deliver_v1_order,
    make_refund_request,
    pay_for_new_order,
    register_worker,
)


@pytest.fixture
def admin_id(session_factory: sessionmaker[Session]) -> str:
    return create_admin(session_factory)


@pytest.fixture
def admin_client(client: TestClient, admin_id: str) -> TestClient:
    admin = TestClient(client.app)
    csrf = admin_login(admin)
    admin.headers.update(admin_headers(csrf))
    return admin


class TestOverview:
    def test_overview_counts_come_from_one_snapshot(
        self,
        client: TestClient,
        admin_client: TestClient,
    ) -> None:
        authenticate(client)
        pay_for_new_order(client, pages=2)
        pay_for_new_order(client, pages=3)

        body = admin_client.get("/admin/api/v1/overview").json()

        assert body["orders"]["v1_queued"] == 2
        assert body["jobs"]["queued"] == 2
        # The two figures describe the same instant, so a job cannot be queued
        # for an order the same snapshot does not know about.
        assert body["jobs"]["queued"] <= sum(body["orders"].values())

    def test_overview_reports_worker_and_storage_health(
        self,
        client: TestClient,
        admin_client: TestClient,
    ) -> None:
        register_worker(client, installation_id="install-overview")

        body = admin_client.get("/admin/api/v1/overview").json()

        assert body["workers"]["online"] == 1
        assert body["workers"]["disabled"] == 0
        assert 0 <= body["storage"]["used_percent"] <= 100
        # Phase 09 owns real backups, so this must not claim one happened.
        assert body["storage"]["latest_backup_age_seconds"] is None

    def test_overview_accounts_for_every_worker_status(
        self,
        client: TestClient,
        admin_client: TestClient,
    ) -> None:
        """A drained Worker must still be visible somewhere in the counts.

        Operators read this panel to answer "how much capacity is running". A
        status the panel does not know about makes the machine disappear
        entirely, which reads as zero capacity — and hides a Worker stuck
        half-drained.
        """
        worker_id = register_worker(client, installation_id="install-drain-count")[
            "worker_id"
        ]
        admin_client.post(f"/admin/api/v1/workers/{worker_id}/drain")

        workers = admin_client.get("/admin/api/v1/overview").json()["workers"]

        assert workers["draining"] == 1
        assert sum(workers.values()) == 1, "the Worker must not vanish from the counts"

    def test_overview_counts_pending_manual_refunds(
        self,
        client: TestClient,
        authenticated_client: TestClient,
        admin_client: TestClient,
    ) -> None:
        make_refund_request(authenticated_client, pages=11)

        body = admin_client.get("/admin/api/v1/overview").json()

        assert body["refunds"]["pending_manual"] == 1
        assert body["refunds"]["failed"] == 0

    def test_overview_requires_an_admin_session(self, client: TestClient) -> None:
        assert TestClient(client.app).get("/admin/api/v1/overview").status_code == 401

    def test_a_miniapp_token_cannot_read_the_overview(
        self,
        authenticated_client: TestClient,
    ) -> None:
        assert authenticated_client.get("/admin/api/v1/overview").status_code == 401


class TestOrderSearch:
    def test_admin_search_sees_orders_from_every_user(
        self,
        client: TestClient,
        admin_client: TestClient,
    ) -> None:
        """No owner filter: that is the difference from the mini-program API."""
        authenticate(client, "test-parent-1")
        first = pay_for_new_order(client, pages=2)
        authenticate(client, "test-parent-2")
        second = pay_for_new_order(client, pages=2)

        body = admin_client.get("/admin/api/v1/orders").json()

        found = {item["id"] for item in body["items"]}
        assert {first, second} <= found

    def test_admin_order_search_does_not_expose_file_paths(
        self,
        client: TestClient,
        admin_client: TestClient,
    ) -> None:
        authenticate(client)
        order_id = pay_for_new_order(client, pages=2)

        body = admin_client.get(f"/admin/api/v1/orders/{order_id}").json()

        assert "relative_path" not in json.dumps(body)
        assert body["id"] == order_id

    def test_order_detail_never_leaks_storage_or_secrets(
        self,
        client: TestClient,
        admin_client: TestClient,
    ) -> None:
        authenticate(client)
        delivered = deliver_v1_order(client, pages=2)

        raw = admin_client.get(f"/admin/api/v1/orders/{delivered['order_id']}").text

        for forbidden in ("relative_path", "sqlite", "session_secret", ".sqlite3"):
            assert forbidden not in raw
        # Files are described by logical name, not by location.
        body = json.loads(raw)
        kinds = {item["kind"] for item in body["files"]}
        assert "source_pdf" in kinds

    def test_detail_reports_rounds_jobs_and_timeline(
        self,
        client: TestClient,
        admin_client: TestClient,
    ) -> None:
        authenticate(client)
        delivered = deliver_v1_order(client, pages=2)

        body = admin_client.get(f"/admin/api/v1/orders/{delivered['order_id']}").json()

        assert body["state"] == OrderState.V1_DELIVERED
        assert len(body["rounds"]) == 1
        assert body["rounds"][0]["round_number"] == 1
        assert body["rounds"][0]["job"]["state"] == JobState.SUCCEEDED
        assert [event["event"] for event in body["timeline"]]
        assert body["payment"]["state"] == "succeeded"

    def test_detail_reports_the_admin_actions_available(
        self,
        client: TestClient,
        admin_client: TestClient,
    ) -> None:
        authenticate(client)
        order_id = pay_for_new_order(client, pages=2)

        body = admin_client.get(f"/admin/api/v1/orders/{order_id}").json()

        # A paid, queued order can be refunded technically but not approved.
        assert "technical_refund" in body["available_admin_actions"]

    def test_an_unknown_order_is_a_404(self, admin_client: TestClient) -> None:
        response = admin_client.get(
            "/admin/api/v1/orders/00000000-0000-0000-0000-000000000000"
        )
        assert response.status_code == 404

    def test_search_by_exact_order_id(
        self,
        client: TestClient,
        admin_client: TestClient,
    ) -> None:
        authenticate(client)
        order_id = pay_for_new_order(client, pages=2)
        pay_for_new_order(client, pages=2)

        body = admin_client.get("/admin/api/v1/orders", params={"query": order_id}).json()

        assert [item["id"] for item in body["items"]] == [order_id]

    def test_search_by_public_user_id(
        self,
        client: TestClient,
        admin_client: TestClient,
    ) -> None:
        user = authenticate(client, "test-parent-7")
        order_id = pay_for_new_order(client, pages=2)
        authenticate(client, "test-parent-8")
        other = pay_for_new_order(client, pages=2)

        body = admin_client.get(
            "/admin/api/v1/orders", params={"query": user["public_id"]}
        ).json()

        found = {item["id"] for item in body["items"]}
        assert order_id in found
        assert other not in found

    def test_search_by_payment_transaction_id(
        self,
        client: TestClient,
        admin_client: TestClient,
        session_factory: sessionmaker[Session],
    ) -> None:
        authenticate(client)
        order_id = pay_for_new_order(client, pages=2)
        with session_factory() as session:
            from server.models import Payment

            order = session.get(Order, order_id)
            payment = (
                session.query(Payment)
                .filter(Payment.quote_session_id == order.quote_session_id)
                .one()
            )
            transaction_id = payment.external_transaction_id
        assert transaction_id

        body = admin_client.get(
            "/admin/api/v1/orders", params={"query": transaction_id}
        ).json()

        assert [item["id"] for item in body["items"]] == [order_id]

    def test_filter_by_state(
        self,
        client: TestClient,
        admin_client: TestClient,
    ) -> None:
        authenticate(client)
        # Deliver first: leases are FIFO across the whole queue, so an order
        # queued beforehand would be the one the Worker picks up.
        delivered = deliver_v1_order(client, pages=2)
        queued = pay_for_new_order(client, pages=2)

        body = admin_client.get(
            "/admin/api/v1/orders", params={"state": OrderState.V1_QUEUED}
        ).json()

        found = {item["id"] for item in body["items"]}
        assert queued in found
        assert delivered["order_id"] not in found

    def test_filter_by_creation_date_range(
        self,
        client: TestClient,
        admin_client: TestClient,
        session_factory: sessionmaker[Session],
    ) -> None:
        authenticate(client)
        old_order = pay_for_new_order(client, pages=2)
        recent = pay_for_new_order(client, pages=2)
        long_ago = datetime.now(timezone.utc) - timedelta(days=30)
        with session_factory() as session:
            session.get(Order, old_order).created_at = long_ago
            session.commit()

        body = admin_client.get(
            "/admin/api/v1/orders",
            params={
                "created_from": (
                    datetime.now(timezone.utc) - timedelta(days=1)
                ).isoformat()
            },
        ).json()

        found = {item["id"] for item in body["items"]}
        assert recent in found
        assert old_order not in found

    def test_results_are_keyset_paginated_without_duplicates(
        self,
        client: TestClient,
        admin_client: TestClient,
    ) -> None:
        authenticate(client)
        created = {pay_for_new_order(client, pages=2) for _ in range(5)}

        seen: list[str] = []
        cursor: str | None = None
        for _ in range(5):
            params = {"page_size": 2}
            if cursor is not None:
                params["cursor"] = cursor
            body = admin_client.get("/admin/api/v1/orders", params=params).json()
            seen.extend(item["id"] for item in body["items"])
            cursor = body["next_cursor"]
            if cursor is None:
                break

        assert len(seen) == len(set(seen)), "a keyset page must not repeat a row"
        assert created <= set(seen)
        assert cursor is None

    def test_an_invalid_cursor_is_rejected(self, admin_client: TestClient) -> None:
        response = admin_client.get(
            "/admin/api/v1/orders", params={"cursor": "not-a-cursor"}
        )
        assert response.status_code == 400

    def test_page_size_is_capped(
        self,
        client: TestClient,
        admin_client: TestClient,
    ) -> None:
        response = admin_client.get(
            "/admin/api/v1/orders", params={"page_size": 10_000}
        )
        assert response.status_code == 422

    def test_search_results_carry_no_file_paths_either(
        self,
        client: TestClient,
        admin_client: TestClient,
    ) -> None:
        authenticate(client)
        pay_for_new_order(client, pages=2)

        raw = admin_client.get("/admin/api/v1/orders").text

        assert "relative_path" not in raw
