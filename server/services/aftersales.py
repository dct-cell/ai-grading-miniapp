"""V1/V2 acceptance, the single review, and user-requested refunds.

Every action funnels through :func:`_owned_order`, which loads the order under
a row lock, and then re-checks the order's state *inside* the writing
transaction. That ordering is what makes accept, review and refund mutually
exclusive: a caller cannot decide from a stale snapshot and then write, so two
competing requests for one order always leave exactly one winner and one 409 —
never both an Appeal and a Refund. The unique constraints on
``appeals.order_id`` and ``refunds.external_refund_id`` are the last line of
defence if the lock is unavailable.

The refund amount is never read from the request. A refund is always the full
paid amount, returned to the original payment, so a client cannot influence
how much money leaves the account or where it goes.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from types import MappingProxyType
from typing import Final, Mapping

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from server.db_locking import lock_row
from server.domain.states import (
    JobState,
    OrderState,
    require_order_transition,
)
from server.models import (
    Appeal,
    GradingJob,
    GradingRound,
    Order,
    Payment,
    QuoteSession,
    Refund,
)
from server.services.leases import CANCELLABLE_JOB_STATES, cancel_job
from server.services.payments import PaymentState
from server.services.refunds import (
    RefundSource,
    RefundState,
    build_external_refund_id,
)


MAX_APPEAL_TEXT_LENGTH: Final[int] = 2000


class OrderAction(StrEnum):
    ACCEPT = "accept"
    REVIEW = "review"
    REFUND = "refund"


class RefundReason(StrEnum):
    UPLOADED_WRONG_PDF = "uploaded_wrong_pdf"
    GRADING_DISPUTED = "grading_disputed"
    TOO_SLOW = "too_slow"
    OTHER = "other"


DELIVERED_STATES: Final[frozenset[OrderState]] = frozenset(
    {OrderState.V1_DELIVERED, OrderState.V2_DELIVERED}
)

#: States a user may still walk away from. REFUND_PENDING is excluded: a
#: refund decision is already in flight.
REFUNDABLE_STATES: Final[frozenset[OrderState]] = frozenset(
    {
        OrderState.V1_QUEUED,
        OrderState.V1_RUNNING,
        OrderState.V1_DELIVERED,
        OrderState.V2_QUEUED,
        OrderState.V2_RUNNING,
        OrderState.V2_DELIVERED,
    }
)

ACTIONS_BY_STATE: Final[Mapping[OrderState, tuple[OrderAction, ...]]] = (
    MappingProxyType(
        {
            OrderState.V1_QUEUED: (OrderAction.REFUND,),
            OrderState.V1_RUNNING: (OrderAction.REFUND,),
            OrderState.V1_DELIVERED: (
                OrderAction.ACCEPT,
                OrderAction.REVIEW,
                OrderAction.REFUND,
            ),
            OrderState.V2_QUEUED: (OrderAction.REFUND,),
            OrderState.V2_RUNNING: (OrderAction.REFUND,),
            # There is no third round: V2 offers accept or refund only.
            OrderState.V2_DELIVERED: (OrderAction.ACCEPT, OrderAction.REFUND),
        }
    )
)


class OrderNotAvailable(LookupError):
    """The order does not exist, or is not owned by the caller.

    Both cases raise the same error and map to 404 so the API never confirms
    that somebody else's order id is real.
    """


class ActionNotAllowed(ValueError):
    """The order is not in a state where this action is possible."""


@dataclass(frozen=True)
class ActionOutcome:
    order_id: str
    state: OrderState
    amount_cents: int | None = None
    refund_id: str | None = None
    appeal_id: str | None = None


def _after_state_check(action: str) -> None:
    """Seam for interleaving a competing request in tests.

    Production behaviour is a no-op. Tests monkeypatch this to run a second
    request in the window between the state check and the write, proving the
    exclusivity comes from the locked re-read rather than from luck.
    """


def _owned_order(session: Session, owner_user_id: str, order_id: str) -> Order:
    """Load an order the caller owns, or raise OrderNotAvailable.

    Ownership is derived from the quote's owner_user_id; it is never taken
    from the request.
    """
    owner = session.scalar(
        select(QuoteSession.owner_user_id)
        .join(Order, Order.quote_session_id == QuoteSession.id)
        .where(Order.id == order_id)
    )
    if owner is None or owner != owner_user_id:
        raise OrderNotAvailable(order_id)
    order = lock_row(session, Order, order_id)
    if order is None:
        raise OrderNotAvailable(order_id)
    return order


def _require_order_owner(session: Session, owner_user_id: str, order_id: str) -> None:
    """Verify ownership without taking the Order lock.

    Refunds may also cancel a Worker job.  They lock that Job before the Order,
    matching result delivery and lease repair, so claim/refund cannot deadlock
    by acquiring the same two rows in opposite order.
    """
    owner = session.scalar(
        select(QuoteSession.owner_user_id)
        .join(Order, Order.quote_session_id == QuoteSession.id)
        .where(Order.id == order_id)
    )
    if owner is None or owner != owner_user_id:
        raise OrderNotAvailable(order_id)


def _lock_unfinished_job(session: Session, order_id: str) -> GradingJob | None:
    statement = (
        select(GradingJob)
        .where(
            GradingJob.order_id == order_id,
            GradingJob.state.in_(CANCELLABLE_JOB_STATES),
        )
        .order_by(GradingJob.round_number.desc())
        .limit(1)
    )
    if session.get_bind().dialect.name != "sqlite":
        statement = statement.with_for_update()
    return session.scalars(statement).first()


def available_actions(
    session: Session,
    order: Order,
    *,
    now: datetime | None = None,
) -> tuple[OrderAction, ...]:
    """Report the actions the owner may still take on this order.

    The mini-program renders buttons from this list, but it is advisory only:
    every action re-checks the same conditions inside its own transaction.
    """
    actions = ACTIONS_BY_STATE.get(OrderState(order.state), ())
    if not actions:
        return ()
    moment = now or datetime.now(timezone.utc)
    if _window_closed(order, moment):
        # An expired window still allows accept; the scheduler will do it
        # anyway, so letting the user confirm is not a privilege escalation.
        return tuple(action for action in actions if action is OrderAction.ACCEPT)
    if _has_open_appeal(session, order.id):
        actions = tuple(action for action in actions if action is not OrderAction.REVIEW)
    return actions


def _window_closed(order: Order, moment: datetime) -> bool:
    if order.state not in DELIVERED_STATES:
        return False
    return order.acceptance_deadline is not None and order.acceptance_deadline <= moment


def _has_open_appeal(session: Session, order_id: str) -> bool:
    return (
        session.scalar(select(Appeal.id).where(Appeal.order_id == order_id)) is not None
    )


def _require_action(
    session: Session,
    order: Order,
    action: OrderAction,
    moment: datetime,
) -> None:
    if action not in available_actions(session, order, now=moment):
        raise ActionNotAllowed("当前订单状态不支持该操作。")


def _claim_transition(
    session: Session,
    order: Order,
    target: OrderState,
    **columns: object,
) -> bool:
    """Move the order to``target`` only if it is still in the state we read.

    This is a compare-and-set, not a blind write, and it is what actually makes
    the three aftersales actions mutually exclusive. A plain
    ``order.state = target`` would issue ``UPDATE orders SET state=...
    WHERE id=...``, which happily overwrites a decision another request already
    committed — the classic check-then-write bug. Guarding on the previously
    observed state means the loser updates zero rows and can be turned into a
    409.

    The guard is required on both backends. ``lock_row()`` degrades to no lock on
    SQLite, and pysqlite does not even open a transaction until the first DML,
    so the earlier SELECT provides no isolation there at all.
    """
    observed = OrderState(order.state)
    require_order_transition(observed, target)
    result = session.execute(
        update(Order)
        .where(Order.id == order.id, Order.state == observed)
        .values(state=target, **columns)
    )
    if result.rowcount != 1:
        return False
    # Keep the identity map honest: the row changed underneath the instance.
    session.expire(order)
    return True


_LOST_THE_RACE = "当前订单状态不支持该操作。"


def accept_order(
    *,
    session: Session,
    owner_user_id: str,
    order_id: str,
    now: datetime | None = None,
) -> ActionOutcome:
    """Close a delivered order at the user's request."""
    moment = now or datetime.now(timezone.utc)
    order = _owned_order(session, owner_user_id, order_id)
    _require_action(session, order, OrderAction.ACCEPT, moment)

    if not _claim_transition(session, order, OrderState.ACCEPTED):
        session.rollback()
        raise ActionNotAllowed(_LOST_THE_RACE)
    session.commit()
    return ActionOutcome(order_id=order_id, state=OrderState.ACCEPTED)


def request_review(
    *,
    session: Session,
    owner_user_id: str,
    order_id: str,
    text: str,
    now: datetime | None = None,
) -> ActionOutcome:
    """Buy the single second grading round for a V1-delivered order.

    Round 2 grades the *same* immutable source and reference files against the
    same standard: a review is a re-grade, not a re-submission. The unique
    constraint on appeals.order_id is the final guard against a second review,
    the same way orders.quote_session_id backs payment idempotency.
    """
    moment = now or datetime.now(timezone.utc)
    order = _owned_order(session, owner_user_id, order_id)
    _require_action(session, order, OrderAction.REVIEW, moment)
    if OrderState(order.state) is not OrderState.V1_DELIVERED:
        # V2 has no review: the state machine would allow REFUND_PENDING, but
        # a third grading round must never be reachable from the API.
        raise ActionNotAllowed("该订单已完成复核，无法再次复核。")

    _after_state_check("review")

    round_one = session.scalar(
        select(GradingRound).where(
            GradingRound.order_id == order_id,
            GradingRound.round_number == 1,
        )
    )
    if round_one is None:
        raise ActionNotAllowed(_LOST_THE_RACE)

    # Claim the transition first: if a refund landed in the meantime this
    # updates no rows and no appeal, round or job is ever created.
    if not _claim_transition(
        session,
        order,
        OrderState.V2_QUEUED,
        current_round_number=2,
        acceptance_deadline=None,
    ):
        session.rollback()
        raise ActionNotAllowed(_LOST_THE_RACE)

    appeal = Appeal(order_id=order_id, text=text)
    session.add(appeal)
    session.add(
        GradingRound(
            order_id=order_id,
            round_number=2,
            service_tier=round_one.service_tier,
            grading_standard=round_one.grading_standard,
            league_scope=round_one.league_scope,
            note=round_one.note,
        )
    )
    session.add(
        GradingJob(
            order_id=order_id,
            round_number=2,
            state=JobState.QUEUED,
            queued_at=moment,
            lease_version=0,
            attempt_count=0,
        )
    )

    try:
        session.commit()
    except IntegrityError as error:
        # Lost a race for appeals.order_id or grading_rounds(order_id,
        # round_number): another request already bought the review.
        session.rollback()
        raise ActionNotAllowed("该订单已完成复核，无法再次复核。") from error

    return ActionOutcome(
        order_id=order_id,
        state=OrderState.V2_QUEUED,
        appeal_id=appeal.id,
    )


def request_refund(
    *,
    session: Session,
    owner_user_id: str,
    order_id: str,
    reason: RefundReason,
    now: datetime | None = None,
) -> ActionOutcome:
    """Open a full refund for the caller's own order.

    The amount is the full paid amount from the order row, and the refund is
    bound to the original successful Payment. Nothing about the money comes
    from the request body.
    """
    moment = now or datetime.now(timezone.utc)
    _require_order_owner(session, owner_user_id, order_id)
    job = _lock_unfinished_job(session, order_id)
    order = lock_row(session, Order, order_id)
    if order is None:
        raise OrderNotAvailable(order_id)
    _require_action(session, order, OrderAction.REFUND, moment)
    if OrderState(order.state) not in REFUNDABLE_STATES:
        raise ActionNotAllowed(_LOST_THE_RACE)

    _after_state_check("refund")

    payment = _successful_payment(session, order)
    if payment is None:
        raise ActionNotAllowed("该订单没有可退款的支付记录。")
    amount_cents = order.paid_amount_cents

    # Claim the transition before inserting the Refund: if a review or accept
    # landed in the meantime this updates no rows and no refund is created, so
    # the two decisions can never both exist.
    if not _claim_transition(session, order, OrderState.REFUND_PENDING):
        session.rollback()
        raise ActionNotAllowed(_LOST_THE_RACE)

    if job is not None:
        cancel_job(session, job, event_type="refund_cancelled")

    refund = Refund(
        payment_id=payment.id,
        external_refund_id=build_external_refund_id(order_id),
        source=RefundSource.USER,
        state=RefundState.PENDING,
        amount_cents=amount_cents,
    )
    session.add(refund)

    try:
        session.commit()
    except IntegrityError as error:
        # refunds.external_refund_id is unique, so a concurrent duplicate
        # request loses here rather than creating a second refund for the same
        # money.
        session.rollback()
        raise ActionNotAllowed("该订单已有退款申请。") from error

    return ActionOutcome(
        order_id=order_id,
        state=OrderState.REFUND_PENDING,
        amount_cents=refund.amount_cents,
        refund_id=refund.id,
    )


def _successful_payment(session: Session, order: Order) -> Payment | None:
    return session.scalar(
        select(Payment).where(
            Payment.quote_session_id == order.quote_session_id,
            Payment.state == PaymentState.SUCCEEDED,
        )
    )
