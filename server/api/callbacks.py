from __future__ import annotations

from fastapi import APIRouter, HTTPException, Response, status

from server.api.dependencies import DatabaseSession
from server.schemas.payments import FakeCallbackBody
from server.services.payments import (
    CallbackRejected,
    PaymentNotFound,
    confirm_by_transaction,
)


router = APIRouter(prefix="/callbacks", tags=["payment-callbacks"])

SUCCESS_STATUS = "SUCCESS"


@router.post("/fake/pay", status_code=status.HTTP_204_NO_CONTENT)
def fake_payment_callback(
    payload: FakeCallbackBody,
    session: DatabaseSession,
) -> Response:
    """Authoritative server-side notification for the fake gateway."""
    if payload.status != SUCCESS_STATUS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="支付未成功。",
        )

    try:
        confirm_by_transaction(
            session=session,
            prepay_id=payload.fake_transaction_id,
            external_transaction_id=payload.fake_transaction_id,
        )
    except PaymentNotFound:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="支付记录不存在。",
        ) from None
    except CallbackRejected as error:
        session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(error),
        ) from None

    return Response(status_code=status.HTTP_204_NO_CONTENT)
