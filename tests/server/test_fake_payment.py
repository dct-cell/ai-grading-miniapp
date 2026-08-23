from __future__ import annotations

import hashlib
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session, sessionmaker

from server.config import Environment
from server.domain.states import JobState, OrderState
from server.models import (
    FileObject,
    GradingJob,
    GradingRound,
    Order,
    Payment,
    QuoteSession,
)
from server.services.files import FileState
from tests.server.conftest import (
    authenticate,
    build_client,
    build_settings,
    create_quote,
)


def prepay(client: TestClient, quote_id: str) -> dict:
    response = client.post("/api/v1/payments/prepay", json={"quote_id": quote_id})
    assert response.status_code == 201, response.text
    return response.json()


def callback_payload(prepay_body: dict, transaction_id: str | None = None) -> dict:
    return {
        "fake_transaction_id": transaction_id or prepay_body["prepay_id"],
        "status": "SUCCESS",
    }


def count(session: Session, model) -> int:
    return session.scalar(select(func.count()).select_from(model))


def test_duplicate_payment_callback_creates_one_order_and_one_job(
    authenticated_client: TestClient,
    quote_id: str,
    session_factory: sessionmaker[Session],
) -> None:
    body = prepay(authenticated_client, quote_id)
    payload = callback_payload(body)

    assert authenticated_client.post("/callbacks/fake/pay", json=payload).status_code == 204
    assert authenticated_client.post("/callbacks/fake/pay", json=payload).status_code == 204

    with session_factory() as session:
        assert count(session, Order) == 1
        assert count(session, GradingJob) == 1
        assert count(session, GradingRound) == 1
        assert count(session, Payment) == 1


def test_successful_callback_creates_the_full_v1_queue_record(
    authenticated_client: TestClient,
    session_factory: sessionmaker[Session],
) -> None:
    quote = create_quote(
        authenticated_client,
        pages=3,
        grading_standard="cmo",
        note="第二题请核对引理",
        reference_pages=1,
    )
    body = prepay(authenticated_client, quote["id"])

    response = authenticated_client.post(
        "/callbacks/fake/pay", json=callback_payload(body)
    )

    assert response.status_code == 204
    with session_factory() as session:
        order = session.scalars(select(Order)).one()
        round_one = session.scalars(select(GradingRound)).one()
        job = session.scalars(select(GradingJob)).one()
        payment = session.scalars(select(Payment)).one()
        stored_quote = session.get(QuoteSession, quote["id"])
        files = session.scalars(select(FileObject)).all()

    assert order.state == OrderState.V1_QUEUED
    assert order.paid_amount_cents == 1500
    assert order.current_round_number == 1
    assert order.quote_session_id == quote["id"]
    assert round_one.round_number == 1
    assert round_one.order_id == order.id
    assert round_one.grading_standard == "cmo"
    assert round_one.note == "第二题请核对引理"
    assert job.state == JobState.QUEUED
    assert job.round_number == 1
    assert job.order_id == order.id
    assert job.worker_id is None
    assert job.lease_version == 0
    assert job.attempt_count == 0
    assert payment.state == "succeeded"
    assert payment.amount_cents == 1500
    assert payment.external_transaction_id == body["prepay_id"]
    assert stored_quote.consumed_at is not None
    assert len(files) == 2
    assert {record.state for record in files} == {FileState.RETAINED}


def test_paid_files_are_promoted_without_moving_the_stored_bytes(
    authenticated_client: TestClient,
    session_factory: sessionmaker[Session],
    settings,
) -> None:
    quote = create_quote(authenticated_client, reference_pages=1)
    with session_factory() as session:
        stored_quote = session.get(QuoteSession, quote["id"])
        before = {
            record.id: (record.relative_path, record.sha256)
            for record in session.scalars(select(FileObject)).all()
        }
        assert stored_quote is not None
    body = prepay(authenticated_client, quote["id"])

    authenticated_client.post("/callbacks/fake/pay", json=callback_payload(body))

    with session_factory() as session:
        files = session.scalars(select(FileObject)).all()

    for record in files:
        assert record.state == FileState.RETAINED
        assert record.relative_path == before[record.id][0]
        path = settings.data_dir / record.relative_path
        assert path.is_file()
        assert (
            hashlib.sha256(path.read_bytes()).hexdigest() == before[record.id][1]
        )


def test_a_failed_commit_leaves_no_order_and_no_orphaned_file_state(
    authenticated_client: TestClient,
    session_factory: sessionmaker[Session],
    settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    quote = create_quote(authenticated_client, reference_pages=1)
    body = prepay(authenticated_client, quote["id"])
    with session_factory() as session:
        before = {
            record.id: (record.relative_path, record.state)
            for record in session.scalars(select(FileObject)).all()
        }

    from sqlalchemy.orm import Session as SqlSession

    original_commit = SqlSession.commit
    calls = {"count": 0}

    def failing_commit(self) -> None:
        calls["count"] += 1
        raise OperationalError("commit", {}, Exception("database is locked"))

    monkeypatch.setattr(SqlSession, "commit", failing_commit)
    response = authenticated_client.post(
        "/callbacks/fake/pay", json=callback_payload(body)
    )
    monkeypatch.setattr(SqlSession, "commit", original_commit)

    assert calls["count"] >= 1
    assert response.status_code >= 400
    with session_factory() as session:
        assert count(session, Order) == 0
        assert count(session, GradingJob) == 0
        assert count(session, GradingRound) == 0
        assert session.get(QuoteSession, quote["id"]).consumed_at is None
        assert session.get(Payment, body["payment_id"]).state == "pending"
        after = {
            record.id: (record.relative_path, record.state)
            for record in session.scalars(select(FileObject)).all()
        }

    assert after == before
    for relative_path, _ in before.values():
        assert (settings.data_dir / relative_path).is_file()


def test_front_end_success_without_a_callback_creates_no_order(
    authenticated_client: TestClient,
    quote_id: str,
    session_factory: sessionmaker[Session],
) -> None:
    prepay(authenticated_client, quote_id)

    with session_factory() as session:
        assert count(session, Order) == 0
        assert count(session, GradingJob) == 0
        assert session.get(QuoteSession, quote_id).consumed_at is None
        assert session.scalars(select(Payment)).one().state == "pending"


def test_forged_callback_for_an_unknown_transaction_creates_no_order(
    authenticated_client: TestClient,
    quote_id: str,
    session_factory: sessionmaker[Session],
) -> None:
    prepay(authenticated_client, quote_id)

    response = authenticated_client.post(
        "/callbacks/fake/pay",
        json={"fake_transaction_id": "fake-forged-order", "status": "SUCCESS"},
    )

    assert response.status_code == 404
    with session_factory() as session:
        assert count(session, Order) == 0
        assert count(session, GradingJob) == 0


def test_callback_with_a_failed_status_creates_no_order(
    authenticated_client: TestClient,
    quote_id: str,
    session_factory: sessionmaker[Session],
) -> None:
    body = prepay(authenticated_client, quote_id)

    response = authenticated_client.post(
        "/callbacks/fake/pay",
        json={"fake_transaction_id": body["prepay_id"], "status": "FAIL"},
    )

    assert response.status_code == 400
    with session_factory() as session:
        assert count(session, Order) == 0
        assert session.scalars(select(Payment)).one().state == "pending"


def test_callback_rejects_an_amount_mismatch(
    authenticated_client: TestClient,
    quote_id: str,
    session_factory: sessionmaker[Session],
) -> None:
    body = prepay(authenticated_client, quote_id)
    with session_factory() as session:
        quote = session.get(QuoteSession, quote_id)
        quote.quoted_amount_cents = 999
        session.add(quote)
        session.commit()

    response = authenticated_client.post(
        "/callbacks/fake/pay", json=callback_payload(body)
    )

    assert response.status_code == 409
    with session_factory() as session:
        assert count(session, Order) == 0
        assert count(session, GradingJob) == 0
        assert session.get(QuoteSession, quote_id).consumed_at is None


def test_callback_rejects_an_expired_quote(
    authenticated_client: TestClient,
    quote_id: str,
    session_factory: sessionmaker[Session],
) -> None:
    body = prepay(authenticated_client, quote_id)
    with session_factory() as session:
        quote = session.get(QuoteSession, quote_id)
        quote.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        session.add(quote)
        session.commit()

    response = authenticated_client.post(
        "/callbacks/fake/pay", json=callback_payload(body)
    )

    assert response.status_code == 409
    with session_factory() as session:
        assert count(session, Order) == 0
        assert count(session, GradingJob) == 0


def test_callback_rejects_a_quote_consumed_by_another_transaction(
    authenticated_client: TestClient,
    quote_id: str,
    session_factory: sessionmaker[Session],
) -> None:
    first = prepay(authenticated_client, quote_id)
    second = prepay(authenticated_client, quote_id)
    assert first["prepay_id"] != second["prepay_id"]
    assert (
        authenticated_client.post(
            "/callbacks/fake/pay", json=callback_payload(first)
        ).status_code
        == 204
    )

    response = authenticated_client.post(
        "/callbacks/fake/pay", json=callback_payload(second)
    )

    assert response.status_code == 409
    with session_factory() as session:
        assert count(session, Order) == 1
        assert count(session, GradingJob) == 1
        assert count(session, GradingRound) == 1


def test_concurrent_callbacks_create_exactly_one_order(
    authenticated_client: TestClient,
    quote_id: str,
    session_factory: sessionmaker[Session],
) -> None:
    body = prepay(authenticated_client, quote_id)
    payload = callback_payload(body)

    with ThreadPoolExecutor(max_workers=4) as executor:
        statuses = [
            response.status_code
            for response in executor.map(
                lambda _: authenticated_client.post("/callbacks/fake/pay", json=payload),
                range(4),
            )
        ]

    assert statuses.count(204) >= 1
    assert set(statuses) <= {204, 409}
    with session_factory() as session:
        assert count(session, Order) == 1
        assert count(session, GradingJob) == 1
        assert count(session, GradingRound) == 1
        assert count(session, Payment) == 1


def test_database_refuses_two_payments_with_one_transaction_id(
    authenticated_client: TestClient,
    session_factory: sessionmaker[Session],
) -> None:
    first = prepay(authenticated_client, create_quote(authenticated_client)["id"])
    second = prepay(authenticated_client, create_quote(authenticated_client)["id"])
    assert (
        authenticated_client.post(
            "/callbacks/fake/pay", json=callback_payload(first)
        ).status_code
        == 204
    )

    with session_factory() as session:
        loser = session.get(Payment, second["payment_id"])
        loser.external_transaction_id = first["prepay_id"]
        session.add(loser)
        with pytest.raises(IntegrityError):
            session.commit()

    with session_factory() as session:
        assert count(session, Order) == 1
        assert count(session, Payment) == 2
        assert (
            session.get(Payment, second["payment_id"]).external_transaction_id is None
        )


def test_database_refuses_two_orders_for_one_quote(
    authenticated_client: TestClient,
    quote_id: str,
    session_factory: sessionmaker[Session],
) -> None:
    body = prepay(authenticated_client, quote_id)
    authenticated_client.post("/callbacks/fake/pay", json=callback_payload(body))

    with session_factory() as session:
        session.add(
            Order(
                quote_session_id=quote_id,
                state=OrderState.V1_QUEUED,
                paid_amount_cents=2000,
                current_round_number=1,
            )
        )
        with pytest.raises(IntegrityError):
            session.commit()

    with session_factory() as session:
        assert count(session, Order) == 1


def test_prepay_requires_authentication(client: TestClient, tmp_path: Path) -> None:
    authenticate(client, "test-owner")
    quote = create_quote(client)
    del client.headers["Authorization"]

    response = client.post("/api/v1/payments/prepay", json={"quote_id": quote["id"]})

    assert response.status_code == 401


def test_prepay_refuses_another_users_quote(
    client: TestClient,
    session_factory: sessionmaker[Session],
) -> None:
    authenticate(client, "test-alice")
    alice_quote = create_quote(client)
    authenticate(client, "test-bob")

    response = client.post(
        "/api/v1/payments/prepay", json={"quote_id": alice_quote["id"]}
    )

    assert response.status_code == 404
    assert response.json()["detail"] == "报价不存在或已失效。"
    with session_factory() as session:
        assert count(session, Payment) == 0


def test_prepay_refuses_an_expired_quote(
    authenticated_client: TestClient,
    quote_id: str,
    session_factory: sessionmaker[Session],
) -> None:
    with session_factory() as session:
        quote = session.get(QuoteSession, quote_id)
        quote.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        session.add(quote)
        session.commit()

    response = authenticated_client.post(
        "/api/v1/payments/prepay", json={"quote_id": quote_id}
    )

    assert response.status_code == 409
    with session_factory() as session:
        assert count(session, Payment) == 0


def test_prepay_refuses_a_consumed_quote(
    authenticated_client: TestClient,
    quote_id: str,
) -> None:
    body = prepay(authenticated_client, quote_id)
    authenticated_client.post("/callbacks/fake/pay", json=callback_payload(body))

    response = authenticated_client.post(
        "/api/v1/payments/prepay", json={"quote_id": quote_id}
    )

    assert response.status_code == 409


def test_prepay_snapshots_the_quoted_amount(
    authenticated_client: TestClient,
    session_factory: sessionmaker[Session],
) -> None:
    quote = create_quote(authenticated_client, pages=4)

    body = prepay(authenticated_client, quote["id"])

    assert body["amount_cents"] == 2000
    with session_factory() as session:
        payment = session.scalars(select(Payment)).one()
    assert payment.amount_cents == 2000
    assert payment.quote_session_id == quote["id"]
    assert body["prepay_id"] == payment.prepay_id
    assert body["client_payload"] == {"fake_prepay_id": payment.prepay_id}


def test_simulate_success_creates_the_order_for_the_owner(
    authenticated_client: TestClient,
    quote_id: str,
    session_factory: sessionmaker[Session],
) -> None:
    body = prepay(authenticated_client, quote_id)

    response = authenticated_client.post(
        f"/api/v1/payments/{body['payment_id']}/simulate-success"
    )

    assert response.status_code == 204
    with session_factory() as session:
        assert count(session, Order) == 1
        assert session.scalars(select(GradingJob)).one().state == JobState.QUEUED


def test_simulate_success_refuses_another_users_payment(
    client: TestClient,
    session_factory: sessionmaker[Session],
) -> None:
    authenticate(client, "test-alice")
    alice_payment = prepay(client, create_quote(client)["id"])
    authenticate(client, "test-bob")

    response = client.post(
        f"/api/v1/payments/{alice_payment['payment_id']}/simulate-success"
    )

    assert response.status_code == 404
    with session_factory() as session:
        assert count(session, Order) == 0


def test_simulate_success_requires_authentication(
    client: TestClient,
) -> None:
    authenticate(client, "test-owner")
    payment = prepay(client, create_quote(client)["id"])
    del client.headers["Authorization"]

    response = client.post(f"/api/v1/payments/{payment['payment_id']}/simulate-success")

    assert response.status_code == 401


def test_simulate_success_is_idempotent(
    authenticated_client: TestClient,
    quote_id: str,
    session_factory: sessionmaker[Session],
) -> None:
    body = prepay(authenticated_client, quote_id)
    route = f"/api/v1/payments/{body['payment_id']}/simulate-success"

    assert authenticated_client.post(route).status_code == 204
    assert authenticated_client.post(route).status_code == 204

    with session_factory() as session:
        assert count(session, Order) == 1
        assert count(session, GradingJob) == 1


def route_paths(app) -> set[str]:
    """Collect every registered path, including nested included routers."""
    collected: set[str] = set()
    pending = [app.router]
    while pending:
        router = pending.pop()
        for route in getattr(router, "routes", []):
            nested = getattr(route, "original_router", None)
            if nested is not None:
                pending.append(nested)
                continue
            path = getattr(route, "path", None)
            if path is not None:
                collected.add(path)
    return collected


@pytest.mark.parametrize(
    "environment",
    [Environment.DEVELOPMENT, Environment.TEST, Environment.STAGING],
)
def test_simulate_success_route_is_registered_outside_production(
    tmp_path: Path,
    environment: Environment,
) -> None:
    settings = build_settings(tmp_path, environment=environment)
    with build_client(settings) as client:
        assert (
            "/api/v1/payments/{payment_id}/simulate-success"
            in route_paths(client.app)
        )
        assert (
            "/api/v1/payments/{payment_id}/simulate-success"
            in client.get("/openapi.json").json()["paths"]
        )
        assert "/callbacks/fake/pay" in route_paths(client.app)


def _production_settings(tmp_path: Path):
    return build_settings(
        tmp_path,
        environment=Environment.PRODUCTION,
        database_url="mysql+pymysql://grader:placeholder@127.0.0.1:3306/grader",
    )


def test_production_does_not_register_the_fake_payment_routes(
    tmp_path: Path,
) -> None:
    from server.main import create_app

    app = create_app(_production_settings(tmp_path))

    paths = route_paths(app)

    assert "/api/v1/payments/{payment_id}/simulate-success" not in paths
    assert "/callbacks/fake/pay" not in paths
    assert "/api/v1/payments/prepay" in paths


def test_production_openapi_omits_the_fake_payment_routes(tmp_path: Path) -> None:
    from server.main import create_app

    app = create_app(_production_settings(tmp_path))
    with TestClient(app) as client:
        documented = client.get("/openapi.json").json()["paths"]

    assert "/api/v1/payments/{payment_id}/simulate-success" not in documented
    assert "/callbacks/fake/pay" not in documented
    assert "/api/v1/payments/prepay" in documented


def test_production_fake_payment_requests_return_404(tmp_path: Path) -> None:
    from server.main import create_app

    app = create_app(_production_settings(tmp_path))
    with TestClient(app) as client:
        simulate = client.post("/api/v1/payments/any-payment-id/simulate-success")
        callback = client.post(
            "/callbacks/fake/pay",
            json={"fake_transaction_id": "fake-anything", "status": "SUCCESS"},
        )
        real_route = client.post("/api/v1/payments/prepay", json={"quote_id": "x"})

    assert simulate.status_code == 404
    assert callback.status_code == 404
    assert real_route.status_code == 401
