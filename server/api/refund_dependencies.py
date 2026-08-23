"""Shared construction of the RefundService.

Both the user-facing refund endpoint and the Admin decision endpoints execute
refunds through the same service and the same idempotent method, so the factory
lives here rather than in either router.

The gateway is a replaceable seam: FakePaymentGateway stands in until WeChat Pay
credentials exist, and swapping in the real adapter must not change any caller.
"""
from __future__ import annotations

from fastapi import Request

from server.adapters.payments import FakePaymentGateway
from server.services.refunds import RefundService


def build_refund_service(request: Request) -> RefundService:
    return RefundService(
        request.app.state.session_factory,
        FakePaymentGateway(),
    )
