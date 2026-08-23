"""Admin overview and order search endpoints.

Every response here is assembled field by field rather than dumped from the ORM,
which is what keeps ``FileObject.relative_path`` and other storage details out of
the Admin API. Attachments are named logically instead.
"""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel

from server.api.admin_dependencies import CurrentAdmin, Settings
from server.api.dependencies import DatabaseSession
from server.services.admin_orders import (
    DEFAULT_PAGE_SIZE,
    MAX_PAGE_SIZE,
    InvalidCursor,
    available_admin_actions,
    collect_overview,
    load_order_detail,
    search_orders,
)


router = APIRouter(prefix="/admin/api/v1", tags=["admin-orders"])

_ORDER_NOT_FOUND = HTTPException(
    status_code=status.HTTP_404_NOT_FOUND,
    detail="订单不存在。",
)
_BAD_CURSOR = HTTPException(
    status_code=status.HTTP_400_BAD_REQUEST,
    detail="翻页游标无效。",
)


class OverviewView(BaseModel):
    orders: dict[str, int]
    jobs: dict[str, int]
    workers: dict[str, int]
    refunds: dict[str, int]
    storage: dict[str, object]


class OrderRowView(BaseModel):
    id: str
    state: str
    owner_public_id: str
    paid_amount_cents: int
    page_count: int
    current_round_number: int
    created_at: datetime


class OrderListView(BaseModel):
    items: list[OrderRowView]
    next_cursor: str | None


class JobView(BaseModel):
    id: str
    state: str
    worker_id: str | None
    attempt_count: int
    lease_version: int
    lease_expires_at: datetime | None


class RoundView(BaseModel):
    round_number: int
    delivered_at: datetime | None
    has_result_pdf: bool
    has_result_json: bool
    job: JobView | None


class FileView(BaseModel):
    """Logical name and size. Deliberately carries no path."""

    kind: str
    size_bytes: int


class RefundView(BaseModel):
    id: str
    state: str
    source: str
    amount_cents: int
    created_at: datetime


class PaymentView(BaseModel):
    id: str
    state: str
    amount_cents: int
    external_transaction_id: str | None


class TimelineView(BaseModel):
    event: str
    at: datetime


class OrderDetailView(BaseModel):
    id: str
    state: str
    owner_public_id: str
    paid_amount_cents: int
    page_count: int
    grading_standard: str
    note: str
    current_round_number: int
    acceptance_deadline: datetime | None
    downloads_revoked_at: datetime | None
    created_at: datetime
    payment: PaymentView | None
    refunds: list[RefundView]
    rounds: list[RoundView]
    files: list[FileView]
    timeline: list[TimelineView]
    available_admin_actions: list[str]


@router.get("/overview", response_model=OverviewView)
def read_overview(
    admin: CurrentAdmin,
    session: DatabaseSession,
    settings: Settings,
) -> OverviewView:
    del admin
    snapshot = collect_overview(session, data_dir=settings.data_dir)
    return OverviewView(
        orders=snapshot.orders,
        jobs=snapshot.jobs,
        workers=snapshot.workers,
        refunds=snapshot.refunds,
        storage=snapshot.storage,
    )


@router.get("/orders", response_model=OrderListView)
def list_orders(
    admin: CurrentAdmin,
    session: DatabaseSession,
    query: str | None = None,
    state: str | None = None,
    created_from: datetime | None = None,
    created_to: datetime | None = None,
    cursor: str | None = None,
    page_size: int = Query(default=DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
) -> OrderListView:
    del admin
    try:
        page = search_orders(
            session,
            query=query,
            state=state,
            created_from=created_from,
            created_to=created_to,
            cursor=cursor,
            page_size=page_size,
        )
    except InvalidCursor:
        raise _BAD_CURSOR from None

    return OrderListView(
        items=[
            OrderRowView(
                id=row.order.id,
                state=row.order.state,
                owner_public_id=row.owner_public_id,
                paid_amount_cents=row.order.paid_amount_cents,
                page_count=row.quote.page_count,
                current_round_number=row.order.current_round_number,
                created_at=row.order.created_at,
            )
            for row in page.items
        ],
        next_cursor=page.next_cursor,
    )


@router.get("/orders/{order_id}", response_model=OrderDetailView)
def read_order(
    order_id: str,
    admin: CurrentAdmin,
    session: DatabaseSession,
) -> OrderDetailView:
    del admin
    detail = load_order_detail(session, order_id)
    if detail is None:
        raise _ORDER_NOT_FOUND

    return OrderDetailView(
        id=detail.order.id,
        state=detail.order.state,
        owner_public_id=detail.owner_public_id,
        paid_amount_cents=detail.order.paid_amount_cents,
        page_count=detail.quote.page_count,
        grading_standard=detail.quote.grading_standard,
        note=detail.quote.note,
        current_round_number=detail.order.current_round_number,
        acceptance_deadline=detail.order.acceptance_deadline,
        downloads_revoked_at=detail.order.downloads_revoked_at,
        created_at=detail.order.created_at,
        payment=(
            PaymentView(
                id=detail.payment.id,
                state=detail.payment.state,
                amount_cents=detail.payment.amount_cents,
                external_transaction_id=detail.payment.external_transaction_id,
            )
            if detail.payment is not None
            else None
        ),
        refunds=[
            RefundView(
                id=refund.id,
                state=refund.state,
                source=refund.source,
                amount_cents=refund.amount_cents,
                created_at=refund.created_at,
            )
            for refund in detail.refunds
        ],
        rounds=[
            RoundView(
                round_number=round_.round_number,
                delivered_at=round_.delivered_at,
                has_result_pdf=round_.result_pdf_file_id is not None,
                has_result_json=round_.result_json_file_id is not None,
                job=(
                    JobView(
                        id=job.id,
                        state=job.state,
                        worker_id=job.worker_id,
                        attempt_count=job.attempt_count,
                        lease_version=job.lease_version,
                        lease_expires_at=job.lease_expires_at,
                    )
                    if job is not None
                    else None
                ),
            )
            for round_, job in detail.rounds
        ],
        files=[FileView(kind=kind, size_bytes=size) for kind, size in detail.files],
        timeline=[
            TimelineView(event=event.event, at=event.at) for event in detail.timeline
        ],
        available_admin_actions=list(available_admin_actions(detail)),
    )
