"""Admin refund decisions.

Three operations: approve a manual refund, decline it, or issue a technical
refund when we failed the user. All three write an AuditLog row naming the real
admin who acted.

The amount is never taken from the request. It always comes from the order row
and always returns to the original transaction, which is what bounds the damage
if the Admin shared key leaks.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, Field

from server.api.admin_dependencies import CurrentAdmin
from server.api.dependencies import DatabaseSession
from server.api.refund_dependencies import build_refund_service
from server.models import AuditLog
from server.services.refunds import (
    RefundNotDecidable,
    RefundNotFound,
    RefundOutcome,
    RefundService,
)


router = APIRouter(prefix="/admin/api/v1/refunds", tags=["admin-refunds"])

_REFUND_NOT_FOUND = HTTPException(
    status_code=status.HTTP_404_NOT_FOUND,
    detail="退款记录不存在。",
)
#: Deliberately free of amounts, external ids and user metrics.
_NOT_DECIDABLE = HTTPException(
    status_code=status.HTTP_409_CONFLICT,
    detail="该退款已处理，无法再次决定。",
)


class RejectRequestBody(BaseModel):
    reason: str = Field(min_length=1, max_length=500)


class TechnicalRefundBody(BaseModel):
    """Only the order and a reason. The amount is not negotiable.

    ``model_config`` forbids extra fields so a request that tries to smuggle an
    amount or a destination account is rejected outright rather than silently
    ignored.
    """

    model_config = {"extra": "forbid"}

    order_id: str
    reason: str = Field(min_length=1, max_length=500)


class RefundDecisionView(BaseModel):
    refund_id: str
    state: str
    order_id: str
    order_state: str


def _view(outcome: RefundOutcome) -> RefundDecisionView:
    return RefundDecisionView(
        refund_id=outcome.refund_id,
        state=str(outcome.state),
        order_id=outcome.order_id,
        order_state=str(outcome.order_state),
    )


def _audit(
    session: DatabaseSession,
    *,
    admin_id: str,
    action: str,
    refund_id: str,
    details: dict[str, object],
) -> None:
    """Record who did what to which refund.

    Details carry decision metadata only — never the amount, the external refund
    id, the shared key or the user's refund metrics.
    """
    session.add(
        AuditLog(
            actor_type="admin",
            actor_id=admin_id,
            action=action,
            target_type="refund",
            target_id=refund_id,
            details=details,
        )
    )
    session.commit()


@router.post("/{refund_id}/approve", response_model=RefundDecisionView)
def approve_refund(
    refund_id: str,
    admin: CurrentAdmin,
    session: DatabaseSession,
    request: Request,
) -> RefundDecisionView:
    """Execute a manual refund through the same idempotent path as automatic."""
    service = build_refund_service(request)
    # Record the intent before the money moves. If the gateway call or the
    # write-back fails, an approval that may already have reached the provider
    # must still leave a trace naming the admin who authorised it.
    _audit(
        session,
        admin_id=admin.id,
        action="refund.approve",
        refund_id=refund_id,
        details={"stage": "requested"},
    )
    try:
        outcome = service.execute(refund_id, reason="admin_approved")
    except RefundNotFound:
        raise _REFUND_NOT_FOUND from None
    except RefundNotDecidable:
        raise _NOT_DECIDABLE from None

    _audit(
        session,
        admin_id=admin.id,
        action="refund.approve",
        refund_id=refund_id,
        details={"stage": "settled", "result": str(outcome.state)},
    )
    return _view(outcome)


@router.post("/{refund_id}/reject", response_model=RefundDecisionView)
def reject_refund(
    refund_id: str,
    payload: RejectRequestBody,
    admin: CurrentAdmin,
    session: DatabaseSession,
    request: Request,
) -> RefundDecisionView:
    """Decline a manual refund; the order returns to ACCEPTED with downloads."""
    service = build_refund_service(request)
    try:
        outcome = service.reject(
            refund_id, admin_id=admin.id, reason=payload.reason
        )
    except RefundNotFound:
        raise _REFUND_NOT_FOUND from None
    except RefundNotDecidable:
        raise _NOT_DECIDABLE from None

    _audit(
        session,
        admin_id=admin.id,
        action="refund.reject",
        refund_id=refund_id,
        details={"reason": payload.reason},
    )
    return _view(outcome)


@router.post(
    "/technical",
    response_model=RefundDecisionView,
    status_code=status.HTTP_201_CREATED,
)
def create_technical_refund(
    payload: TechnicalRefundBody,
    admin: CurrentAdmin,
    session: DatabaseSession,
    request: Request,
) -> RefundDecisionView:
    """Refund an order because we failed it, bypassing the user policy.

    Recorded with source admin_technical so it never counts against the user's
    monthly quota or cumulative refund ratio.
    """
    service = build_refund_service(request)
    try:
        refund_id = service.create_technical_refund(
            order_id=payload.order_id,
            admin_id=admin.id,
            reason=payload.reason,
        )
        outcome = service.execute(refund_id, reason=payload.reason)
    except RefundNotFound:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="订单不存在。",
        ) from None
    except RefundNotDecidable:
        raise _NOT_DECIDABLE from None

    _audit(
        session,
        admin_id=admin.id,
        action="refund.technical",
        refund_id=refund_id,
        details={"reason": payload.reason, "result": str(outcome.state)},
    )
    return _view(outcome)
