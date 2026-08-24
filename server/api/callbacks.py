from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, Response, status

from server.adapters.wechat_pay import (
    WeChatPayGateway,
    WeChatPayNotificationError,
)
from server.api.dependencies import DatabaseSession
from server.schemas.payments import FakeCallbackBody
from server.services.payments import (
    CallbackRejected,
    PaymentNotFound,
    confirm_by_merchant_order,
    confirm_by_transaction,
)
from server.services.refunds import (
    RefundNotDecidable,
    RefundNotFound,
    RefundService,
)


router = APIRouter(prefix="/callbacks", tags=["payment-callbacks"])
wechat_router = APIRouter(prefix="/callbacks/wechat", tags=["wechat-callbacks"])

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


def _wechat_gateway(request: Request) -> WeChatPayGateway:
    gateway = request.app.state.payment_gateway
    if not isinstance(gateway, WeChatPayGateway):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    return gateway


async def _notification(request: Request) -> dict:
    body = await request.body()
    try:
        return _wechat_gateway(request).parse_notification(request.headers, body)
    except WeChatPayNotificationError as error:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="微信支付通知验证失败。",
        ) from error


@wechat_router.post("/pay", status_code=status.HTTP_204_NO_CONTENT)
async def wechat_payment_callback(
    request: Request,
    session: DatabaseSession,
) -> Response:
    notification = await _notification(request)
    payload = notification["resource"]
    if (
        notification.get("event_type") != "TRANSACTION.SUCCESS"
        or payload.get("trade_state") != "SUCCESS"
    ):
        return Response(status_code=status.HTTP_204_NO_CONTENT)
    try:
        confirm_by_merchant_order(
            session=session,
            merchant_order_id=str(payload["out_trade_no"]),
            external_transaction_id=str(payload["transaction_id"]),
            paid_amount_cents=int(payload["amount"]["total"]),
        )
    except (KeyError, TypeError, ValueError, PaymentNotFound, CallbackRejected) as error:
        session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="支付通知与本地订单不一致。",
        ) from error
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@wechat_router.post("/refund", status_code=status.HTTP_204_NO_CONTENT)
async def wechat_refund_callback(request: Request) -> Response:
    notification = await _notification(request)
    payload = notification["resource"]
    refund_status = payload.get("refund_status")
    if refund_status == "PROCESSING":
        return Response(status_code=status.HTTP_204_NO_CONTENT)
    try:
        RefundService(
            request.app.state.session_factory,
            request.app.state.payment_gateway,
        ).settle_notification(
            external_refund_id=str(payload["out_refund_no"]),
            succeeded=refund_status == "SUCCESS",
        )
    except (KeyError, RefundNotFound, RefundNotDecidable) as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="退款通知与本地记录不一致。",
        ) from error
    return Response(status_code=status.HTTP_204_NO_CONTENT)
