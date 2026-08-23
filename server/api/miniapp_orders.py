from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, status

from server.api.dependencies import CurrentUser, DatabaseSession
from server.domain.service_tiers import service_tier_label
from server.schemas.orders import (
    OrderDetailView,
    OrderEtaView,
    OrderPageView,
    OrderRoundView,
    OrderSummaryView,
)
from server.services.orders import (
    DEFAULT_PAGE_SIZE,
    MAX_PAGE_SIZE,
    InvalidCursor,
    OrderCategory,
    OrderSummary,
    category_of,
    get_order_detail,
    list_orders,
)


router = APIRouter(prefix="/api/v1/orders", tags=["miniapp-orders"])

_ORDER_NOT_FOUND = HTTPException(
    status_code=status.HTTP_404_NOT_FOUND,
    detail="订单不存在。",
)


def _summary(entry: OrderSummary) -> OrderSummaryView:
    return OrderSummaryView(
        id=entry.order.id,
        state=entry.order.state,
        category=category_of(entry.order.state),
        service_tier=entry.quote.service_tier,
        service_tier_label=service_tier_label(entry.quote.service_tier),
        grading_standard=entry.quote.grading_standard,
        page_count=entry.quote.page_count,
        paid_amount_cents=entry.order.paid_amount_cents,
        current_round_number=entry.order.current_round_number,
        created_at=entry.order.created_at,
    )


@router.get("", response_model=OrderPageView)
def list_owned_orders(
    user: CurrentUser,
    session: DatabaseSession,
    category: OrderCategory = OrderCategory.ALL,
    cursor: str | None = None,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = DEFAULT_PAGE_SIZE,
) -> OrderPageView:
    """Ownership comes from the session; user_id is never read from the query."""
    try:
        page = list_orders(
            session=session,
            owner_user_id=user.id,
            category=category,
            cursor=cursor,
            limit=limit,
        )
    except InvalidCursor as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(error),
        ) from None

    return OrderPageView(
        items=[_summary(entry) for entry in page.items],
        next_cursor=page.next_cursor,
    )


@router.get("/{order_id}", response_model=OrderDetailView)
def read_owned_order(
    order_id: str,
    user: CurrentUser,
    session: DatabaseSession,
) -> OrderDetailView:
    detail = get_order_detail(
        session=session,
        owner_user_id=user.id,
        order_id=order_id,
    )
    if detail is None:
        raise _ORDER_NOT_FOUND

    summary = _summary(OrderSummary(order=detail.order, quote=detail.quote))
    return OrderDetailView(
        **summary.model_dump(),
        note=detail.quote.note,
        available_actions=list(detail.available_actions),
        appeal_text=detail.appeal_text,
        acceptance_deadline=detail.order.acceptance_deadline,
        eta=(
            OrderEtaView(
                earliest_minutes=detail.eta.earliest_minutes,
                latest_minutes=detail.eta.latest_minutes,
                earliest_at=detail.eta.earliest_at,
                latest_at=detail.eta.latest_at,
            )
            if detail.eta is not None
            else None
        ),
        rounds=[
            OrderRoundView(
                round_number=record.round_number,
                service_tier=record.service_tier,
                state="delivered" if job is None else job.state,
                delivered_at=record.delivered_at,
            )
            for record, job in detail.rounds
        ],
    )
