"""Admin reads over orders: the overview snapshot and cross-user search.

Two things separate this from ``services/orders.py``:

*No owner filter.* This is a management plane, so an admin searches every user's
orders. The mini-program's ownership predicate must not be copied across —
doing so would silently return nothing useful — but neither may the responses
built from these reads disclose storage layout. ``FileObject.relative_path`` is
never selected here.

*One consistent snapshot.* The overview counts are gathered inside a single
transaction so the numbers cannot contradict each other, e.g. reporting a queued
job whose order the same response says does not exist.
"""
from __future__ import annotations

import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import Select, func, or_, select
from sqlalchemy.orm import Session

from server.domain.states import JobState, OrderState
from server.models import (
    GradingJob,
    GradingRound,
    Order,
    Payment,
    QuoteSession,
    Refund,
    User,
    Worker,
)
from server.services.orders import (
    InvalidCursor,
    decode_cursor,
    encode_cursor,
)
from server.services.refunds import RefundSource, RefundState
from server.services.workers import ALL_WORKER_STATUSES


MAX_PAGE_SIZE = 100
DEFAULT_PAGE_SIZE = 25

#: Manual review queue: a user refund still waiting for an admin decision.
PENDING_MANUAL_STATES = (RefundState.PENDING,)


@dataclass(frozen=True)
class OverviewSnapshot:
    orders: dict[str, int]
    jobs: dict[str, int]
    workers: dict[str, int]
    refunds: dict[str, int]
    storage: dict[str, object]


@dataclass(frozen=True)
class AdminOrderRow:
    order: Order
    quote: QuoteSession
    owner_public_id: str


@dataclass(frozen=True)
class AdminOrderPage:
    items: tuple[AdminOrderRow, ...]
    next_cursor: str | None


@dataclass(frozen=True)
class TimelineEvent:
    event: str
    at: datetime


@dataclass(frozen=True)
class AdminOrderDetail:
    order: Order
    quote: QuoteSession
    owner_public_id: str
    payment: Payment | None
    refunds: tuple[Refund, ...]
    rounds: tuple[tuple[GradingRound, GradingJob | None], ...]
    files: tuple[tuple[str, int], ...]
    timeline: tuple[TimelineEvent, ...]


def collect_overview(session: Session, *, data_dir: Path) -> OverviewSnapshot:
    """Count everything the overview shows from one read of the database.

    A single session and no intervening commit means every count observes the
    same state; issuing these as separate requests would let an order move
    between two of them and produce a self-contradicting dashboard.
    """
    order_counts = dict(
        session.execute(
            select(Order.state, func.count()).group_by(Order.state)
        ).all()
    )
    job_counts = dict(
        session.execute(
            select(GradingJob.state, func.count()).group_by(GradingJob.state)
        ).all()
    )
    worker_counts = dict(
        session.execute(
            select(Worker.status, func.count()).group_by(Worker.status)
        ).all()
    )

    pending_manual = session.scalar(
        select(func.count())
        .select_from(Refund)
        .where(
            Refund.state.in_(PENDING_MANUAL_STATES),
            Refund.source == RefundSource.USER,
        )
    )
    failed_refunds = session.scalar(
        select(func.count())
        .select_from(Refund)
        .where(Refund.state == RefundState.REFUND_FAILED)
    )

    return OverviewSnapshot(
        orders={state.value: int(order_counts.get(state.value, 0)) for state in OrderState},
        jobs={state.value: int(job_counts.get(state.value, 0)) for state in JobState},
        workers={
            # Enumerated from WorkerStatus rather than listed by hand: a status
            # this dict does not name makes those Workers vanish from the panel
            # entirely, which an operator reads as lost capacity. Phase 07's
            # `draining` was missed exactly that way.
            status: int(worker_counts.get(status, 0))
            for status in ALL_WORKER_STATUSES
        },
        refunds={
            "pending_manual": int(pending_manual or 0),
            "failed": int(failed_refunds or 0),
        },
        storage=_storage_health(data_dir),
    )


def _storage_health(data_dir: Path) -> dict[str, object]:
    """Report disk pressure, and refuse to invent a backup age.

    Real encrypted off-site backups are Phase 09. Reporting anything other than
    ``None`` here would let the dashboard imply a recovery point that does not
    exist, which is worse than showing nothing.
    """
    try:
        usage = shutil.disk_usage(data_dir)
        used_percent = round(usage.used / usage.total * 100, 1)
    except (OSError, ZeroDivisionError):
        used_percent = None
    return {
        "used_percent": used_percent,
        "latest_backup_age_seconds": None,
    }


def _search_base() -> Select:
    return (
        select(Order, QuoteSession, User.public_id)
        .join(QuoteSession, QuoteSession.id == Order.quote_session_id)
        .join(User, User.id == QuoteSession.owner_user_id)
    )


def search_orders(
    session: Session,
    *,
    query: str | None = None,
    state: str | None = None,
    created_from: datetime | None = None,
    created_to: datetime | None = None,
    cursor: str | None = None,
    page_size: int = DEFAULT_PAGE_SIZE,
) -> AdminOrderPage:
    """Keyset-paginate a cross-user order search.

    Keyset rather than offset for the same reason as the mini-program list: rows
    are being created while an operator pages, and OFFSET would silently repeat
    or skip one. The cursor is unsigned, which is safe here because there is no
    ownership predicate to bypass — an admin may read every order anyway.
    """
    statement = _search_base()

    if query:
        # Exact matches only. A LIKE across these columns would let an operator
        # accidentally page through the entire table, and none of these
        # identifiers are things anyone types partially.
        statement = statement.where(
            or_(
                Order.id == query,
                User.public_id == query,
                Payment.external_transaction_id == query,
            )
        ).outerjoin(Payment, Payment.quote_session_id == QuoteSession.id)
    if state:
        statement = statement.where(Order.state == state)
    if created_from is not None:
        statement = statement.where(Order.created_at >= created_from)
    if created_to is not None:
        statement = statement.where(Order.created_at <= created_to)

    if cursor is not None:
        created_at, order_id = decode_cursor(cursor)
        statement = statement.where(
            or_(
                Order.created_at < created_at,
                (Order.created_at == created_at) & (Order.id < order_id),
            )
        )

    statement = statement.order_by(Order.created_at.desc(), Order.id.desc()).limit(
        page_size + 1
    )
    rows = session.execute(statement).all()

    has_more = len(rows) > page_size
    visible = rows[:page_size]
    next_cursor = (
        encode_cursor(visible[-1][0].created_at, visible[-1][0].id)
        if has_more and visible
        else None
    )
    return AdminOrderPage(
        items=tuple(
            AdminOrderRow(order=order, quote=quote, owner_public_id=public_id)
            for order, quote, public_id in visible
        ),
        next_cursor=next_cursor,
    )


def load_order_detail(session: Session, order_id: str) -> AdminOrderDetail | None:
    row = session.execute(
        _search_base().where(Order.id == order_id)
    ).one_or_none()
    if row is None:
        return None
    order, quote, public_id = row

    payment = session.scalar(
        select(Payment).where(Payment.quote_session_id == quote.id)
    )
    refunds = (
        tuple(
            session.scalars(
                select(Refund)
                .where(Refund.payment_id == payment.id)
                .order_by(Refund.created_at)
            ).all()
        )
        if payment is not None
        else ()
    )

    rounds = tuple(
        session.scalars(
            select(GradingRound)
            .where(GradingRound.order_id == order.id)
            .order_by(GradingRound.round_number)
        ).all()
    )
    jobs = {
        job.round_number: job
        for job in session.scalars(
            select(GradingJob).where(GradingJob.order_id == order.id)
        ).all()
    }

    return AdminOrderDetail(
        order=order,
        quote=quote,
        owner_public_id=public_id,
        payment=payment,
        refunds=refunds,
        rounds=tuple((round_, jobs.get(round_.round_number)) for round_ in rounds),
        files=_describe_files(session, quote, rounds),
        timeline=_build_timeline(order, payment, refunds, rounds),
    )


def _describe_files(
    session: Session,
    quote: QuoteSession,
    rounds: tuple[GradingRound, ...],
) -> tuple[tuple[str, int], ...]:
    """Describe attachments by logical name and size only.

    Deliberately never reads ``relative_path``: an admin needs to know a
    reference PDF exists, not where on the server's disk it is kept. Leaking the
    layout would help an attacker who later finds a path-traversal bug.
    """
    from server.models import FileObject

    wanted: list[tuple[str, str | None]] = [
        ("source_pdf", quote.source_file_id),
        ("reference_pdf", quote.reference_file_id),
    ]
    for round_ in rounds:
        wanted.append((f"round{round_.round_number}_result_pdf", round_.result_pdf_file_id))
        wanted.append(
            (f"round{round_.round_number}_result_json", round_.result_json_file_id)
        )

    described: list[tuple[str, int]] = []
    for kind, file_id in wanted:
        if file_id is None:
            continue
        size = session.scalar(
            select(FileObject.size_bytes).where(FileObject.id == file_id)
        )
        if size is not None:
            described.append((kind, int(size)))
    return tuple(described)


def _build_timeline(
    order: Order,
    payment: Payment | None,
    refunds: tuple[Refund, ...],
    rounds: tuple[GradingRound, ...],
) -> tuple[TimelineEvent, ...]:
    events = [TimelineEvent(event="order_created", at=order.created_at)]
    if payment is not None:
        events.append(TimelineEvent(event="payment_recorded", at=payment.created_at))
    for round_ in rounds:
        events.append(
            TimelineEvent(event=f"round{round_.round_number}_queued", at=round_.created_at)
        )
        if round_.delivered_at is not None:
            events.append(
                TimelineEvent(
                    event=f"round{round_.round_number}_delivered",
                    at=round_.delivered_at,
                )
            )
    for refund in refunds:
        events.append(
            TimelineEvent(event=f"refund_{refund.state}", at=refund.created_at)
        )
    if order.downloads_revoked_at is not None:
        events.append(
            TimelineEvent(event="downloads_revoked", at=order.downloads_revoked_at)
        )
    return tuple(sorted(events, key=lambda event: event.at))


#: Order states from which an admin may issue a technical refund: the user has
#: paid and we have either not delivered yet or failed outright.
TECHNICAL_REFUND_STATES = frozenset(
    {
        OrderState.V1_QUEUED,
        OrderState.V1_RUNNING,
        OrderState.V1_DELIVERED,
        OrderState.V2_QUEUED,
        OrderState.V2_RUNNING,
        OrderState.V2_DELIVERED,
        OrderState.ACCEPTED,
    }
)


def available_admin_actions(detail: AdminOrderDetail) -> tuple[str, ...]:
    """Advisory list for the UI. Every action re-checks its own preconditions."""
    actions: list[str] = []
    if OrderState(detail.order.state) in TECHNICAL_REFUND_STATES:
        actions.append("technical_refund")
    if any(refund.state == RefundState.PENDING for refund in detail.refunds):
        actions.extend(("approve_refund", "reject_refund"))
    if any(refund.state == RefundState.REFUND_FAILED for refund in detail.refunds):
        actions.append("retry_refund")
    return tuple(actions)


__all__ = [
    "AdminOrderDetail",
    "AdminOrderPage",
    "AdminOrderRow",
    "DEFAULT_PAGE_SIZE",
    "InvalidCursor",
    "MAX_PAGE_SIZE",
    "OverviewSnapshot",
    "TimelineEvent",
    "available_admin_actions",
    "collect_overview",
    "load_order_detail",
    "search_orders",
]
