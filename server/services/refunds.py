"""Refund records and their idempotent execution.

Money is the one place where a retry must never mean "do it again". The design
has three parts:

1. The Refund row is persisted *before* the gateway is called, carrying the
   ``external_refund_id`` the gateway will see. A retry re-reads that row and
   reuses the same id, so the payment provider can deduplicate. Minting a fresh
   id per attempt would defeat that and could move the money twice.
2. The gateway is called **outside** any open transaction. An external call
   inside a transaction would hold row locks across network latency and, worse,
   a rollback would leave us unable to tell a failed write from a refund that
   actually went through.
3. Only a successful gateway result advances the order to REFUNDED and revokes
   downloads. A failure marks the refund ``refund_failed`` and leaves the order
   in REFUND_PENDING, which is a retryable state. The order is never marked
   refunded on the strength of a request we did not see succeed.

User-requested refunds are routed by :mod:`server.domain.refund_policy`.
Technical refunds bypass that policy entirely and are excluded from every user
metric: our own operational failures must not push a user towards manual
review.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from secrets import token_hex
from typing import Final
from zoneinfo import ZoneInfo

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session, sessionmaker

from server.adapters.payments import (
    PaymentGateway,
    RefundFailed,
    RefundRequest,
)
from server.domain.refund_policy import (
    RefundFacts,
    RefundRoute,
    decide_refund_route,
)
from server.domain.states import (
    ORDER_TRANSITIONS,
    OrderState,
    require_order_transition,
)
from server.models import Order, Payment, QuoteSession, Refund


SHANGHAI = ZoneInfo("Asia/Shanghai")


class RefundSource(StrEnum):
    """Matches the ck_refunds_source check constraint."""

    USER = "user"
    ADMIN_TECHNICAL = "admin_technical"


class RefundState(StrEnum):
    PENDING = "pending"
    REFUNDED = "refunded"
    REFUND_FAILED = "refund_failed"
    REJECTED = "rejected"


#: States from which execute() may still call the gateway.
EXECUTABLE_REFUND_STATES: Final[frozenset[str]] = frozenset(
    {RefundState.PENDING, RefundState.REFUND_FAILED}
)

#: Refund states that count towards a user's monthly quota. A refund an Admin
#: declined, or one that failed at the gateway, is not evidence of user
#: behaviour — counting either would penalise the user for asking, or for our
#: own outage, and push their next legitimate request into manual review.
#: PENDING counts because the request is live and expected to settle.
QUOTA_REFUND_STATES: Final[frozenset[str]] = frozenset(
    {RefundState.PENDING, RefundState.REFUNDED}
)


class RefundNotFound(LookupError):
    """No refund matches the supplied identifier."""


class RefundNotDecidable(ValueError):
    """The refund has already been settled one way or the other."""


def build_external_refund_id(order_id: str) -> str:
    """Mint the id the payment provider will deduplicate on.

    Generated once, when the Refund row is created, and never regenerated:
    every retry of the same refund must present the same id.
    """
    return f"rf-{order_id[:8]}-{token_hex(8)}"


@dataclass(frozen=True)
class RefundOutcome:
    refund_id: str
    state: RefundState
    order_id: str
    order_state: OrderState
    route: RefundRoute | None = None


@dataclass(frozen=True)
class UserRefundMetrics:
    """Only user-requested refunds contribute to these numbers."""

    monthly_user_refund_count: int
    lifetime_paid_cents: int
    lifetime_user_refunded_cents: int


def _lock(session: Session, model, primary_key: str):
    """Take a row lock where the backend supports one.

    Mirrors server.services.payments._lock. SQLite ignores FOR UPDATE and
    serialises writers; MySQL issues a real SELECT ... FOR UPDATE.
    """
    if session.get_bind().dialect.name == "sqlite":
        return session.get(model, primary_key)
    return session.get(model, primary_key, with_for_update=True)


def _month_start_utc(moment: datetime) -> datetime:
    """First instant of ``moment``'s Asia/Shanghai calendar month, in UTC.

    The business rule is expressed in local calendar months, so the boundary is
    computed in Shanghai and then converted; counting in UTC would misfile
    refunds made in the early hours of the 1st.
    """
    local = moment.astimezone(SHANGHAI)
    start_local = local.replace(
        day=1, hour=0, minute=0, second=0, microsecond=0
    )
    return start_local.astimezone(timezone.utc)


class RefundService:
    """Owns every write to a refund row and every gateway refund call."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        gateway: PaymentGateway,
    ) -> None:
        self._session_factory = session_factory
        self._gateway = gateway

    # -- reads ---------------------------------------------------------------

    def get(self, refund_id: str) -> Refund | None:
        with self._session_factory() as session:
            return session.get(Refund, refund_id)

    def user_metrics(
        self,
        user_id: str,
        *,
        now: datetime | None = None,
    ) -> UserRefundMetrics:
        """Summarise a user's own refund history.

        Technical refunds are excluded from all three figures.
        """
        moment = now or datetime.now(timezone.utc)
        month_start = _month_start_utc(moment)
        with self._session_factory() as session:
            monthly = session.scalar(
                select(func.count())
                .select_from(Refund)
                .join(Payment, Payment.id == Refund.payment_id)
                .join(QuoteSession, QuoteSession.id == Payment.quote_session_id)
                .where(
                    QuoteSession.owner_user_id == user_id,
                    Refund.source == RefundSource.USER,
                    Refund.state.in_(QUOTA_REFUND_STATES),
                    Refund.created_at >= month_start,
                )
            )
            paid = session.scalar(
                select(func.coalesce(func.sum(Payment.amount_cents), 0))
                .join(QuoteSession, QuoteSession.id == Payment.quote_session_id)
                .where(
                    QuoteSession.owner_user_id == user_id,
                    Payment.state == "succeeded",
                )
            )
            refunded = session.scalar(
                select(func.coalesce(func.sum(Refund.amount_cents), 0))
                .select_from(Refund)
                .join(Payment, Payment.id == Refund.payment_id)
                .join(QuoteSession, QuoteSession.id == Payment.quote_session_id)
                .where(
                    QuoteSession.owner_user_id == user_id,
                    Refund.source == RefundSource.USER,
                    Refund.state == RefundState.REFUNDED,
                )
            )
        return UserRefundMetrics(
            monthly_user_refund_count=int(monthly or 0),
            lifetime_paid_cents=int(paid or 0),
            lifetime_user_refunded_cents=int(refunded or 0),
        )

    # -- routing -------------------------------------------------------------

    def decide_route(
        self,
        refund_id: str,
        *,
        now: datetime | None = None,
    ) -> RefundRoute:
        """Classify a pending user refund as automatic or manual.

        A technical refund never consults the policy.
        """
        with self._session_factory() as session:
            refund = session.get(Refund, refund_id)
            if refund is None:
                raise RefundNotFound(refund_id)
            if refund.source == RefundSource.ADMIN_TECHNICAL:
                return RefundRoute.AUTOMATIC
            owner_user_id = self._owner_of(session, refund)
            amount_cents = refund.amount_cents

        metrics = self.user_metrics(owner_user_id, now=now)
        return decide_refund_route(
            RefundFacts(
                order_amount_cents=amount_cents,
                monthly_user_refund_count=metrics.monthly_user_refund_count,
                lifetime_paid_cents=metrics.lifetime_paid_cents,
                lifetime_user_refunded_cents=metrics.lifetime_user_refunded_cents,
            )
        )

    def route_and_execute(
        self,
        refund_id: str,
        *,
        now: datetime | None = None,
    ) -> RefundOutcome:
        """Execute immediately when policy allows, else leave it for an Admin."""
        route = self.decide_route(refund_id, now=now)
        if route is RefundRoute.MANUAL:
            return self._describe(refund_id, route=route)
        outcome = self.execute(refund_id, now=now)
        return RefundOutcome(
            refund_id=outcome.refund_id,
            state=outcome.state,
            order_id=outcome.order_id,
            order_state=outcome.order_state,
            route=route,
        )

    # -- writes --------------------------------------------------------------

    def execute(
        self,
        refund_id: str,
        *,
        reason: str = "user_requested",
        now: datetime | None = None,
    ) -> RefundOutcome:
        """Send one refund to the gateway, at most once per settled refund.

        Safe to call repeatedly: an already-refunded row short-circuits without
        touching the gateway, and a previously failed row is retried under its
        original external_refund_id.
        """
        moment = now or datetime.now(timezone.utc)

        #1. Read what we need, then leave the transaction before any I/O.
        with self._session_factory() as session:
            refund = session.get(Refund, refund_id)
            if refund is None:
                raise RefundNotFound(refund_id)
            if refund.state == RefundState.REFUNDED:
                return self._describe_locked(session, refund)
            if refund.state not in EXECUTABLE_REFUND_STATES:
                raise RefundNotDecidable(refund_id)
            order = self._order_of(session, refund)
            if OrderState(order.state) is OrderState.REFUNDED:
                # The order is the authority on whether this money has already
                # gone back. A second refund row against a settled order — a
                # stale technical refund, a duplicate created by an older
                # version — would pay the user twice under a different
                # external_refund_id, which the provider cannot deduplicate.
                raise RefundNotDecidable(refund_id)
            payment = session.get(Payment, refund.payment_id)
            request = RefundRequest(
                external_refund_id=refund.external_refund_id,
                external_transaction_id=payment.external_transaction_id or "",
                amount_cents=refund.amount_cents,
                reason=reason,
            )

        # 2. Call the provider with no transaction open and no locks held.
        try:
            result = self._gateway.refund(request)
            succeeded = result.succeeded
        except RefundFailed:
            succeeded = False

        # 3. Record the outcome. Only success is allowed to end the order.
        with self._session_factory() as session:
            refund = session.get(Refund, refund_id)
            if refund is None:
                raise RefundNotFound(refund_id)
            if refund.state == RefundState.REFUNDED:
                # Another attempt settled it while the gateway call was in
                # flight; do not transition the order twice.
                return self._describe_locked(session, refund)
            if not succeeded:
                refund.state = RefundState.REFUND_FAILED
                session.add(refund)
                session.commit()
                return self._describe_locked(session, refund)

            refund.state = RefundState.REFUNDED
            session.add(refund)
            order = self._order_of(session, refund)
            if OrderState(order.state) is not OrderState.REFUNDED:
                require_order_transition(
                    OrderState(order.state), OrderState.REFUNDED
                )
                # Compare-and-set so a concurrent settlement cannot be
                # overwritten, and revoke downloads in the same statement:
                # access must end the moment the money goes back.
                updated = session.execute(
                    update(Order)
                    .where(Order.id == order.id, Order.state == order.state)
                    .values(
                        state=OrderState.REFUNDED,
                        downloads_revoked_at=moment,
                    )
                )
                if updated.rowcount != 1:
                    session.rollback()
                    return self._describe(refund_id)
                session.expire(order)
            session.commit()
            return self._describe_locked(session, refund)

    def create_technical_refund(
        self,
        *,
        order_id: str,
        admin_id: str,
        reason: str = "technical",
    ) -> str:
        """Open a full refund that bypasses the user policy.

        Used when we failed the user (a Worker exception, a bad delivery). It is
        recorded with source ``admin_technical`` so it never counts against the
        user's monthly quota or refund ratio.
        """
        with self._session_factory() as session:
            # Lock the order for the whole decision, the same way the payment
            # and lease services do. The compare-and-set below is what
            # guarantees correctness, but taking the lock first means a
            # competing user action waits rather than racing and losing.
            order = _lock(session, Order, order_id)
            if order is None:
                raise RefundNotFound(order_id)
            payment = session.scalar(
                select(Payment).where(
                    Payment.quote_session_id == order.quote_session_id,
                    Payment.state == "succeeded",
                )
            )
            if payment is None:
                raise RefundNotDecidable(order_id)

            # Never open a second refund against a payment that already has a
            # live one. Crucially this includes REFUND_FAILED: that refund is
            # retryable and still owns the payment's external_refund_id, so
            # adding another row would let both settle under different ids and
            # pay the user twice.
            existing = session.scalar(
                select(Refund).where(
                    Refund.payment_id == payment.id,
                    Refund.state.in_(
                        (
                            RefundState.PENDING,
                            RefundState.REFUND_FAILED,
                            RefundState.REFUNDED,
                        )
                    ),
                )
            )
            if existing is not None:
                return existing.id

            if OrderState(order.state) is not OrderState.REFUND_PENDING:
                observed = OrderState(order.state)
                if OrderState.REFUND_PENDING not in ORDER_TRANSITIONS.get(
                    observed, frozenset()
                ):
                    # Accepted, already refunded, or otherwise terminal. This is
                    # a routine operator action on a finished order, so it must
                    # surface as a conflict rather than an unhandled ValueError.
                    raise RefundNotDecidable(order_id)
                require_order_transition(observed, OrderState.REFUND_PENDING)
                # Compare-and-set, like every other order write: a user
                # accepting between the read above and this write must win.
                claimed = session.execute(
                    update(Order)
                    .where(Order.id == order.id, Order.state == observed)
                    .values(state=OrderState.REFUND_PENDING)
                )
                if claimed.rowcount != 1:
                    session.rollback()
                    raise RefundNotDecidable(order_id)

            refund = Refund(
                payment_id=payment.id,
                external_refund_id=build_external_refund_id(order_id),
                source=RefundSource.ADMIN_TECHNICAL,
                state=RefundState.PENDING,
                amount_cents=order.paid_amount_cents,
            )
            session.add(refund)
            session.commit()
            return refund.id

    def reject(
        self,
        refund_id: str,
        *,
        admin_id: str,
        reason: str,
    ) -> RefundOutcome:
        """Decline a manual refund and return the order toACCEPTED.

        Downloads are deliberately left intact: the user keeps the result they
        paid for until the normal expiry.
        """
        with self._session_factory() as session:
            refund = session.get(Refund, refund_id)
            if refund is None:
                raise RefundNotFound(refund_id)
            if refund.state != RefundState.PENDING:
                raise RefundNotDecidable(refund_id)

            order = self._order_of(session, refund)
            require_order_transition(OrderState(order.state), OrderState.ACCEPTED)
            updated = session.execute(
                update(Order)
                .where(Order.id == order.id, Order.state == order.state)
                .values(state=OrderState.ACCEPTED)
            )
            if updated.rowcount != 1:
                session.rollback()
                raise RefundNotDecidable(refund_id)

            refund.state = RefundState.REJECTED
            session.add(refund)
            session.commit()
            session.expire(order)
            return self._describe_locked(session, refund)

    # -- helpers -------------------------------------------------------------

    @staticmethod
    def _order_of(session: Session, refund: Refund) -> Order:
        payment = session.get(Payment, refund.payment_id)
        return session.scalar(
            select(Order).where(Order.quote_session_id == payment.quote_session_id)
        )

    @staticmethod
    def _owner_of(session: Session, refund: Refund) -> str:
        payment = session.get(Payment, refund.payment_id)
        quote = session.get(QuoteSession, payment.quote_session_id)
        return quote.owner_user_id

    def _describe(
        self,
        refund_id: str,
        *,
        route: RefundRoute | None = None,
    ) -> RefundOutcome:
        with self._session_factory() as session:
            refund = session.get(Refund, refund_id)
            if refund is None:
                raise RefundNotFound(refund_id)
            return self._describe_locked(session, refund, route=route)

    def _describe_locked(
        self,
        session: Session,
        refund: Refund,
        *,
        route: RefundRoute | None = None,
    ) -> RefundOutcome:
        order = self._order_of(session, refund)
        return RefundOutcome(
            refund_id=refund.id,
            state=RefundState(refund.state),
            order_id=order.id,
            order_state=OrderState(order.state),
            route=route,
        )
