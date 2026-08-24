from __future__ import annotations

import base64
import json
import time
from pathlib import Path

import httpx
import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from server.adapters.auth import WeChatAuthProvider
from server.adapters.payments import PrepayRequest
from server.adapters.wechat_pay import (
    WeChatPayGateway,
    WeChatPayNotificationError,
)
from server.config import Environment, ServerSettings


def _keys(tmp_path: Path):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_path = tmp_path / "merchant.pem"
    public_path = tmp_path / "wechatpay.pem"
    private_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    public_path.write_bytes(
        key.public_key().public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    )
    return key, private_path, public_path


def _signed_headers(key, body: bytes, *, timestamp: str | None = None) -> dict[str, str]:
    timestamp = timestamp or str(int(time.time()))
    nonce = "wechat-response-nonce"
    message = timestamp.encode() + b"\n" + nonce.encode() + b"\n" + body + b"\n"
    signature = key.sign(message, padding.PKCS1v15(), hashes.SHA256())
    return {
        "Wechatpay-Timestamp": timestamp,
        "Wechatpay-Nonce": nonce,
        "Wechatpay-Signature": base64.b64encode(signature).decode(),
        "Wechatpay-Serial": "WECHAT-PUBLIC-ID",
    }


def _gateway(tmp_path: Path, handler):
    key, private_path, public_path = _keys(tmp_path)
    client = httpx.Client(
        transport=httpx.MockTransport(handler),
        base_url="https://api.mch.weixin.qq.com",
    )
    gateway = WeChatPayGateway(
        app_id="wx-app-id",
        merchant_id="1900000001",
        certificate_serial="MERCHANT-SERIAL",
        private_key_path=private_path,
        public_key_id="WECHAT-PUBLIC-ID",
        public_key_path=public_path,
        api_v3_key="v" * 32,
        payment_notify_url="https://api.example/callbacks/wechat/pay",
        refund_notify_url="https://api.example/callbacks/wechat/refund",
        client=client,
    )
    return gateway, key


def test_wechat_login_exchanges_only_with_the_official_endpoint() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "api.weixin.qq.com"
        assert request.url.path == "/sns/jscode2session"
        assert request.url.params["js_code"] == "short-code"
        return httpx.Response(200, json={"openid": "openid-1"})

    provider = WeChatAuthProvider(
        app_id="wx-app-id",
        app_secret="secret",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    assert provider.exchange_code("short-code").openid == "openid-1"


def test_jsapi_prepay_is_signed_and_returns_wx_client_fields(tmp_path: Path) -> None:
    key_holder = {}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v3/pay/transactions/jsapi"
        assert request.headers["Authorization"].startswith(
            "WECHATPAY2-SHA256-RSA2048 "
        )
        payload = json.loads(request.content)
        assert payload["payer"]["openid"] == "openid-1"
        assert payload["amount"] == {"total": 500, "currency": "CNY"}
        body = b'{"prepay_id":"wx-prepay-1"}'
        return httpx.Response(
            200,
            content=body,
            headers=_signed_headers(key_holder["key"], body),
        )

    gateway, key = _gateway(tmp_path, handler)
    key_holder["key"] = key

    result = gateway.create_prepay(
        PrepayRequest(
            merchant_order_id="merchant-order-1",
            amount_cents=500,
            description="数学竞赛答卷批改",
            payer_openid="openid-1",
        )
    )

    assert result.prepay_id == "wx-prepay-1"
    assert set(result.client_payload) == {
        "timeStamp",
        "nonceStr",
        "package",
        "signType",
        "paySign",
    }
    assert result.client_payload["package"] == "prepay_id=wx-prepay-1"


def test_callback_signature_and_aes_gcm_are_verified(tmp_path: Path) -> None:
    gateway, key = _gateway(
        tmp_path,
        lambda request: httpx.Response(500),
    )
    resource = {
        "trade_state": "SUCCESS",
        "out_trade_no": "merchant-order-1",
        "transaction_id": "wx-transaction-1",
        "amount": {"total": 500},
    }
    nonce = b"123456789012"
    associated = b"transaction"
    ciphertext = AESGCM(b"v" * 32).encrypt(
        nonce,
        json.dumps(resource, separators=(",", ":")).encode(),
        associated,
    )
    envelope = {
        "event_type": "TRANSACTION.SUCCESS",
        "resource": {
            "nonce": nonce.decode(),
            "associated_data": associated.decode(),
            "ciphertext": base64.b64encode(ciphertext).decode(),
        },
    }
    body = json.dumps(envelope, separators=(",", ":")).encode()

    parsed = gateway.parse_notification(_signed_headers(key, body), body)

    assert parsed["event_type"] == "TRANSACTION.SUCCESS"
    assert parsed["resource"] == resource

    bad_headers = _signed_headers(key, body)
    bad_headers["Wechatpay-Signature"] = base64.b64encode(b"bad").decode()
    with pytest.raises(WeChatPayNotificationError):
        gateway.parse_notification(bad_headers, body)


def test_production_refuses_missing_wechat_credentials(tmp_path: Path) -> None:
    settings = ServerSettings(
        environment=Environment.PRODUCTION,
        database_url="mysql+pymysql://grader:pw@127.0.0.1/grader",
        data_dir=tmp_path,
        session_secret="s" * 32,
        worker_shared_key="w" * 32,
        admin_shared_key="a" * 32,
    )

    with pytest.raises(ValueError, match="production WeChat configuration"):
        settings.require_wechat_production_settings()
