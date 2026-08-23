"""Admin users, funds, operational settings and the append-only audit view.

The load-bearing rules here:

*Repricing versions, never rewrites.* An existing quote keeps the amount it
showed the user. Editing ``QuoteSession.quoted_amount_cents`` in place would
change the price of something already agreed.

*Settings never return secrets.* The settings API exposes operational knobs, so
it must not become a way to read ``session_secret``, either shared key, or the
database URL out of a running deployment.

*The audit log is append-only.* There is no update or delete route, and details
carry no plaintext secret.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from server.models import AuditLog, PriceRule, QuoteSession
from tests.server.conftest import (
    ADMIN_PASSWORD,
    ADMIN_SHARED_KEY,
    SHARED_KEY,
    admin_headers,
    admin_login,
    authenticate,
    create_admin,
    create_quote,
    deliver_v1_order,
    make_pdf_bytes,
    make_refund_request,
    pay_for_new_order,
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


class TestPriceRules:
    def test_price_change_does_not_modify_existing_quote(
        self,
        authenticated_client: TestClient,
        admin_client: TestClient,
        session_factory: sessionmaker[Session],
    ) -> None:
        quote = create_quote(authenticated_client, pages=2)
        before = quote["amount_cents"]

        response = admin_client.post(
            "/admin/api/v1/settings/price-rules",
            json={"service_tier": "annotated_review", "cents_per_page": 1200},
        )

        assert response.status_code == 201
        with session_factory() as session:
            reloaded = session.get(QuoteSession, quote["id"])
            assert reloaded.quoted_amount_cents == before

    def test_repricing_creates_a_new_version_and_retires_the_old(
        self,
        admin_client: TestClient,
        session_factory: sessionmaker[Session],
    ) -> None:
        admin_client.post(
            "/admin/api/v1/settings/price-rules",
            json={"service_tier": "annotated_review", "cents_per_page": 1200},
        )
        admin_client.post(
            "/admin/api/v1/settings/price-rules",
            json={"service_tier": "annotated_review", "cents_per_page": 1500},
        )

        with session_factory() as session:
            rules = session.scalars(
                select(PriceRule).order_by(PriceRule.effective_from)
            ).all()
            live = [rule for rule in rules if rule.retired_at is None]
            annotated_live = [
                rule for rule in live if rule.service_tier == "annotated_review"
            ]
            assert len(annotated_live) == 1
            assert annotated_live[0].cents_per_page == 1500
            assert len(rules) >= 2, "history must be kept, not overwritten"

    def test_a_new_quote_uses_the_new_price(
        self,
        authenticated_client: TestClient,
        admin_client: TestClient,
    ) -> None:
        admin_client.post(
            "/admin/api/v1/settings/price-rules",
            json={"service_tier": "annotated_review", "cents_per_page": 1200},
        )

        quote = create_quote(authenticated_client, pages=2)

        assert quote["cents_per_page"] == 1200
        assert quote["amount_cents"] == 2400

    def test_repricing_is_audited(
        self,
        admin_client: TestClient,
        admin_id: str,
        session_factory: sessionmaker[Session],
    ) -> None:
        admin_client.post(
            "/admin/api/v1/settings/price-rules",
            json={"service_tier": "annotated_review", "cents_per_page": 1300},
        )

        with session_factory() as session:
            entry = session.scalars(
                select(AuditLog).where(AuditLog.action == "settings.price_rule")
            ).one()
            assert entry.actor_id == admin_id
            assert entry.details["service_tier"] == "annotated_review"
            assert entry.details["cents_per_page"] == 1300

    def test_a_non_positive_price_is_rejected(self, admin_client: TestClient) -> None:
        response = admin_client.post(
            "/admin/api/v1/settings/price-rules",
            json={"service_tier": "annotated_review", "cents_per_page": 0},
        )
        assert response.status_code == 422


class TestOperationalSettings:
    def test_settings_never_return_secret_values(
        self,
        admin_client: TestClient,
    ) -> None:
        raw = admin_client.get("/admin/api/v1/settings").text

        for secret in (ADMIN_SHARED_KEY, SHARED_KEY, "s" * 32, "sqlite", ".sqlite3"):
            assert secret not in raw
        body = admin_client.get("/admin/api/v1/settings").json()
        for forbidden in (
            "session_secret",
            "worker_shared_key",
            "admin_shared_key",
            "database_url",
        ):
            assert forbidden not in body

    def test_settings_report_the_values_in_force(
        self,
        admin_client: TestClient,
    ) -> None:
        body = admin_client.get("/admin/api/v1/settings").json()

        assert body["max_pdf_pages"] >= 1
        assert body["max_pdf_bytes"] >= 1024
        assert body["acceptance_ttl_seconds"] >= 60
        assert body["minutes_per_page"] >= 1
        assert body["summary_cents_per_page"] == 100
        assert body["annotated_cents_per_page"] == 500
        assert body["automatic_refund_max_amount_cents"] >= 1

    def test_an_operational_value_can_be_changed_and_takes_effect(
        self,
        authenticated_client: TestClient,
        admin_client: TestClient,
    ) -> None:
        response = admin_client.patch(
            "/admin/api/v1/settings", json={"max_pdf_pages": 3}
        )

        assert response.status_code == 200
        assert response.json()["max_pdf_pages"] == 3
        # A 4-page upload must now be refused by the quote path, which proves
        # the stored value is actually consulted rather than merely recorded.
        rejected = authenticated_client.post(
            "/api/v1/quotes",
            files={
                "source_pdf": (
                    "answers.pdf",
                    make_pdf_bytes(4),
                    "application/pdf",
                )
            },
            data={
                "service_tier": "annotated_review",
                "grading_standard": "imo",
                "note": "",
            },
        )
        assert rejected.status_code == 400, rejected.text

    def test_changing_a_setting_is_audited(
        self,
        admin_client: TestClient,
        admin_id: str,
        session_factory: sessionmaker[Session],
    ) -> None:
        admin_client.patch("/admin/api/v1/settings", json={"minutes_per_page": 12})

        with session_factory() as session:
            entry = session.scalars(
                select(AuditLog).where(AuditLog.action == "settings.update")
            ).one()
            assert entry.actor_id == admin_id
            assert entry.details["changes"]["minutes_per_page"] == 12

    def test_an_unknown_setting_is_rejected(self, admin_client: TestClient) -> None:
        response = admin_client.patch(
            "/admin/api/v1/settings", json={"session_secret": "z" * 40}
        )
        assert response.status_code == 422

    def test_the_service_layer_refuses_a_name_off_the_allow_list(
        self,
        session_factory: sessionmaker[Session],
        settings,
        admin_id: str,
    ) -> None:
        """The allow-list must hold on its own, not only via the request schema.

        Two independent guards reject an unknown name: the request model forbids
        extra fields, and ``update_settings`` checks EDITABLE_SETTINGS. Testing
        only through HTTP would leave the service-layer guard unpinned, so a
        future refactor that relaxed the schema would silently make secrets
        writable. This asserts the inner guard directly.
        """
        from server.services.admin_operations import (
            SettingOutOfRange,
            UnknownSetting,
            update_settings,
        )

        with session_factory() as session:
            with pytest.raises(UnknownSetting):
                update_settings(
                    session,
                    settings,
                    changes={"session_secret": 1},
                    admin_id=admin_id,
                )
            # And bounds are enforced there too, not only by the schema.
            with pytest.raises(SettingOutOfRange):
                update_settings(
                    session,
                    settings,
                    changes={"max_pdf_pages": 0},
                    admin_id=admin_id,
                )

    def test_nothing_is_written_when_a_change_is_rejected(
        self,
        session_factory: sessionmaker[Session],
        settings,
        admin_id: str,
    ) -> None:
        """A batch containing one bad name must write none of it."""
        from server.models import OperationalSetting
        from server.services.admin_operations import UnknownSetting, update_settings

        with session_factory() as session:
            with pytest.raises(UnknownSetting):
                update_settings(
                    session,
                    settings,
                    changes={"max_pdf_pages": 5, "session_secret": 1},
                    admin_id=admin_id,
                )
            session.rollback()

        with session_factory() as session:
            assert session.scalars(select(OperationalSetting)).all() == []

    def test_out_of_range_values_are_rejected(self, admin_client: TestClient) -> None:
        assert (
            admin_client.patch(
                "/admin/api/v1/settings", json={"max_pdf_pages": 0}
            ).status_code
            == 422
        )
        assert (
            admin_client.patch(
                "/admin/api/v1/settings", json={"acceptance_ttl_seconds": 1}
            ).status_code
            == 422
        )

    def test_an_existing_order_keeps_its_acceptance_deadline(
        self,
        client: TestClient,
        admin_client: TestClient,
        session_factory: sessionmaker[Session],
    ) -> None:
        """A delivered order's snapshot must survive a later policy change."""
        from server.models import Order

        authenticate(client)
        delivered = deliver_v1_order(client, pages=2)
        with session_factory() as session:
            before = session.get(Order, delivered["order_id"]).acceptance_deadline

        admin_client.patch(
            "/admin/api/v1/settings", json={"acceptance_ttl_seconds": 600}
        )

        with session_factory() as session:
            assert session.get(Order, delivered["order_id"]).acceptance_deadline == (
                before
            )

    def test_settings_require_a_csrf_token(self, client: TestClient, admin_id: str) -> None:
        naked = TestClient(client.app)
        admin_login(naked)

        response = naked.patch("/admin/api/v1/settings", json={"max_pdf_pages": 5})

        assert response.status_code == 403


class TestUsers:
    def test_user_detail_reports_the_refund_metrics(
        self,
        client: TestClient,
        admin_client: TestClient,
    ) -> None:
        user = authenticate(client, "test-parent-metrics")
        pay_for_new_order(client, pages=2)

        body = admin_client.get(f"/admin/api/v1/users/{user['public_id']}").json()

        assert body["public_id"] == user["public_id"]
        assert body["lifetime_paid_cents"] == 1000
        assert body["lifetime_user_refunded_cents"] == 0
        assert body["technical_refunded_cents"] == 0
        assert body["monthly_user_refund_count"] == 0
        assert body["lifetime_refund_ratio"] == 0.0
        assert body["order_count"] == 1

    def test_user_detail_never_exposes_the_openid(
        self,
        client: TestClient,
        admin_client: TestClient,
    ) -> None:
        """openid is the WeChat identifier; the public id is what admins use."""
        user = authenticate(client, "test-parent-openid")
        pay_for_new_order(client, pages=2)

        raw = admin_client.get(f"/admin/api/v1/users/{user['public_id']}").text

        assert "openid" not in raw

    def test_an_unknown_user_is_a_404(self, admin_client: TestClient) -> None:
        assert admin_client.get("/admin/api/v1/users/u-nope").status_code == 404


class TestFunds:
    def test_funds_summarise_payments_and_refunds(
        self,
        authenticated_client: TestClient,
        admin_client: TestClient,
    ) -> None:
        make_refund_request(authenticated_client, pages=2)

        body = admin_client.get("/admin/api/v1/funds").json()

        assert body["payments"]["succeeded_cents"] > 0
        assert body["refunds"]["refunded_cents"] >= 0
        assert body["refunds"]["failed_count"] == 0

    def test_funds_do_not_claim_a_bank_withdrawal_happened(
        self,
        admin_client: TestClient,
    ) -> None:
        body = admin_client.get("/admin/api/v1/funds").json()

        # Settlement is only knowable from an authoritative statement, which we
        # do not import yet. Reporting a figure here would be a fabrication.
        assert body["reconciliation"]["source"] == "none"
        assert body["reconciliation"]["settled_to_bank_cents"] is None


class TestAuditView:
    def test_audit_entries_can_be_filtered(
        self,
        client: TestClient,
        admin_client: TestClient,
        admin_id: str,
    ) -> None:
        from tests.server.conftest import register_worker

        worker_id = register_worker(client, installation_id="install-audit-view")[
            "worker_id"
        ]
        admin_client.post(f"/admin/api/v1/workers/{worker_id}/drain")

        body = admin_client.get(
            "/admin/api/v1/audit", params={"action": "worker.drain"}
        ).json()

        assert [entry["action"] for entry in body["items"]] == ["worker.drain"]
        assert body["items"][0]["actor_id"] == admin_id
        assert body["items"][0]["target_id"] == worker_id

    def test_audit_can_be_filtered_by_actor_and_target(
        self,
        client: TestClient,
        admin_client: TestClient,
        admin_id: str,
    ) -> None:
        admin_client.post(
            "/admin/api/v1/settings/price-rules",
            json={"service_tier": "annotated_review", "cents_per_page": 1400},
        )

        by_actor = admin_client.get(
            "/admin/api/v1/audit", params={"actor_id": admin_id}
        ).json()
        by_target = admin_client.get(
            "/admin/api/v1/audit", params={"target_type": "price_rule"}
        ).json()

        assert by_actor["items"]
        assert by_target["items"]
        assert all(entry["actor_id"] == admin_id for entry in by_actor["items"])

    def test_the_audit_api_has_no_write_route(self, admin_client: TestClient) -> None:
        """Append-only: the log is evidence, so it must not be editable.

        A write action comes first so there is genuinely a row to attack. The
        earlier version of this test wrapped its assertions in ``if entries:``
        and the fixture produced none, so it passed without checking anything —
        the audit routes could have been made writable and it would still have
        been green.
        """
        admin_client.post(
            "/admin/api/v1/settings/price-rules",
            json={"service_tier": "annotated_review", "cents_per_page": 1700},
        )

        entries = admin_client.get("/admin/api/v1/audit").json()["items"]

        assert entries, "the write above must have produced an audit row"
        entry_id = entries[0]["id"]
        for method in ("patch", "delete", "put"):
            response = getattr(admin_client, method)(
                f"/admin/api/v1/audit/{entry_id}"
            )
            assert response.status_code in {404, 405}, method

        # And the collection itself must not accept a write either.
        assert admin_client.post("/admin/api/v1/audit", json={}).status_code in {
            404,
            405,
        }

    def test_audit_details_carry_no_plaintext_secret(
        self,
        admin_client: TestClient,
    ) -> None:
        admin_client.post(
            "/admin/api/v1/settings/price-rules",
            json={"service_tier": "annotated_review", "cents_per_page": 1600},
        )

        raw = admin_client.get("/admin/api/v1/audit").text

        for secret in (ADMIN_PASSWORD, ADMIN_SHARED_KEY, SHARED_KEY):
            assert secret not in raw

    def test_audit_is_paginated_newest_first(
        self,
        admin_client: TestClient,
    ) -> None:
        for cents in (1100, 1200, 1300):
            admin_client.post(
                "/admin/api/v1/settings/price-rules",
                json={"service_tier": "annotated_review", "cents_per_page": cents},
            )

        body = admin_client.get("/admin/api/v1/audit", params={"page_size": 2}).json()

        assert len(body["items"]) == 2
        assert body["items"][0]["created_at"] >= body["items"][1]["created_at"]

    def test_a_miniapp_token_cannot_read_the_audit_log(
        self,
        authenticated_client: TestClient,
    ) -> None:
        assert authenticated_client.get("/admin/api/v1/audit").status_code == 401
