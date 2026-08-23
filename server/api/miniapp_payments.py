from __future__ import annotations

from secrets import token_hex

from fastapi import APIRouter, HTTPException, Response, status

from server.adapters.payments import FakePaymentGateway
from server.api.dependencies import CurrentUser, DatabaseSession
from server.schemas.payments import PrepayRequestBody, PrepayView
from server.services.payments import (
    CallbackRejected,
    QuoteNotAvailable,
    QuoteNotPayable,
    confirm_payment,
    create_prepay,
    get_owned_payment,
)


router = APIRouter(prefix="/api/v1/payments", tags=["miniapp-payments"])
fake_router = APIRouter(prefix="/api/v1/payments", tags=["miniapp-payments-fake"])


@router.post(
    "/prepay",
    response_model=PrepayView,
    status_code=status.HTTP_201_CREATED,
)
def create_payment_intent(
    payload: PrepayRequestBody,
    user: CurrentUser,
    session: DatabaseSession,
) -> PrepayView:
    try:
        intent = create_prepay(
            session=session,
            gateway=FakePaymentGateway(),
            owner_user_id=user.id,
            quote_id=payload.quote_id,
        )
    except QuoteNotAvailable:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="报价不存在或已失效。",
        ) from None
    except QuoteNotPayable as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(error),
        ) from None

    return PrepayView(
        payment_id=intent.payment.id,
        prepay_id=intent.payment.prepay_id,
        amount_cents=intent.payment.amount_cents,
        client_payload=intent.client_payload,
    )


@fake_router.post(
    "/{payment_id}/simulate-success",
    status_code=status.HTTP_204_NO_CONTENT,
)
def simulate_success(
    payment_id: str,
    user: CurrentUser,
    session: DatabaseSession,
) -> Response:
    """Non-production helper that drives the same verified callback service."""
    payment = get_owned_payment(session, user.id, payment_id)
    if payment is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="支付记录不存在。",
        )

    transaction_id = payment.external_transaction_id or f"fake-{token_hex(8)}"
    try:
        confirm_payment(
            session=session,
            payment_id=payment.id,
            external_transaction_id=transaction_id,
            paid_amount_cents=payment.amount_cents,
        )
    except CallbackRejected as error:
        session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(error),
        ) from None

    return Response(status_code=status.HTTP_204_NO_CONTENT)
