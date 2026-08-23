"""Mini-program aftersales actions: accept, review and refund.

These routes only translate HTTP into service calls and errors into status
codes. Ownership comes from the session, never from the request, and an order
that belongs to somebody else is reported as 404 rather than 403 so the API
does not confirm that another user's order id exists.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, status

from server.api.dependencies import CurrentUser, DatabaseSession
from server.api.refund_dependencies import build_refund_service
from server.schemas.aftersales import (
    OrderActionView,
    RefundRequestBody,
    ReviewRequestBody,
)
from server.services.aftersales import (
    ActionNotAllowed,
    ActionOutcome,
    OrderNotAvailable,
    accept_order,
    request_refund,
    request_review,
)


router = APIRouter(prefix="/api/v1/orders", tags=["miniapp-aftersales"])

_ORDER_NOT_FOUND = HTTPException(
    status_code=status.HTTP_404_NOT_FOUND,
    detail="订单不存在。",
)


def _view(outcome: ActionOutcome) -> OrderActionView:
    return OrderActionView(
        order_id=outcome.order_id,
        state=str(outcome.state),
        amount_cents=outcome.amount_cents,
        refund_id=outcome.refund_id,
        appeal_id=outcome.appeal_id,
    )


@router.post("/{order_id}/accept", response_model=OrderActionView)
def accept(
    order_id: str,
    user: CurrentUser,
    session: DatabaseSession,
) -> OrderActionView:
    try:
        outcome = accept_order(
            session=session, owner_user_id=user.id, order_id=order_id
        )
    except OrderNotAvailable:
        raise _ORDER_NOT_FOUND from None
    except ActionNotAllowed as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=str(error)
        ) from None
    return _view(outcome)


@router.post(
    "/{order_id}/review",
    response_model=OrderActionView,
    status_code=status.HTTP_202_ACCEPTED,
)
def review(
    order_id: str,
    payload: ReviewRequestBody,
    user: CurrentUser,
    session: DatabaseSession,
) -> OrderActionView:
    """Buy the single second grading round. 202: a Worker grades it later."""
    try:
        outcome = request_review(
            session=session,
            owner_user_id=user.id,
            order_id=order_id,
            text=payload.text,
        )
    except OrderNotAvailable:
        raise _ORDER_NOT_FOUND from None
    except ActionNotAllowed as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=str(error)
        ) from None
    return _view(outcome)


@router.post(
    "/{order_id}/refund",
    response_model=OrderActionView,
    status_code=status.HTTP_202_ACCEPTED,
)
def refund(
    order_id: str,
    payload: RefundRequestBody,
    user: CurrentUser,
    session: DatabaseSession,
    request: Request,
) -> OrderActionView:
    """Open a full refund, and settle it when policy allows it automatically.

    The refund record is committed before the payment provider is contacted, so
    a failure while talking to the provider leaves a retryable row rather than
    losing the request. Refunds that exceed the policy limits stay pending for
    an Admin decision.
    """
    try:
        outcome = request_refund(
            session=session,
            owner_user_id=user.id,
            order_id=order_id,
            reason=payload.reason,
        )
    except OrderNotAvailable:
        raise _ORDER_NOT_FOUND from None
    except ActionNotAllowed as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=str(error)
        ) from None

    settled = build_refund_service(request).route_and_execute(outcome.refund_id)
    return OrderActionView(
        order_id=outcome.order_id,
        state=str(settled.order_state),
        amount_cents=outcome.amount_cents,
        refund_id=outcome.refund_id,
    )
