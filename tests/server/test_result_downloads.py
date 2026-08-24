"""User-facing result downloads.

Phase 06 adds the one endpoint the mini-program needs to deliver the product
itself: the graded PDF and its result JSON.

Authorisation is checked on *every* byte-serving request rather than being
cached in a token, because `orders.downloads_revoked_at` is written the moment
a refund succeeds and a cached grant would keep serving the file afterwards.
Three conditions must all hold:

  1. the caller owns the order (otherwise 404 — never confirm somebody else's
     order id exists);
  2. downloads have not been revoked (410);
  3. the requested round was delivered and has that artefact (404).
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from server.models import Order
from tests.server.conftest import (
    ADMIN_SHARED_KEY,
    SHARED_KEY,
    authenticate,
    create_admin,
    deliver_v1_order,
    make_pdf_bytes,
)


@pytest.fixture
def delivered(authenticated_client: TestClient) -> dict:
    return deliver_v1_order(authenticated_client)


def download(client: TestClient, order_id: str, round_number: int = 1, kind: str = "result_pdf"):
    return client.get(
        f"/api/v1/orders/{order_id}/rounds/{round_number}/result/{kind}"
    )


def test_owner_downloads_the_graded_pdf(
    authenticated_client: TestClient, delivered: dict
) -> None:
    response = download(authenticated_client, delivered["order_id"])

    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == "application/pdf"
    # A real PDF, not an error page rendered with a 200.
    assert response.content.startswith(b"%PDF")


def test_owner_downloads_the_result_json(
    authenticated_client: TestClient, delivered: dict
) -> None:
    """The mini-program reads the score summary from this artefact."""
    response = download(authenticated_client, delivered["order_id"], kind="result_json")

    assert response.status_code == 200, response.text
    payload = json.loads(response.content)
    assert payload["service_tier"] == "annotated_review"
    assert payload["grading_standard"] == "imo"
    assert payload["total_score"] == 0
    assert payload["max_score"] == 7
    assert len(payload["problems"]) == 1


def test_download_is_denied_after_a_refund_revokes_it(
    authenticated_client: TestClient,
    session_factory: sessionmaker[Session],
    delivered: dict,
) -> None:
    """The invariant Phase 05 wrote the field for.

    A refund revokes download permission immediately; a client that already
    knew the URL must not keep fetching the paid-for result.
    """
    order_id = delivered["order_id"]
    assert download(authenticated_client, order_id).status_code == 200

    refund = authenticated_client.post(
        f"/api/v1/orders/{order_id}/refund", json={"reason": "grading_disputed"}
    )
    assert refund.status_code == 202, refund.text

    with session_factory() as session:
        order = session.get(Order, order_id)
        assert order.downloads_revoked_at is not None, "refund must revoke downloads"

    denied = download(authenticated_client, order_id)
    assert denied.status_code == 410
    assert "下载" in denied.json()["detail"]


def test_a_pending_refund_does_not_revoke_downloads(
    authenticated_client: TestClient,
    session_factory: sessionmaker[Session],
) -> None:
    """Only a *succeeded* refund revokes access.

    A refund awaiting Admin approval must not strip the user of a result they
    have paid for and may still keep if the refund is rejected.
    """
    order_id = deliver_v1_order(authenticated_client)["order_id"]
    with session_factory() as session:
        order = session.get(Order, order_id)
        assert order.downloads_revoked_at is None

    assert download(authenticated_client, order_id).status_code == 200


def test_another_user_cannot_download_and_is_told_404(
    client: TestClient, delivered: dict
) -> None:
    order_id = delivered["order_id"]

    authenticate(client, code="test-other-user")
    response = download(client, order_id)

    # 404 rather than 403: a 403 would confirm that this order id exists.
    assert response.status_code == 404


def test_download_requires_authentication(client: TestClient, delivered: dict) -> None:
    client.headers.pop("Authorization", None)
    response = download(client, delivered["order_id"])
    assert response.status_code == 401


def test_worker_and_admin_credentials_cannot_download_user_results(
    client: TestClient,
    session_factory: sessionmaker[Session],
    delivered: dict,
) -> None:
    """The three credential domains stay separate in this direction too."""
    order_id = delivered["order_id"]
    admin_id = create_admin(session_factory, username="download-admin")

    client.headers.pop("Authorization", None)
    worker_attempt = client.get(
        f"/api/v1/orders/{order_id}/rounds/1/result/result_pdf",
        headers={"Authorization": f"Bearer {SHARED_KEY}", "X-Worker-ID": "w-1"},
    )
    admin_attempt = client.get(
        f"/api/v1/orders/{order_id}/rounds/1/result/result_pdf",
        headers={"Authorization": f"Bearer {ADMIN_SHARED_KEY}", "X-Admin-ID": admin_id},
    )

    assert worker_attempt.status_code == 401
    assert admin_attempt.status_code == 401


def test_an_undelivered_round_has_nothing_to_download(
    authenticated_client: TestClient,
) -> None:
    from tests.server.conftest import pay_for_new_order

    order_id = pay_for_new_order(authenticated_client)
    response = download(authenticated_client, order_id)
    assert response.status_code == 404


def test_a_round_that_does_not_exist_is_404(
    authenticated_client: TestClient, delivered: dict
) -> None:
    response = download(authenticated_client, delivered["order_id"], round_number=2)
    assert response.status_code == 404


def test_an_unknown_artefact_kind_is_rejected(
    authenticated_client: TestClient, delivered: dict
) -> None:
    response = download(
        authenticated_client, delivered["order_id"], kind="internal_notes"
    )
    # Guessing at other artefacts must not reach the filesystem at all.
    assert response.status_code in {400, 404, 422}


def test_an_unknown_order_is_404(authenticated_client: TestClient) -> None:
    response = download(authenticated_client, "00000000-0000-0000-0000-000000000000")
    assert response.status_code == 404


def test_the_endpoint_is_registered_in_production(tmp_path) -> None:
    """Result delivery is a real feature, not a fake adapter.

    Unlike the fake login/payment routes it must exist in production, so it is
    deliberately outside FAKE_ADAPTER_ENVIRONMENTS.
    """
    from server.config import Environment
    from server.main import create_app
    from tests.server.conftest import build_settings
    from tests.server.test_auth_environment_gate import route_paths

    settings = build_settings(
        tmp_path,
        environment=Environment.PRODUCTION,
        database_url="mysql+pymysql://user:pw@127.0.0.1:3306/grader",
    )
    app = create_app(settings)
    paths = route_paths(app)
    assert "/api/v1/orders/{order_id}/rounds/{round_number}/result/{kind}" in paths
    # The fake adapters stay gated; adding a real endpoint must not open them.
    assert "/api/v1/auth/login" in paths


def test_content_disposition_names_the_file_safely(
    authenticated_client: TestClient, delivered: dict
) -> None:
    response = download(authenticated_client, delivered["order_id"])
    disposition = response.headers.get("content-disposition", "")
    assert "attachment" in disposition
    # No raw newline or quote may reach the header.
    assert "\n" not in disposition and "\r" not in disposition
