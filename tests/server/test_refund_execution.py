"""Idempotent refund execution, user metrics, and the Admin decision seam.

Money is the one place a retry must never mean "do it again". These tests pin
down three things: a retry reuses one external_refund_id, a gateway failure
leaves the order retryable rather than refunded, and a successful refund
revokes downloads immediately.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from server.adapters.payments import FakePaymentGateway, RefundFailed
from server.domain.states import OrderState
from server.models import AdminUser, AuditLog, Order, Payment, Refund
from server.scheduler.tasks import SchedulerTasks
from server.services.refunds import (
    RefundNotDecidable,
    RefundService,
    RefundSource,
    RefundState,
)
from tests.server.conftest import (
    ADMIN_PASSWORD,
    ADMIN_SHARED_KEY,
    admin_headers,
    admin_login,
    authenticate,
    create_admin,
    deliver_v1_order,
    make_refund_request,
    pay_for_new_order,
)


SHANGHAI = ZoneInfo("Asia/Shanghai")


def count(session: Session, model) -> int:
    return session.scalar(select(func.count()).select_from(model))


@pytest.fixture
def gateway() -> FakePaymentGateway:
    return FakePaymentGateway()


@pytest.fixture
def refund_service(
    session_factory: sessionmaker[Session],
    gateway: FakePaymentGateway,
) -> RefundService:
    return RefundService(session_factory, gateway)


@pytest.fixture
def pending_refund(authenticated_client: TestClient) -> str:
    """A user refund still awaiting execution.

    Uses an amount above the automatic cap so the request path leaves it
    pending: these tests drive execution themselves through their own gateway,
    and an already-settled refund would short-circuit every one of them.
    """
    return make_refund_request(authenticated_client, pages=11)["refund_id"]


def test_retry_uses_the_same_external_refund_id(
    refund_service: RefundService,
    pending_refund: str,
    gateway: FakePaymentGateway,
    session_factory: sessionmaker[Session],
) -> None:
    """A failed attempt must be retried under its original id.

    Minting a fresh id per attempt would defeat the provider's deduplication
    and could refund the same order twice.
    """
    with session_factory() as session:
        external_id = session.get(Refund, pending_refund).external_refund_id

    gateway.fail_once()
    first = refund_service.execute(pending_refund)
    second = refund_service.execute(pending_refund)

    assert first.state is RefundState.REFUND_FAILED
    assert second.state is RefundState.REFUNDED
    assert gateway.external_ids == [external_id, external_id]
    with session_factory() as session:
        refund = session.get(Refund, pending_refund)
    assert refund.state == RefundState.REFUNDED
    # The stored id is the one the provider saw, unchanged by the retry.
    assert refund.external_refund_id == external_id


def test_external_refund_id_is_persisted_not_regenerated(
    refund_service: RefundService,
    pending_refund: str,
    gateway: FakePaymentGateway,
    session_factory: sessionmaker[Session],
) -> None:
    """The id sent to the gateway must come from the row, not a fresh mint.

    Guards the retry contract at its root: if execute() ever derived the id
    itself, two attempts would present different ids and the provider would
    treat the second as an independent refund of the same order.
    """
    gateway.fail_times(3)
    for _ in range(3):
        refund_service.execute(pending_refund)

    with session_factory() as session:
        stored = session.get(Refund, pending_refund).external_refund_id

    assert len(gateway.external_ids) == 3
    assert set(gateway.external_ids) == {stored}


def test_each_refund_gets_a_distinct_external_id(
    authenticated_client: TestClient,
    session_factory: sessionmaker[Session],
) -> None:
    """Distinct refunds must not collide on the provider's deduplication key."""
    first = make_refund_request(authenticated_client, pages=11)
    second = make_refund_request(authenticated_client, pages=11)

    with session_factory() as session:
        ids = {
            session.get(Refund, first["refund_id"]).external_refund_id,
            session.get(Refund, second["refund_id"]).external_refund_id,
        }

    assert len(ids) == 2


def test_execute_is_idempotent_once_refunded(
    refund_service: RefundService,
    pending_refund: str,
    gateway: FakePaymentGateway,
) -> None:
    """Re-running a settled refund must not call the gateway again."""
    refund_service.execute(pending_refund)
    assert len(gateway.calls) == 1

    again = refund_service.execute(pending_refund)

    assert again.state is RefundState.REFUNDED
    assert len(gateway.calls) == 1, "a settled refund must not be re-sent"


def test_failed_gateway_leaves_the_order_refund_pending(
    refund_service: RefundService,
    pending_refund: str,
    gateway: FakePaymentGateway,
    session_factory: sessionmaker[Session],
) -> None:
    """A failure must never look like a completed refund."""
    gateway.fail_once()

    outcome = refund_service.execute(pending_refund)

    assert outcome.state is RefundState.REFUND_FAILED
    with session_factory() as session:
        refund = session.get(Refund, pending_refund)
        payment = session.get(Payment, refund.payment_id)
        order = session.scalar(
            select(Order).where(Order.quote_session_id == payment.quote_session_id)
        )
    assert refund.state == RefundState.REFUND_FAILED
    assert order.state == OrderState.REFUND_PENDING
    assert order.downloads_revoked_at is None


def test_unreachable_gateway_is_retryable(
    refund_service: RefundService,
    pending_refund: str,
    gateway: FakePaymentGateway,
    session_factory: sessionmaker[Session],
) -> None:
    """A transport error must be recorded, not swallowed or fatal."""
    gateway.fail_once(raising=True)

    outcome = refund_service.execute(pending_refund)

    assert outcome.state is RefundState.REFUND_FAILED
    assert refund_service.execute(pending_refund).state is RefundState.REFUNDED
    with session_factory() as session:
        assert session.get(Refund, pending_refund).state == RefundState.REFUNDED


def test_successful_refund_revokes_downloads_immediately(
    refund_service: RefundService,
    pending_refund: str,
    session_factory: sessionmaker[Session],
) -> None:
    outcome = refund_service.execute(pending_refund)

    assert outcome.state is RefundState.REFUNDED
    with session_factory() as session:
        refund = session.get(Refund, pending_refund)
        payment = session.get(Payment, refund.payment_id)
        order = session.scalar(
            select(Order).where(Order.quote_session_id == payment.quote_session_id)
        )
    assert order.state == OrderState.REFUNDED
    assert order.downloads_revoked_at is not None


def test_refund_never_skips_refund_pending(
    refund_service: RefundService,
    pending_refund: str,
    session_factory: sessionmaker[Session],
) -> None:
    """REFUNDED is only reachable from REFUND_PENDING via the state machine."""
    with session_factory() as session:
        refund = session.get(Refund, pending_refund)
        payment = session.get(Payment, refund.payment_id)
        order = session.scalar(
            select(Order).where(Order.quote_session_id == payment.quote_session_id)
        )
        assert order.state == OrderState.REFUND_PENDING

    refund_service.execute(pending_refund)

    with session_factory() as session:
        order = session.scalar(
            select(Order).where(Order.quote_session_id == payment.quote_session_id)
        )
    assert order.state == OrderState.REFUNDED


def test_gateway_receives_the_recorded_amount_and_transaction(
    refund_service: RefundService,
    pending_refund: str,
    gateway: FakePaymentGateway,
    session_factory: sessionmaker[Session],
) -> None:
    """The refund targets the original transaction for the full paid amount."""
    refund_service.execute(pending_refund)

    with session_factory() as session:
        refund = session.get(Refund, pending_refund)
        payment = session.get(Payment, refund.payment_id)

    assert len(gateway.calls) == 1
    call = gateway.calls[0]
    assert call.amount_cents == refund.amount_cents
    assert call.amount_cents == payment.amount_cents
    assert call.external_transaction_id == payment.external_transaction_id


def test_automatic_route_executes_and_manual_route_waits(
    authenticated_client: TestClient,
    session_factory: sessionmaker[Session],
    gateway: FakePaymentGateway,
) -> None:
    """Routing decides whether execution happens now or waits for a human.

    The small refund has already been settled by the request path, so
    re-running route_and_execute is also an idempotency check: it must not
    contact the gateway a second time.
    """
    service = RefundService(session_factory, gateway)

    small = make_refund_request(authenticated_client, pages=2)  # 1000 cents
    settled = service.route_and_execute(small["refund_id"])
    assert settled.state is RefundState.REFUNDED
    assert gateway.calls == [], "an already-settled refund must not be re-sent"

    large = make_refund_request(authenticated_client, pages=11)  # 5500 cents
    waiting = service.route_and_execute(large["refund_id"])
    assert waiting.state is RefundState.PENDING
    assert gateway.calls == [], "a manual refund must not touch the gateway"


def test_user_refund_endpoint_settles_an_automatic_refund(
    authenticated_client: TestClient,
    session_factory: sessionmaker[Session],
) -> None:
    """Routing must happen on the request path, not only in the service.

    A ¥10 refund is within every policy limit, so by the time the user gets a
    response the money is on its way back and downloads are revoked. Leaving it
    ``pending`` would silently require an Admin to approve every refund.
    """
    order_id = deliver_v1_order(authenticated_client, pages=2)["order_id"]

    response = authenticated_client.post(
        f"/api/v1/orders/{order_id}/refund", json={"reason": "grading_disputed"}
    )

    assert response.status_code in {200, 202}, response.text
    assert response.json()["state"] == "refunded"
    with session_factory() as session:
        refund = session.scalar(select(Refund))
        order = session.get(Order, order_id)
    assert refund.state == RefundState.REFUNDED
    assert order.state == OrderState.REFUNDED
    assert order.downloads_revoked_at is not None


def test_user_refund_endpoint_leaves_a_manual_refund_pending(
    authenticated_client: TestClient,
    session_factory: sessionmaker[Session],
) -> None:
    """An oversized refund must wait for an Admin, with downloads intact."""
    order_id = deliver_v1_order(authenticated_client, pages=11)["order_id"]

    response = authenticated_client.post(
        f"/api/v1/orders/{order_id}/refund", json={"reason": "grading_disputed"}
    )

    assert response.status_code in {200, 202}, response.text
    assert response.json()["state"] == "refund_pending"
    with session_factory() as session:
        refund = session.scalar(select(Refund))
        order = session.get(Order, order_id)
    assert refund.state == RefundState.PENDING
    assert order.state == OrderState.REFUND_PENDING
    assert order.downloads_revoked_at is None


def test_technical_refund_does_not_change_user_metrics(
    refund_service: RefundService,
    authenticated_client: TestClient,
    session_factory: sessionmaker[Session],
) -> None:
    """Our own operational failures must not count against the user.

    A technical refund is excluded from the monthly count, the projected
    numerator and the lifetime denominator, so it can never push a later
    legitimate refund into manual review.
    """
    order_id = pay_for_new_order(authenticated_client)
    with session_factory() as session:
        user_id = _owner_of(session, order_id)

    before = refund_service.user_metrics(user_id)
    refund_id = refund_service.create_technical_refund(
        order_id=order_id, admin_id="admin-1", reason="worker_exception"
    )
    refund_service.execute(refund_id)
    after = refund_service.user_metrics(user_id)

    assert after == before
    with session_factory() as session:
        refund = session.get(Refund, refund_id)
    assert refund.source == RefundSource.ADMIN_TECHNICAL


def test_user_metrics_count_only_this_calendar_month_in_shanghai(
    refund_service: RefundService,
    authenticated_client: TestClient,
    session_factory: sessionmaker[Session],
) -> None:
    """The monthly count uses Asia/Shanghai boundaries, not UTC.

    A refund at 07:30 Shanghai on the 1st is still 23:30 UTC on the last day of
    the previous month, so a UTC-based count would place it in the wrong month.
    """
    first = make_refund_request(authenticated_client)
    refund_service.execute(first["refund_id"])
    with session_factory() as session:
        user_id = _owner_of(session, first["order_id"])
        refund = session.get(Refund, first["refund_id"])
        # 1st of this month, 07:30 Shanghai == 23:30 UTC on the previous day.
        now_shanghai = datetime.now(SHANGHAI)
        refund.created_at = datetime(
            now_shanghai.year, now_shanghai.month, 1, 7, 30, tzinfo=SHANGHAI
        ).astimezone(timezone.utc)
        session.add(refund)
        session.commit()

    metrics = refund_service.user_metrics(user_id)

    assert metrics.monthly_user_refund_count == 1


def test_user_metrics_ignore_last_months_refunds(
    refund_service: RefundService,
    authenticated_client: TestClient,
    session_factory: sessionmaker[Session],
) -> None:
    first = make_refund_request(authenticated_client)
    refund_service.execute(first["refund_id"])
    with session_factory() as session:
        user_id = _owner_of(session, first["order_id"])
        refund = session.get(Refund, first["refund_id"])
        now_shanghai = datetime.now(SHANGHAI)
        last_month = datetime(
            now_shanghai.year, now_shanghai.month, 1, 12, 0, tzinfo=SHANGHAI
        ) - timedelta(days=2)
        refund.created_at = last_month.astimezone(timezone.utc)
        session.add(refund)
        session.commit()

    metrics = refund_service.user_metrics(user_id)

    assert metrics.monthly_user_refund_count == 0
    # Lifetime totals are not windowed, so the money still counts.
    assert metrics.lifetime_user_refunded_cents > 0


def _owner_of(session: Session, order_id: str) -> str:
    from server.models import QuoteSession

    order = session.get(Order, order_id)
    return session.get(QuoteSession, order.quote_session_id).owner_user_id


# --- Admin decision endpoints -------------------------------------------------


@pytest.fixture
def admin_id(session_factory: sessionmaker[Session]) -> str:
    return create_admin(session_factory)


@pytest.fixture
def admin_client(client: TestClient, admin_id: str) -> TestClient:
    """A separate client already holding an Admin cookie session.

    Phase 07 retired the shared-key seam, so an admin now authenticates exactly
    as the SPA does: log in, keep the HttpOnly cookie, and send the CSRF token
    on every mutation. A distinct client keeps the mini-program credential on
    ``client`` from mixing into the Admin domain.
    """
    admin = TestClient(client.app)
    csrf = admin_login(admin)
    admin.headers.update(admin_headers(csrf))
    return admin


@pytest.fixture
def manual_refund(authenticated_client: TestClient) -> dict:
    """An oversized refund that policy routes to manual review."""
    return make_refund_request(authenticated_client, pages=11)


def test_admin_approve_executes_the_refund(
    client: TestClient,
    admin_id: str,
    manual_refund: dict,
    session_factory: sessionmaker[Session],
    admin_client: TestClient,
) -> None:
    response = admin_client.post(
        f"/admin/api/v1/refunds/{manual_refund['refund_id']}/approve",
    )

    assert response.status_code == 200, response.text
    assert response.json()["state"] == "refunded"
    with session_factory() as session:
        refund = session.get(Refund, manual_refund["refund_id"])
        order = session.get(Order, manual_refund["order_id"])
    assert refund.state == RefundState.REFUNDED
    assert order.state == OrderState.REFUNDED
    assert order.downloads_revoked_at is not None


def test_admin_reject_returns_the_order_to_accepted_and_keeps_downloads(
    client: TestClient,
    admin_id: str,
    manual_refund: dict,
    session_factory: sessionmaker[Session],
    admin_client: TestClient,
) -> None:
    """A rejected refund preserves access until the normal expiry."""
    response = admin_client.post(
        f"/admin/api/v1/refunds/{manual_refund['refund_id']}/reject",
        json={"reason": "答卷与订单不符"},
    )

    assert response.status_code == 200, response.text
    with session_factory() as session:
        refund = session.get(Refund, manual_refund["refund_id"])
        order = session.get(Order, manual_refund["order_id"])
    assert refund.state == RefundState.REJECTED
    assert order.state == OrderState.ACCEPTED
    assert order.downloads_revoked_at is None


def test_admin_decisions_are_audited_with_the_real_actor(
    client: TestClient,
    admin_id: str,
    manual_refund: dict,
    session_factory: sessionmaker[Session],
    admin_client: TestClient,
) -> None:
    """Refunds spend money, so the audit trail must name a real admin row."""
    admin_client.post(
        f"/admin/api/v1/refunds/{manual_refund['refund_id']}/approve",
    )

    with session_factory() as session:
        entries = session.scalars(select(AuditLog)).all()
        admin = session.get(AdminUser, admin_id)

    assert entries, "an approval must be audited"
    for entry in entries:
        assert entry.actor_type == "admin"
        assert entry.actor_id == admin.id
        assert entry.action == "refund.approve"
        assert entry.target_type == "refund"
        assert entry.target_id == manual_refund["refund_id"]
        # The audit detail must not carry the shared key.
        assert ADMIN_SHARED_KEY not in repr(entry.details)
    # The intent is recorded before the money moves and the result after.
    stages = {entry.details.get("stage") for entry in entries}
    assert stages == {"requested", "settled"}


def test_an_approval_that_fails_still_leaves_an_audit_trail(
    client: TestClient,
    admin_id: str,
    manual_refund: dict,
    session_factory: sessionmaker[Session],
    monkeypatch,
    admin_client: TestClient,
) -> None:
    """A crash mid-refund must not erase who authorised it.

    The money may already have left; an approval with no record of the actor
    would be unauditable.
    """
    from server.services.refunds import RefundService as Service

    def explode(self, refund_id, **kwargs):
        raise RuntimeError("gateway exploded")

    monkeypatch.setattr(Service, "execute", explode)
    with pytest.raises(RuntimeError, match="gateway exploded"):
        admin_client.post(
            f"/admin/api/v1/refunds/{manual_refund['refund_id']}/approve",
        )

    with session_factory() as session:
        entries = session.scalars(select(AuditLog)).all()

    assert len(entries) == 1
    assert entries[0].actor_id == admin_id
    assert entries[0].details["stage"] == "requested"


def test_admin_approve_is_idempotent(
    client: TestClient,
    admin_id: str,
    manual_refund: dict,
    session_factory: sessionmaker[Session],
    admin_client: TestClient,
) -> None:
    """A double-clicked approval must not refund twice."""
    first = admin_client.post(
        f"/admin/api/v1/refunds/{manual_refund['refund_id']}/approve",
    )
    second = admin_client.post(
        f"/admin/api/v1/refunds/{manual_refund['refund_id']}/approve",
    )

    assert first.status_code == 200
    assert second.status_code == 200
    with session_factory() as session:
        assert count(session, Refund) == 1
        assert session.get(Refund, manual_refund["refund_id"]).state == (
            RefundState.REFUNDED
        )


def test_admin_cannot_reject_an_already_refunded_refund(
    client: TestClient,
    admin_id: str,
    manual_refund: dict,
    session_factory: sessionmaker[Session],
    admin_client: TestClient,
) -> None:
    admin_client.post(
        f"/admin/api/v1/refunds/{manual_refund['refund_id']}/approve",
    )

    response = admin_client.post(
        f"/admin/api/v1/refunds/{manual_refund['refund_id']}/reject",
        json={"reason": "改主意了"},
    )

    assert response.status_code == 409
    with session_factory() as session:
        assert session.get(Refund, manual_refund["refund_id"]).state == (
            RefundState.REFUNDED
        )


def test_admin_technical_refund_endpoint_bypasses_user_policy(
    client: TestClient,
    admin_id: str,
    session_factory: sessionmaker[Session],
    admin_client: TestClient,
) -> None:
    """A technical refund is allowed regardless of amount or history."""
    authenticate(client, code="test-technical-refund")
    order_id = pay_for_new_order(client, pages=20)  # 10000 cents, well over cap

    response = admin_client.post(
        "/admin/api/v1/refunds/technical",
        json={"order_id": order_id, "reason": "worker_exception"},
    )

    assert response.status_code == 201, response.text
    assert response.json()["state"] == "refunded"
    with session_factory() as session:
        refund = session.scalar(select(Refund))
        order = session.get(Order, order_id)
    assert refund.source == RefundSource.ADMIN_TECHNICAL
    assert order.state == OrderState.REFUNDED


def test_admin_refund_amount_is_never_client_supplied(
    client: TestClient,
    admin_id: str,
    session_factory: sessionmaker[Session],
    admin_client: TestClient,
) -> None:
    """Even an admin cannot choose the amount or the destination.

    This is the boundary that limits the damage if the shared key leaks: the
    worst an attacker can do is refund a real user's full payment back to the
    card it came from.
    """
    authenticate(client, code="test-admin-amount")
    order_id = pay_for_new_order(client, pages=3)

    response = admin_client.post(
        "/admin/api/v1/refunds/technical",
        json={
            "order_id": order_id,
            "reason": "worker_exception",
            "amount_cents": 999_999,
            "external_transaction_id": "attacker-account",
        },
    )

    assert response.status_code in {201, 422}
    with session_factory() as session:
        order = session.get(Order, order_id)
        refund = session.scalar(select(Refund))
        payment = session.scalar(
            select(Payment).where(Payment.quote_session_id == order.quote_session_id)
        )
    if refund is not None:
        assert refund.amount_cents == order.paid_amount_cents
        assert refund.amount_cents != 999_999
        assert payment.external_transaction_id != "attacker-account"


def test_one_payment_is_never_refunded_twice(
    authenticated_client: TestClient,
    client: TestClient,
    admin_id: str,
    session_factory: sessionmaker[Session],
    admin_client: TestClient,
) -> None:
    """A failed user refund must not open the door to a second real refund.

    The sequence is entirely realistic: a user refund fails at the gateway, an
    Admin issues a technical refund for the same order, and the scheduler later
    retries the failed one. If the technical refund is allowed to exist
    alongside the failed one, both settle under *different* external ids, the
    provider cannot deduplicate them, and the user is paid twice.
    """
    refund = make_refund_request(authenticated_client, pages=11)
    gateway = FakePaymentGateway()
    gateway.fail_once(raising=True)
    RefundService(session_factory, gateway).execute(refund["refund_id"])
    with session_factory() as session:
        assert session.get(Refund, refund["refund_id"]).state == (
            RefundState.REFUND_FAILED
        )

    admin_client.post(
        "/admin/api/v1/refunds/technical",
        json={"order_id": refund["order_id"], "reason": "worker_exception"},
    )
    tasks_gateway = FakePaymentGateway()
    SchedulerTasks(
        session_factory,
        settings=client.app.state.settings,
        gateway=tasks_gateway,
    ).retry_failed_refund_queries()

    with session_factory() as session:
        order = session.get(Order, refund["order_id"])
        refunds = session.scalars(select(Refund)).all()
        settled = [row for row in refunds if row.state == RefundState.REFUNDED]

    total = sum(row.amount_cents for row in settled)
    assert total <= order.paid_amount_cents, (
        f"refunded {total} cents for an order worth {order.paid_amount_cents}"
    )


def test_a_technical_refund_reuses_a_failed_user_refund(
    authenticated_client: TestClient,
    session_factory: sessionmaker[Session],
    gateway: FakePaymentGateway,
) -> None:
    """A retryable refund already exists, so no second row may be created.

    Reusing it keeps one external_refund_id per payment, which is what lets the
    provider reject a duplicate.
    """
    refund = make_refund_request(authenticated_client, pages=11)
    service = RefundService(session_factory, gateway)
    gateway.fail_once()
    service.execute(refund["refund_id"])

    reused = service.create_technical_refund(
        order_id=refund["order_id"], admin_id="admin-1", reason="worker_exception"
    )

    assert reused == refund["refund_id"]
    with session_factory() as session:
        assert count(session, Refund) == 1


def test_execute_refuses_a_refund_for_an_already_refunded_order(
    authenticated_client: TestClient,
    session_factory: sessionmaker[Session],
    gateway: FakePaymentGateway,
) -> None:
    """The order is the source of truth for "has this money gone back".

    Even if a stale refund row is still executable, the order having reached
    REFUNDED means the money already left; executing again would double-pay.
    """
    refund = make_refund_request(authenticated_client, pages=11)
    service = RefundService(session_factory, gateway)
    gateway.fail_once()
    service.execute(refund["refund_id"])

    # Force a second, independent refund row onto the same payment, as the
    # technical-refund path used to do.
    with session_factory() as session:
        original = session.get(Refund, refund["refund_id"])
        session.add(
            Refund(
                payment_id=original.payment_id,
                external_refund_id="rf-duplicate-row",
                source=RefundSource.ADMIN_TECHNICAL,
                state=RefundState.PENDING,
                amount_cents=original.amount_cents,
            )
        )
        session.commit()
        duplicate_id = session.scalar(
            select(Refund.id).where(Refund.external_refund_id == "rf-duplicate-row")
        )

    service.execute(refund["refund_id"])  # settles the original
    calls_before = len(gateway.calls)

    with pytest.raises(RefundNotDecidable):
        service.execute(duplicate_id)

    assert len(gateway.calls) == calls_before, "the duplicate must not be sent"
    with session_factory() as session:
        order = session.get(Order, refund["order_id"])
        settled = [
            row.amount_cents
            for row in session.scalars(select(Refund)).all()
            if row.state == RefundState.REFUNDED
        ]
    assert sum(settled) == order.paid_amount_cents


def test_technical_refund_on_a_refunded_order_is_a_conflict_not_a_crash(
    client: TestClient,
    authenticated_client: TestClient,
    admin_id: str,
    session_factory: sessionmaker[Session],
    admin_client: TestClient,
) -> None:
    """Operating on a finished order is routine; it must not surface as a 500."""
    order_id = deliver_v1_order(authenticated_client, pages=2)["order_id"]
    authenticated_client.post(f"/api/v1/orders/{order_id}/accept")

    response = admin_client.post(
        "/admin/api/v1/refunds/technical",
        json={"order_id": order_id, "reason": "worker_exception"},
    )

    assert response.status_code == 409, response.text


def test_admin_amount_override_is_rejected_outright(
    client: TestClient,
    admin_id: str,
    authenticated_client: TestClient,
    session_factory: sessionmaker[Session],
    admin_client: TestClient,
) -> None:
    """An unexpected amount field is refused, not silently ignored."""
    order_id = pay_for_new_order(authenticated_client, pages=3)

    response = admin_client.post(
        "/admin/api/v1/refunds/technical",
        json={
            "order_id": order_id,
            "reason": "worker_exception",
            "amount_cents": 999_999,
        },
    )

    assert response.status_code == 422, response.text
    with session_factory() as session:
        assert count(session, Refund) == 0


def test_a_rejected_refund_does_not_consume_the_users_quota(
    client: TestClient,
    authenticated_client: TestClient,
    admin_id: str,
    refund_service: RefundService,
    session_factory: sessionmaker[Session],
    admin_client: TestClient,
) -> None:
    """A request we declined must not count against the user.

    The monthly quota exists to catch users refunding habitually. A refund an
    Admin refused is not a refund: counting it would penalise the user for
    asking and push their next legitimate request into manual review.
    """
    refund = make_refund_request(authenticated_client, pages=11)
    with session_factory() as session:
        user_id = _owner_of(session, refund["order_id"])
    # While pending, the live request legitimately occupies a quota slot.
    assert refund_service.user_metrics(user_id).monthly_user_refund_count == 1

    rejected = admin_client.post(
        f"/admin/api/v1/refunds/{refund['refund_id']}/reject",
        json={"reason": "答卷与订单不符"},
    )
    assert rejected.status_code == 200, rejected.text

    after = refund_service.user_metrics(user_id)
    assert after.monthly_user_refund_count == 0
    assert after.lifetime_user_refunded_cents == 0


def test_a_failed_refund_attempt_does_not_consume_the_users_quota(
    authenticated_client: TestClient,
    refund_service: RefundService,
    gateway: FakePaymentGateway,
    session_factory: sessionmaker[Session],
) -> None:
    """A gateway failure is our problem, not evidence of user behaviour.

    The refund stays retryable, so it will count again once it settles; what it
    must not do is burn a quota slot while it is stuck.
    """
    refund = make_refund_request(authenticated_client, pages=11)
    with session_factory() as session:
        user_id = _owner_of(session, refund["order_id"])

    gateway.fail_once()
    refund_service.execute(refund["refund_id"])

    assert refund_service.user_metrics(user_id).monthly_user_refund_count == 0
    # Once it succeeds it counts again.
    refund_service.execute(refund["refund_id"])
    assert refund_service.user_metrics(user_id).monthly_user_refund_count == 1


def test_a_live_refund_request_consumes_the_users_quota(
    authenticated_client: TestClient,
    refund_service: RefundService,
    session_factory: sessionmaker[Session],
) -> None:
    """The counter must still work: a live request counts, and settling keeps it.

    Guards the two exclusions above from over-reaching into "nothing counts".
    A pending request is expected to settle, so it consumes quota immediately;
    executing it moves money but must not double-count.
    """
    refund = make_refund_request(authenticated_client, pages=11)
    with session_factory() as session:
        user_id = _owner_of(session, refund["order_id"])
    pending = refund_service.user_metrics(user_id)

    refund_service.execute(refund["refund_id"])
    settled = refund_service.user_metrics(user_id)

    assert pending.monthly_user_refund_count == 1
    assert settled.monthly_user_refund_count == 1
    assert pending.lifetime_user_refunded_cents == 0
    assert settled.lifetime_user_refunded_cents > 0


# --- Admin authentication -----------------------------------------------------


def test_admin_endpoints_reject_every_wrong_credential(
    client: TestClient,
    admin_id: str,
    manual_refund: dict,
    session_factory: sessionmaker[Session],
) -> None:
    """Admin is its own authentication domain, separate from the other two."""
    from tests.server.conftest import SHARED_KEY

    path = f"/admin/api/v1/refunds/{manual_refund['refund_id']}/approve"
    miniapp_token = client.headers.get("Authorization")

    cases = {
        "no credential": {},
        "bearer token of any kind": {
            "Authorization": "Bearer " + "z" * 40,
            "X-Admin-ID": admin_id,
        },
        "worker shared key": {
            "Authorization": f"Bearer {SHARED_KEY}",
            "X-Worker-ID": "worker-1",
        },
        "miniapp session token": {
            "Authorization": miniapp_token or "Bearer none",
        },
        "the retired admin shared key": {
            "Authorization": f"Bearer {ADMIN_SHARED_KEY}",
            "X-Admin-ID": admin_id,
        },
    }

    unauthenticated = TestClient(client.app)
    for label, headers in cases.items():
        response = unauthenticated.post(path, headers=headers)
        assert response.status_code == 401, (label, response.status_code)

    # A forged cookie value must fail too: the stored value is a hash, so a
    # guessed token cannot resolve to a session.
    forged = TestClient(client.app)
    forged.cookies.set("grader_admin_session", "forged", path="/admin")
    assert forged.post(path).status_code == 401

    with session_factory() as session:
        assert session.get(Refund, manual_refund["refund_id"]).state == (
            RefundState.PENDING
        )


def test_a_disabled_admin_cannot_approve(
    client: TestClient,
    manual_refund: dict,
    session_factory: sessionmaker[Session],
) -> None:
    """Disabling an account stops it logging in at all."""
    create_admin(
        session_factory,
        username="disabled-admin",
        disabled_at=datetime.now(timezone.utc),
    )
    probe = TestClient(client.app)

    login = probe.post(
        "/admin/api/v1/auth/login",
        json={"username": "disabled-admin", "password": ADMIN_PASSWORD},
    )

    assert login.status_code == 403
    response = probe.post(
        f"/admin/api/v1/refunds/{manual_refund['refund_id']}/approve"
    )
    assert response.status_code == 401
    with session_factory() as session:
        assert session.get(Refund, manual_refund["refund_id"]).state == (
            RefundState.PENDING
        )


def test_disabling_an_admin_mid_session_stops_further_approvals(
    client: TestClient,
    admin_client: TestClient,
    admin_id: str,
    manual_refund: dict,
    session_factory: sessionmaker[Session],
) -> None:
    """An opaque session means revocation takes effect on the next request."""
    with session_factory() as session:
        session.get(AdminUser, admin_id).disabled_at = datetime.now(timezone.utc)
        session.commit()

    response = admin_client.post(
        f"/admin/api/v1/refunds/{manual_refund['refund_id']}/approve"
    )

    assert response.status_code == 401
    with session_factory() as session:
        assert session.get(Refund, manual_refund["refund_id"]).state == (
            RefundState.PENDING
        )


def test_an_admin_cookie_cannot_authenticate_as_a_miniapp_user_or_worker(
    admin_client: TestClient,
) -> None:
    """The reverse direction: an admin credential is useless elsewhere.

    The cookie is scoped to ``/admin``, so it is not even sent to these paths —
    and the CSRF header it carries means nothing to them either.
    """
    assert admin_client.get("/api/v1/me").status_code == 401
    assert admin_client.post("/worker/v1/jobs/lease").status_code == 401


def test_the_retired_shared_key_cannot_approve_a_refund(
    client: TestClient,
    admin_id: str,
    manual_refund: dict,
    session_factory: sessionmaker[Session],
) -> None:
    """Phase 07 removed the shared-key path, so the key now buys nothing."""
    response = TestClient(client.app).post(
        f"/admin/api/v1/refunds/{manual_refund['refund_id']}/approve",
        headers={
            "Authorization": f"Bearer {ADMIN_SHARED_KEY}",
            "X-Admin-ID": admin_id,
        },
    )

    assert response.status_code == 401
    assert ADMIN_SHARED_KEY not in response.text
    with session_factory() as session:
        assert session.get(Refund, manual_refund["refund_id"]).state == (
            RefundState.PENDING
        )


def test_admin_routes_are_registered_in_production(tmp_path) -> None:
    """Admin is a real endpoint, not a fake adapter behind the env gate.

    Refund approvals have to work in production, so unlike fake login and fake
    payment these routes are registered in every environment — the same
    reasoning as /worker/v1/*.
    """
    from server.config import Environment
    from server.main import create_app
    from tests.server.conftest import build_settings
    from tests.server.test_auth_environment_gate import route_paths

    settings = build_settings(
        tmp_path,
        environment=Environment.PRODUCTION,
        database_url="mysql+pymysql://grader:secret@127.0.0.1/grader",
    )
    paths = route_paths(create_app(settings))

    assert "/admin/api/v1/refunds/{refund_id}/approve" in paths
    assert "/admin/api/v1/refunds/{refund_id}/reject" in paths
    assert "/admin/api/v1/refunds/technical" in paths
    # The fake adapters remain gated.
    assert "/api/v1/auth/login" not in paths
    assert "/callbacks/fake/pay" not in paths


def test_admin_routes_appear_in_the_production_openapi(tmp_path) -> None:
    """A real endpoint must be documented; a gated fake one must not be."""
    from server.config import Environment
    from server.main import create_app
    from tests.server.conftest import build_settings

    settings = build_settings(
        tmp_path,
        environment=Environment.PRODUCTION,
        database_url="mysql+pymysql://grader:secret@127.0.0.1/grader",
    )
    with TestClient(create_app(settings)) as probe:
        documented = probe.get("/openapi.json").json()["paths"]

    assert "/admin/api/v1/refunds/{refund_id}/approve" in documented
    assert "/api/v1/auth/login" not in documented


def test_refund_errors_do_not_leak_amounts_or_ids(
    client: TestClient,
    admin_id: str,
    manual_refund: dict,
    admin_client: TestClient,
) -> None:
    """Failure messages must not disclose refund internals."""
    admin_client.post(
        f"/admin/api/v1/refunds/{manual_refund['refund_id']}/approve",
    )
    conflict = admin_client.post(
        f"/admin/api/v1/refunds/{manual_refund['refund_id']}/reject",
        json={"reason": "太晚了"},
    )

    assert conflict.status_code == 409
    body = conflict.text
    assert "5500" not in body
    assert "rf-" not in body
