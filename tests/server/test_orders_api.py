from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from server.domain.states import JobState, OrderState
from server.models import GradingJob, Order
from tests.server.conftest import authenticate, create_quote


def pay(client: TestClient, quote_id: str) -> None:
    body = client.post("/api/v1/payments/prepay", json={"quote_id": quote_id}).json()
    response = client.post(
        "/callbacks/fake/pay",
        json={"fake_transaction_id": body["prepay_id"], "status": "SUCCESS"},
    )
    assert response.status_code == 204


def place_order(client: TestClient, **quote_kwargs) -> str:
    quote = create_quote(client, **quote_kwargs)
    pay(client, quote["id"])
    return quote["id"]


def order_id_for_quote(
    session_factory: sessionmaker[Session],
    quote_id: str,
) -> str:
    with session_factory() as session:
        return session.scalars(
            select(Order).where(Order.quote_session_id == quote_id)
        ).one().id


def set_state(
    session_factory: sessionmaker[Session],
    order_id: str,
    state: OrderState,
) -> None:
    with session_factory() as session:
        order = session.get(Order, order_id)
        order.state = state
        session.add(order)
        session.commit()


@pytest.fixture
def alice_client(client: TestClient) -> TestClient:
    authenticate(client, "test-alice")
    return client


@pytest.fixture
def bob_order(client: TestClient, session_factory: sessionmaker[Session]) -> Order:
    authenticate(client, "test-bob")
    quote_id = place_order(client)
    with session_factory() as session:
        order = session.scalars(
            select(Order).where(Order.quote_session_id == quote_id)
        ).one()
    del client.headers["Authorization"]
    return order


def test_order_list_never_returns_another_users_order(
    alice_client: TestClient,
    bob_order: Order,
) -> None:
    authenticate(alice_client, "test-alice")
    place_order(alice_client)

    response = alice_client.get("/api/v1/orders")

    assert response.status_code == 200
    assert bob_order.id not in {item["id"] for item in response.json()["items"]}


def test_order_detail_never_returns_another_users_order(
    alice_client: TestClient,
    bob_order: Order,
) -> None:
    authenticate(alice_client, "test-alice")
    own_order = order_id_for_quote(
        alice_client.app.state.session_factory, place_order(alice_client)
    )
    assert alice_client.get(f"/api/v1/orders/{own_order}").status_code == 200

    response = alice_client.get(f"/api/v1/orders/{bob_order.id}")

    assert response.status_code == 404
    assert bob_order.id not in response.text


def test_order_list_requires_authentication(client: TestClient) -> None:
    assert client.get("/api/v1/orders").status_code == 401


def test_order_list_ignores_a_user_id_query_parameter(
    alice_client: TestClient,
    bob_order: Order,
) -> None:
    authenticate(alice_client, "test-alice")
    own_quote = place_order(alice_client)

    response = alice_client.get(
        "/api/v1/orders",
        params={"user_id": bob_order.quote_session_id},
    )

    assert response.status_code == 200
    identifiers = {item["id"] for item in response.json()["items"]}
    assert bob_order.id not in identifiers
    assert len(identifiers) == 1
    assert own_quote is not None


def test_order_detail_exposes_the_queued_v1_round(
    authenticated_client: TestClient,
    session_factory: sessionmaker[Session],
) -> None:
    quote_id = place_order(
        authenticated_client,
        pages=3,
        grading_standard="cmo",
        note="第一题请复核",
    )
    order_id = order_id_for_quote(session_factory, quote_id)

    body = authenticated_client.get(f"/api/v1/orders/{order_id}").json()

    assert body["id"] == order_id
    assert body["state"] == OrderState.V1_QUEUED
    assert body["category"] == "grading"
    assert body["page_count"] == 3
    assert body["paid_amount_cents"] == 1500
    assert body["service_tier"] == "annotated_review"
    assert body["grading_standard"] == "cmo"
    assert body["note"] == "第一题请复核"
    assert body["current_round_number"] == 1
    assert body["rounds"] == [
        {
            "round_number": 1,
            "service_tier": "annotated_review",
            "state": "queued",
            "progress_stage": "queued",
            "delivered_at": None,
        }
    ]


def test_order_list_items_carry_the_summary_fields(
    authenticated_client: TestClient,
) -> None:
    place_order(authenticated_client, pages=2, grading_standard="imo")

    item = authenticated_client.get("/api/v1/orders").json()["items"][0]

    assert set(item) == {
        "id",
        "state",
            "category",
            "service_tier",
            "service_tier_label",
            "grading_standard",
        "page_count",
        "paid_amount_cents",
        "current_round_number",
        "progress_stage",
        "created_at",
    }
    assert item["progress_stage"] == "queued"


@pytest.mark.parametrize(
    ("job_state", "current_phase", "expected"),
    [
        (JobState.QUEUED, None, "queued"),
        (JobState.LEASED, None, "assigned"),
        (JobState.RUNNING, None, "assigned"),
        (JobState.RUNNING, "grading", "assigned"),
        (JobState.RUNNING, "understanding", "understanding"),
        (JobState.RUNNING, "validating", "validating"),
        (JobState.UPLOADING, "validating", "uploading"),
        (JobState.WORKER_EXCEPTION, "scoring", "system_processing"),
        (JobState.SUCCEEDED, "validating", None),
        (JobState.CANCELLED, "verifying", None),
    ],
)
def test_public_progress_uses_only_the_stable_stage_vocabulary(
    authenticated_client: TestClient,
    session_factory: sessionmaker[Session],
    job_state: JobState,
    current_phase: str | None,
    expected: str | None,
) -> None:
    quote_id = place_order(authenticated_client)
    order_id = order_id_for_quote(session_factory, quote_id)
    with session_factory() as session:
        job = session.scalars(
            select(GradingJob).where(GradingJob.order_id == order_id)
        ).one()
        job.state = job_state
        job.current_phase = current_phase
        session.add(job)
        session.commit()

    body = authenticated_client.get(
        "/api/v1/orders/progress", params=[("order_ids", order_id)]
    ).json()

    assert body == {
        "items": [
            {
                "id": order_id,
                "state": OrderState.V1_QUEUED,
                "current_round_number": 1,
                "progress_stage": expected,
            }
        ]
    }


def test_progress_batch_is_bounded_and_never_returns_another_users_order(
    client: TestClient,
    session_factory: sessionmaker[Session],
) -> None:
    authenticate(client, "test-progress-owner")
    owned = order_id_for_quote(session_factory, place_order(client))
    authenticate(client, "test-progress-other")
    foreign = order_id_for_quote(session_factory, place_order(client))
    authenticate(client, "test-progress-owner")

    response = client.get(
        "/api/v1/orders/progress",
        params=[("order_ids", owned), ("order_ids", foreign)],
    )

    assert response.status_code == 200
    assert [item["id"] for item in response.json()["items"]] == [owned]
    assert client.get("/api/v1/orders/progress").status_code == 422
    too_many = [("order_ids", f"order-{index}") for index in range(51)]
    assert client.get("/api/v1/orders/progress", params=too_many).status_code == 422


@pytest.mark.parametrize(
    ("state", "categories"),
    [
        (OrderState.V1_QUEUED, {"all", "grading"}),
        (OrderState.V1_RUNNING, {"all", "grading"}),
        (OrderState.V2_QUEUED, {"all", "grading"}),
        (OrderState.V2_RUNNING, {"all", "grading"}),
        (OrderState.V1_DELIVERED, {"all", "acceptance"}),
        (OrderState.V2_DELIVERED, {"all", "acceptance"}),
        (OrderState.REFUND_PENDING, {"all", "acceptance"}),
        (OrderState.ACCEPTED, {"all"}),
        (OrderState.REFUNDED, {"all"}),
        (OrderState.AWAITING_PAYMENT, {"all"}),
    ],
)
def test_category_filters_follow_the_state_mapping(
    authenticated_client: TestClient,
    session_factory: sessionmaker[Session],
    state: OrderState,
    categories: set[str],
) -> None:
    quote_id = place_order(authenticated_client)
    order_id = order_id_for_quote(session_factory, quote_id)
    set_state(session_factory, order_id, state)

    for category in ("all", "grading", "acceptance"):
        response = authenticated_client.get(
            "/api/v1/orders", params={"category": category}
        )
        assert response.status_code == 200
        identifiers = {item["id"] for item in response.json()["items"]}
        if category in categories:
            assert order_id in identifiers, category
        else:
            assert order_id not in identifiers, category


def test_worker_exception_jobs_stay_in_the_grading_category(
    authenticated_client: TestClient,
    session_factory: sessionmaker[Session],
) -> None:
    from server.domain.states import JobState
    from server.models import GradingJob

    quote_id = place_order(authenticated_client)
    order_id = order_id_for_quote(session_factory, quote_id)
    with session_factory() as session:
        job = session.scalars(
            select(GradingJob).where(GradingJob.order_id == order_id)
        ).one()
        job.state = JobState.WORKER_EXCEPTION
        session.add(job)
        session.commit()

    body = authenticated_client.get(
        "/api/v1/orders", params={"category": "grading"}
    ).json()

    assert order_id in {item["id"] for item in body["items"]}


def test_unknown_category_is_refused(authenticated_client: TestClient) -> None:
    response = authenticated_client.get(
        "/api/v1/orders", params={"category": "everything"}
    )

    assert response.status_code == 422


def test_order_list_is_sorted_by_created_at_then_id_descending(
    authenticated_client: TestClient,
    session_factory: sessionmaker[Session],
) -> None:
    order_ids = [
        order_id_for_quote(session_factory, place_order(authenticated_client))
        for _ in range(4)
    ]
    shared = datetime(2026, 3, 1, tzinfo=timezone.utc)
    with session_factory() as session:
        for order_id in order_ids:
            order = session.get(Order, order_id)
            order.created_at = shared
            session.add(order)
        session.commit()

    items = authenticated_client.get("/api/v1/orders").json()["items"]

    assert [item["id"] for item in items] == sorted(order_ids, reverse=True)


def test_keyset_pagination_walks_every_order_without_repeats(
    authenticated_client: TestClient,
    session_factory: sessionmaker[Session],
) -> None:
    created = [
        order_id_for_quote(session_factory, place_order(authenticated_client))
        for _ in range(5)
    ]
    base = datetime(2026, 3, 1, tzinfo=timezone.utc)
    with session_factory() as session:
        for index, order_id in enumerate(created):
            order = session.get(Order, order_id)
            order.created_at = base + timedelta(minutes=index)
            session.add(order)
        session.commit()

    seen: list[str] = []
    params: dict[str, object] = {"limit": 2}
    while True:
        body = authenticated_client.get("/api/v1/orders", params=params).json()
        seen.extend(item["id"] for item in body["items"])
        if body["next_cursor"] is None:
            break
        params = {"limit": 2, "cursor": body["next_cursor"]}

    assert seen == list(reversed(created))
    assert len(seen) == len(set(seen))


def test_pagination_cursor_is_scoped_to_the_authenticated_user(
    client: TestClient,
    session_factory: sessionmaker[Session],
) -> None:
    authenticate(client, "test-alice")
    alice_order = order_id_for_quote(session_factory, place_order(client))
    alice_cursor = client.get("/api/v1/orders", params={"limit": 1}).json()
    authenticate(client, "test-bob")
    bob_order = order_id_for_quote(session_factory, place_order(client))

    body = client.get(
        "/api/v1/orders",
        params={"limit": 10, "cursor": alice_cursor["next_cursor"] or ""},
    ).json()

    assert alice_order not in {item["id"] for item in body["items"]}
    assert {item["id"] for item in body["items"]} <= {bob_order}


def test_malformed_cursor_is_refused(authenticated_client: TestClient) -> None:
    response = authenticated_client.get(
        "/api/v1/orders", params={"cursor": "not-a-cursor"}
    )

    assert response.status_code == 422


def test_empty_order_list_is_returned_for_a_new_user(
    authenticated_client: TestClient,
) -> None:
    body = authenticated_client.get("/api/v1/orders").json()

    assert body == {"items": [], "next_cursor": None}


def test_unknown_order_detail_returns_404(authenticated_client: TestClient) -> None:
    response = authenticated_client.get("/api/v1/orders/missing")

    assert response.status_code == 404
    assert response.json()["detail"] == "订单不存在。"
