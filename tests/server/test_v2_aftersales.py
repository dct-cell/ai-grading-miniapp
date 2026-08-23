"""V2 acceptance and refund, and the available_actions contract.

V2 is the last round. Its delivery window offers accept or a full refund only:
there is no third grading round, and the API must refuse a V2 review even
though the state machine would permit V2_QUEUED -> REFUND_PENDING.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from server.domain.states import OrderState
from server.models import Appeal, GradingJob, GradingRound, Order, Refund
from tests.server.conftest import (
    authenticate,
    deliver_round,
    deliver_v1_order,
    pay_for_new_order,
)


def count(session: Session, model) -> int:
    return session.scalar(select(func.count()).select_from(model))


def deliver_v2_order(client: TestClient) -> dict:
    """Drive an order through V1 delivery, a review, and V2 delivery."""
    delivered = deliver_v1_order(client)
    order_id = delivered["order_id"]
    review = client.post(
        f"/api/v1/orders/{order_id}/review", json={"text": "请复核第2 题"}
    )
    assert review.status_code == 202, review.text
    second = deliver_round(
        client, delivered["worker_id"], expected_order_id=order_id
    )
    assert second["round_number"] == 2
    return {"order_id": order_id, "worker_id": delivered["worker_id"]}


@pytest.fixture
def v2_order(authenticated_client: TestClient) -> str:
    return deliver_v2_order(authenticated_client)["order_id"]


def test_v2_delivery_reopens_the_acceptance_window(
    authenticated_client: TestClient,
    v2_order: str,
    session_factory: sessionmaker[Session],
) -> None:
    with session_factory() as session:
        order = session.get(Order, v2_order)

    assert order.state == OrderState.V2_DELIVERED
    assert order.current_round_number == 2
    assert order.acceptance_deadline is not None


def test_v2_has_no_review_endpoint(
    authenticated_client: TestClient,
    v2_order: str,
    session_factory: sessionmaker[Session],
) -> None:
    """One review per order, ever. A second must not queue a third round."""
    response = authenticated_client.post(
        f"/api/v1/orders/{v2_order}/review",
        json={"text": "third attempt"},
    )

    assert response.status_code == 409
    with session_factory() as session:
        rounds = session.scalars(
            select(GradingRound).where(GradingRound.order_id == v2_order)
        ).all()
        jobs = session.scalars(
            select(GradingJob).where(GradingJob.order_id == v2_order)
        ).all()
        appeals = session.scalars(
            select(Appeal).where(Appeal.order_id == v2_order)
        ).all()

    assert {record.round_number for record in rounds} == {1, 2}
    assert {job.round_number for job in jobs} == {1, 2}
    assert len(appeals) == 1, "the original review must still be the only one"


def test_v2_allows_full_refund(
    authenticated_client: TestClient,
    v2_order: str,
    session_factory: sessionmaker[Session],
) -> None:
    with session_factory() as session:
        paid_amount_cents = session.get(Order, v2_order).paid_amount_cents

    response = authenticated_client.post(
        f"/api/v1/orders/{v2_order}/refund",
        json={"reason": "grading_disputed"},
    )

    assert response.status_code in {200, 202}, response.text
    assert response.json()["amount_cents"] == paid_amount_cents
    with session_factory() as session:
        refunds = session.scalars(select(Refund)).all()
    assert len(refunds) == 1
    assert refunds[0].amount_cents == paid_amount_cents


def test_v2_accept_closes_the_order(
    authenticated_client: TestClient,
    v2_order: str,
    session_factory: sessionmaker[Session],
) -> None:
    response = authenticated_client.post(f"/api/v1/orders/{v2_order}/accept")

    assert response.status_code == 200, response.text
    with session_factory() as session:
        assert session.get(Order, v2_order).state == OrderState.ACCEPTED


def test_available_actions_track_the_order_lifecycle(
    authenticated_client: TestClient,
    session_factory: sessionmaker[Session],
) -> None:
    """The mini-program renders buttons from this list, so pin every stage."""

    def actions(order_id: str) -> list[str]:
        response = authenticated_client.get(f"/api/v1/orders/{order_id}")
        assert response.status_code == 200, response.text
        return response.json()["available_actions"]

    # V1 delivered: all three actions.
    delivered = deliver_v1_order(authenticated_client)
    v1_order = delivered["order_id"]
    assert actions(v1_order) == ["accept", "review", "refund"]

    # V2 queued: nothing to accept until the re-grade lands.
    authenticated_client.post(
        f"/api/v1/orders/{v1_order}/review", json={"text": "复核一次"}
    )
    assert actions(v1_order) == ["refund"]

    # V2 delivered: no third round.
    deliver_round(
        authenticated_client, delivered["worker_id"], expected_order_id=v1_order
    )
    assert actions(v1_order) == ["accept", "refund"]

    # Accepted: terminal, nothing left to do.
    authenticated_client.post(f"/api/v1/orders/{v1_order}/accept")
    assert actions(v1_order) == []

    # A freshly paid order that nobody has graded yet: refund only.
    queued_order = pay_for_new_order(authenticated_client, pages=3)
    assert actions(queued_order) == ["refund"]


def test_available_actions_drop_review_after_the_window_closes(
    authenticated_client: TestClient,
    session_factory: sessionmaker[Session],
) -> None:
    """An expired window leaves accept only, matching what the API enforces."""
    order_id = deliver_v1_order(authenticated_client)["order_id"]
    with session_factory() as session:
        order = session.get(Order, order_id)
        order.acceptance_deadline = datetime.now(timezone.utc) - timedelta(seconds=1)
        session.add(order)
        session.commit()

    detail = authenticated_client.get(f"/api/v1/orders/{order_id}").json()

    assert detail["available_actions"] == ["accept"]


def test_refunded_order_offers_no_actions(
    authenticated_client: TestClient,
    session_factory: sessionmaker[Session],
) -> None:
    order_id = deliver_v1_order(authenticated_client)["order_id"]
    authenticated_client.post(
        f"/api/v1/orders/{order_id}/refund", json={"reason": "uploaded_wrong_pdf"}
    )

    detail = authenticated_client.get(f"/api/v1/orders/{order_id}").json()

    assert detail["available_actions"] == []
    assert detail["state"] in {"refund_pending", "refunded"}


def test_order_detail_reports_the_appeal_text(
    authenticated_client: TestClient,
) -> None:
    """The mini-program shows the review reason back to the user."""
    order_id = deliver_v1_order(authenticated_client)["order_id"]
    authenticated_client.post(
        f"/api/v1/orders/{order_id}/review", json={"text": "第2题下界证明判断有误"}
    )

    detail = authenticated_client.get(f"/api/v1/orders/{order_id}").json()

    assert detail["appeal_text"] == "第2题下界证明判断有误"


def test_order_detail_omits_appeal_text_when_there_was_no_review(
    authenticated_client: TestClient,
) -> None:
    order_id = deliver_v1_order(authenticated_client)["order_id"]

    detail = authenticated_client.get(f"/api/v1/orders/{order_id}").json()

    assert detail["appeal_text"] is None


def test_available_actions_are_not_leaked_for_other_users(
    client: TestClient,
) -> None:
    """Order detail stays 404 for a non-owner, actions and all."""
    authenticate(client, code="test-v2-owner")
    order_id = deliver_v1_order(client)["order_id"]

    authenticate(client, code="test-v2-stranger")

    assert client.get(f"/api/v1/orders/{order_id}").status_code == 404
