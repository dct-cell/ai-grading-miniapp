from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from types import MappingProxyType
from typing import Final, Mapping

from sqlalchemy import Select, or_, select
from sqlalchemy.orm import Session

from server.domain.eta import EtaRange, estimate_ranges
from server.domain.states import JobState, OrderState
from server.models import (
    Appeal,
    GradingJob,
    GradingRound,
    Order,
    QuoteSession,
    Worker,
)
from server.services.aftersales import available_actions
from server.services.workers import WorkerStatus


MAX_PAGE_SIZE = 50
DEFAULT_PAGE_SIZE = 20

#: Observed average from the verified grading runtime: one page of a competition
#: solution takes roughly ten minutes end to end, including LaTeX rendering.
MINUTES_PER_PAGE = 10


class OrderCategory(StrEnum):
    ALL = "all"
    GRADING = "grading"
    ACCEPTANCE = "acceptance"


GRADING_STATES: Final[frozenset[OrderState]] = frozenset(
    {
        OrderState.V1_QUEUED,
        OrderState.V1_RUNNING,
        OrderState.V2_QUEUED,
        OrderState.V2_RUNNING,
    }
)

ACCEPTANCE_STATES: Final[frozenset[OrderState]] = frozenset(
    {
        OrderState.V1_DELIVERED,
        OrderState.V2_DELIVERED,
        OrderState.REFUND_PENDING,
    }
)

CATEGORY_STATES: Final[Mapping[OrderCategory, frozenset[OrderState]]] = (
    MappingProxyType(
        {
            OrderCategory.ALL: frozenset(OrderState),
            OrderCategory.GRADING: GRADING_STATES,
            OrderCategory.ACCEPTANCE: ACCEPTANCE_STATES,
        }
    )
)


class InvalidCursor(ValueError):
    """The supplied pagination cursor is not a cursor this service issued."""


@dataclass(frozen=True)
class OrderSummary:
    order: Order
    quote: QuoteSession


@dataclass(frozen=True)
class OrderPage:
    items: tuple[OrderSummary, ...]
    next_cursor: str | None


@dataclass(frozen=True)
class OrderDetail:
    order: Order
    quote: QuoteSession
    rounds: tuple[tuple[GradingRound, GradingJob | None], ...]
    appeal_text: str | None = None
    available_actions: tuple[str, ...] = ()
    eta: EtaRange | None = None


def category_of(state: str) -> OrderCategory:
    if state in GRADING_STATES:
        return OrderCategory.GRADING
    if state in ACCEPTANCE_STATES:
        return OrderCategory.ACCEPTANCE
    return OrderCategory.ALL


def encode_cursor(created_at: datetime, order_id: str) -> str:
    raw = f"{created_at.astimezone(timezone.utc).isoformat()}|{order_id}"
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii")


def decode_cursor(cursor: str) -> tuple[datetime, str]:
    try:
        raw = base64.urlsafe_b64decode(cursor.encode("ascii")).decode("utf-8")
        timestamp, separator, order_id = raw.partition("|")
        if not separator or not order_id:
            raise ValueError("missing separator")
        return datetime.fromisoformat(timestamp), order_id
    except (ValueError, binascii.Error, UnicodeDecodeError) as error:
        raise InvalidCursor("翻页游标无效。") from error


def _owned_orders(owner_user_id: str) -> Select:
    return (
        select(Order, QuoteSession)
        .join(QuoteSession, QuoteSession.id == Order.quote_session_id)
        .where(QuoteSession.owner_user_id == owner_user_id)
    )


def list_orders(
    *,
    session: Session,
    owner_user_id: str,
    category: OrderCategory = OrderCategory.ALL,
    cursor: str | None = None,
    limit: int = DEFAULT_PAGE_SIZE,
) -> OrderPage:
    """List orders owned by the authenticated user only.

    Ownership always comes from owner_user_id; callers must never derive it
    from client-supplied query parameters.
    """
    page_size = max(1, min(limit, MAX_PAGE_SIZE))
    statement = _owned_orders(owner_user_id)
    if category is not OrderCategory.ALL:
        statement = statement.where(Order.state.in_(CATEGORY_STATES[category]))
    if cursor:
        created_at, order_id = decode_cursor(cursor)
        statement = statement.where(
            or_(
                Order.created_at < created_at,
                (Order.created_at == created_at) & (Order.id < order_id),
            )
        )

    rows = session.execute(
        statement.order_by(Order.created_at.desc(), Order.id.desc()).limit(
            page_size + 1
        )
    ).all()

    has_more = len(rows) > page_size
    visible = rows[:page_size]
    items = tuple(OrderSummary(order=row[0], quote=row[1]) for row in visible)
    next_cursor = (
        encode_cursor(items[-1].order.created_at, items[-1].order.id)
        if has_more and items
        else None
    )
    return OrderPage(items=items, next_cursor=next_cursor)


def get_order_detail(
    *,
    session: Session,
    owner_user_id: str,
    order_id: str,
    now: datetime | None = None,
) -> OrderDetail | None:
    row = session.execute(
        _owned_orders(owner_user_id).where(Order.id == order_id)
    ).one_or_none()
    if row is None:
        return None
    order, quote = row
    rounds = session.scalars(
        select(GradingRound)
        .where(GradingRound.order_id == order.id)
        .order_by(GradingRound.round_number)
    ).all()
    jobs = {
        job.round_number: job
        for job in session.scalars(
            select(GradingJob).where(GradingJob.order_id == order.id)
        ).all()
    }
    appeal_text = session.scalar(
        select(Appeal.text).where(Appeal.order_id == order.id)
    )
    return OrderDetail(
        order=order,
        quote=quote,
        rounds=tuple(
            (record, jobs.get(record.round_number)) for record in rounds
        ),
        appeal_text=appeal_text,
        available_actions=tuple(
            str(action)
            for action in available_actions(session, order, now=now)
        ),
        eta=order_eta(session=session, order_id=order.id, now=now),
    )


#: Job states that still represent work waiting for, or running on, a Worker.
PENDING_JOB_STATES: Final[frozenset[str]] = frozenset(
    {JobState.QUEUED, JobState.LEASED, JobState.RUNNING, JobState.UPLOADING}
)

#: Workers that can pick up the next queued job. suspected_offline and disabled
#: Workers are excluded: counting them would promise a turnaround nobody is
#: working towards.
READY_WORKER_STATES: Final[frozenset[str]] = frozenset({WorkerStatus.ONLINE})


def order_eta(
    *,
    session: Session,
    order_id: str,
    now: datetime | None = None,
    minutes_per_page: int = MINUTES_PER_PAGE,
) -> EtaRange | None:
    """Estimate when this order's outstanding round will finish.

    Returns None when there is nothing honest to say: the order has no pending
    job (delivered, accepted, refunded, or failed with worker_exception), or no
    Worker is ready to pick anything up.
    """
    moment = now or datetime.now(timezone.utc)

    pending = session.execute(
        select(GradingJob.order_id, GradingJob.id, GradingJob.state, QuoteSession.page_count)
        .join(Order, Order.id == GradingJob.order_id)
        .join(QuoteSession, QuoteSession.id == Order.quote_session_id)
        .where(GradingJob.state.in_(PENDING_JOB_STATES))
        .order_by(GradingJob.queued_at, GradingJob.id)
    ).all()
    if not any(row.order_id == order_id for row in pending):
        return None

    ready_workers = session.scalars(
        select(Worker).where(Worker.status.in_(READY_WORKER_STATES))
    ).all()
    if not ready_workers:
        return None

    # A Worker already running a job cannot start the next one until it is done.
    running_pages = {
        row.id: row.page_count
        for row in pending
        if row.state in {JobState.LEASED, JobState.RUNNING, JobState.UPLOADING}
    }
    available_minutes: list[int] = []
    for worker in ready_workers:
        pages = running_pages.get(worker.current_job_id, 0)
        available_minutes.append(pages * minutes_per_page)

    # Only unstarted work is queued; a job already on a Worker is accounted for
    # through that Worker's remaining time instead.
    queue = [
        (row.order_id, row.page_count)
        for row in pending
        if row.state == JobState.QUEUED
    ]
    ranges = estimate_ranges(
        now=moment,
        worker_available_minutes=available_minutes,
        queued=queue,
        minutes_per_page=minutes_per_page,
    )
    if order_id in ranges:
        return ranges[order_id]

    # The order's job is already on a Worker, so it was never in the queue we
    # simulated. Estimate it from its own remaining pages instead of dropping
    # the countdown.
    for row in pending:
        if row.order_id != order_id:
            continue
        return estimate_ranges(
            now=moment,
            worker_available_minutes=[0],
            queued=[(order_id, row.page_count)],
            minutes_per_page=minutes_per_page,
        )[order_id]
    return None
