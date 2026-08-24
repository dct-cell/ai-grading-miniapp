"""Shared construction of the RefundService.

Both the user-facing refund endpoint and the Admin decision endpoints execute
refunds through the same service and the same idempotent method, so the factory
lives here rather than in either router.

The application factory injects a fake gateway outside production and the
signed WeChat Pay API v3 gateway in production; callers share one service path.
"""
from __future__ import annotations

from fastapi import Request

from server.services.refunds import RefundService


def build_refund_service(request: Request) -> RefundService:
    return RefundService(
        request.app.state.session_factory,
        request.app.state.payment_gateway,
    )
