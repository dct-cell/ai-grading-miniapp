from __future__ import annotations

from typing import cast

from server.adapters.auth import AuthProvider, FakeAuthProvider, WeChatAuthProvider
from server.adapters.payments import FakePaymentGateway, PaymentGateway
from server.adapters.wechat_pay import WeChatPayGateway
from server.config import Environment, ServerSettings


def build_auth_provider(settings: ServerSettings) -> AuthProvider:
    if settings.environment is not Environment.PRODUCTION and not settings.wechat_live_mode:
        return FakeAuthProvider()
    settings.require_wechat_production_settings()
    return WeChatAuthProvider(
        app_id=cast(str, settings.wechat_app_id),
        app_secret=cast(str, settings.wechat_app_secret),
    )


def build_payment_gateway(settings: ServerSettings) -> PaymentGateway:
    if settings.environment is not Environment.PRODUCTION and not settings.wechat_live_mode:
        return FakePaymentGateway()
    settings.require_wechat_production_settings()
    return WeChatPayGateway(
        app_id=cast(str, settings.wechat_app_id),
        merchant_id=cast(str, settings.wechat_pay_merchant_id),
        certificate_serial=cast(str, settings.wechat_pay_certificate_serial),
        private_key_path=settings.wechat_pay_private_key_path,
        public_key_id=cast(str, settings.wechat_pay_public_key_id),
        public_key_path=settings.wechat_pay_public_key_path,
        api_v3_key=cast(str, settings.wechat_pay_api_v3_key),
        payment_notify_url=settings.wechat_pay_notify_url,
        refund_notify_url=settings.wechat_pay_refund_notify_url,
    )
