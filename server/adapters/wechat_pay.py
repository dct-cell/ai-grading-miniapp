"""Minimal direct-merchant WeChat Pay API v3 adapter.

The adapter signs every request, verifies every signed response/notification,
and decrypts callback resources before business services see their contents.
Secrets are loaded from protected files/settings and never enter logs.
"""
from __future__ import annotations

import base64
import json
import secrets
import time
from pathlib import Path
from typing import Mapping

import httpx
from cryptography import x509
from cryptography.exceptions import InvalidSignature, InvalidTag
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from server.adapters.payments import (
    PaymentGatewayUnavailable,
    PrepayRequest,
    PrepayResult,
    RefundFailed,
    RefundRequest,
    RefundResult,
)


class WeChatPayNotificationError(ValueError):
    """A callback failed signature, freshness, or resource validation."""


class WeChatPayGateway:
    """Ordinary direct-merchant JSAPI/refund implementation."""

    def __init__(
        self,
        *,
        app_id: str,
        merchant_id: str,
        certificate_serial: str,
        private_key_path: Path,
        public_key_id: str,
        public_key_path: Path,
        api_v3_key: str,
        payment_notify_url: str,
        refund_notify_url: str,
        client: httpx.Client | None = None,
    ) -> None:
        if len(api_v3_key.encode("utf-8")) != 32:
            raise ValueError("WeChat Pay API v3 key must be exactly 32 bytes")
        self._app_id = app_id
        self._merchant_id = merchant_id
        self._certificate_serial = certificate_serial
        self._public_key_id = public_key_id
        self._api_v3_key = api_v3_key.encode("utf-8")
        self._payment_notify_url = payment_notify_url
        self._refund_notify_url = refund_notify_url
        self._private_key = self._load_private_key(private_key_path)
        self._public_key = self._load_public_key(public_key_path)
        self._client = client or httpx.Client(
            base_url="https://api.mch.weixin.qq.com",
            timeout=httpx.Timeout(30.0, connect=10.0),
        )
        self._owns_client = client is None

    @staticmethod
    def _load_private_key(path: Path) -> rsa.RSAPrivateKey:
        try:
            key = serialization.load_pem_private_key(path.read_bytes(), password=None)
        except (OSError, ValueError, TypeError) as error:
            raise ValueError("WeChat Pay merchant private key is invalid") from error
        if not isinstance(key, rsa.RSAPrivateKey):
            raise ValueError("WeChat Pay merchant key must be RSA")
        return key

    @staticmethod
    def _load_public_key(path: Path) -> rsa.RSAPublicKey:
        try:
            payload = path.read_bytes()
            try:
                key = serialization.load_pem_public_key(payload)
            except ValueError:
                key = x509.load_pem_x509_certificate(payload).public_key()
        except (OSError, ValueError, TypeError) as error:
            raise ValueError("WeChat Pay public key/certificate is invalid") from error
        if not isinstance(key, rsa.RSAPublicKey):
            raise ValueError("WeChat Pay verification key must be RSA")
        return key

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def _sign(self, message: bytes) -> str:
        signature = self._private_key.sign(
            message,
            padding.PKCS1v15(),
            hashes.SHA256(),
        )
        return base64.b64encode(signature).decode("ascii")

    def _authorization(self, method: str, path: str, body: str) -> str:
        timestamp = str(int(time.time()))
        nonce = secrets.token_hex(16)
        message = f"{method}\n{path}\n{timestamp}\n{nonce}\n{body}\n".encode()
        signature = self._sign(message)
        return (
            'WECHATPAY2-SHA256-RSA2048 '
            f'mchid="{self._merchant_id}",'
            f'nonce_str="{nonce}",'
            f'signature="{signature}",'
            f'timestamp="{timestamp}",'
            f'serial_no="{self._certificate_serial}"'
        )

    def _verify_signed_body(
        self,
        *,
        timestamp: str,
        nonce: str,
        signature: str,
        serial: str,
        body: bytes,
        require_fresh: bool,
    ) -> None:
        if serial != self._public_key_id:
            raise WeChatPayNotificationError("unexpected WeChat Pay key id")
        try:
            timestamp_int = int(timestamp)
        except ValueError as error:
            raise WeChatPayNotificationError("invalid WeChat Pay timestamp") from error
        if require_fresh and abs(int(time.time()) - timestamp_int) > 300:
            raise WeChatPayNotificationError("stale WeChat Pay notification")
        message = timestamp.encode() + b"\n" + nonce.encode() + b"\n" + body + b"\n"
        try:
            self._public_key.verify(
                base64.b64decode(signature, validate=True),
                message,
                padding.PKCS1v15(),
                hashes.SHA256(),
            )
        except (InvalidSignature, ValueError) as error:
            raise WeChatPayNotificationError("invalid WeChat Pay signature") from error

    def _verify_response(self, response: httpx.Response) -> None:
        headers = response.headers
        required = {
            "timestamp": headers.get("Wechatpay-Timestamp"),
            "nonce": headers.get("Wechatpay-Nonce"),
            "signature": headers.get("Wechatpay-Signature"),
            "serial": headers.get("Wechatpay-Serial"),
        }
        if any(value is None for value in required.values()):
            raise PaymentGatewayUnavailable("unsigned WeChat Pay response")
        try:
            self._verify_signed_body(
                timestamp=str(required["timestamp"]),
                nonce=str(required["nonce"]),
                signature=str(required["signature"]),
                serial=str(required["serial"]),
                body=response.content,
                require_fresh=False,
            )
        except WeChatPayNotificationError as error:
            raise PaymentGatewayUnavailable("invalid WeChat Pay response") from error

    def _request_json(self, method: str, path: str, payload: dict | None = None) -> dict:
        body = (
            ""
            if payload is None
            else json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        )
        headers = {
            "Accept": "application/json",
            "Authorization": self._authorization(method, path, body),
        }
        if payload is not None:
            headers["Content-Type"] = "application/json"
        try:
            response = self._client.request(
                method,
                path,
                content=body.encode("utf-8") if payload is not None else None,
                headers=headers,
            )
            self._verify_response(response)
            response.raise_for_status()
            result = response.json()
        except (httpx.HTTPError, ValueError, PaymentGatewayUnavailable) as error:
            if isinstance(error, PaymentGatewayUnavailable):
                raise
            raise PaymentGatewayUnavailable("WeChat Pay request failed") from error
        if not isinstance(result, dict):
            raise PaymentGatewayUnavailable("invalid WeChat Pay response body")
        return result

    def create_prepay(self, request: PrepayRequest) -> PrepayResult:
        result = self._request_json(
            "POST",
            "/v3/pay/transactions/jsapi",
            {
                "appid": self._app_id,
                "mchid": self._merchant_id,
                "description": request.description,
                "out_trade_no": request.merchant_order_id,
                "notify_url": self._payment_notify_url,
                "amount": {"total": request.amount_cents, "currency": "CNY"},
                "payer": {"openid": request.payer_openid},
            },
        )
        prepay_id = result.get("prepay_id")
        if not isinstance(prepay_id, str) or not prepay_id:
            raise PaymentGatewayUnavailable("WeChat Pay omitted prepay_id")
        timestamp = str(int(time.time()))
        nonce = secrets.token_hex(16)
        package = f"prepay_id={prepay_id}"
        pay_sign = self._sign(
            f"{self._app_id}\n{timestamp}\n{nonce}\n{package}\n".encode()
        )
        return PrepayResult(
            prepay_id=prepay_id,
            client_payload={
                "timeStamp": timestamp,
                "nonceStr": nonce,
                "package": package,
                "signType": "RSA",
                "paySign": pay_sign,
            },
        )

    def query_order(self, merchant_order_id: str) -> dict:
        return self._request_json(
            "GET",
            f"/v3/pay/transactions/out-trade-no/{merchant_order_id}?mchid={self._merchant_id}",
        )

    def query_refund(self, external_refund_id: str) -> dict:
        return self._request_json(
            "GET",
            f"/v3/refund/domestic/refunds/{external_refund_id}",
        )

    def refund(self, request: RefundRequest) -> RefundResult:
        try:
            result = self._request_json(
                "POST",
                "/v3/refund/domestic/refunds",
                {
                    "transaction_id": request.external_transaction_id,
                    "out_refund_no": request.external_refund_id,
                    "reason": request.reason[:80],
                    "notify_url": self._refund_notify_url,
                    "amount": {
                        "refund": request.amount_cents,
                        "total": request.amount_cents,
                        "currency": "CNY",
                    },
                },
            )
        except PaymentGatewayUnavailable as error:
            raise RefundFailed("WeChat Pay refund request failed") from error
        status = result.get("status")
        return RefundResult(
            external_refund_id=request.external_refund_id,
            succeeded=status == "SUCCESS",
            failure_code=None if status == "SUCCESS" else str(status or "UNKNOWN"),
        )

    def parse_notification(
        self,
        headers: Mapping[str, str],
        body: bytes,
    ) -> dict:
        normalised = {key.lower(): value for key, value in headers.items()}
        try:
            self._verify_signed_body(
                timestamp=normalised["wechatpay-timestamp"],
                nonce=normalised["wechatpay-nonce"],
                signature=normalised["wechatpay-signature"],
                serial=normalised["wechatpay-serial"],
                body=body,
                require_fresh=True,
            )
            envelope = json.loads(body)
            resource = envelope["resource"]
            plaintext = AESGCM(self._api_v3_key).decrypt(
                resource["nonce"].encode(),
                base64.b64decode(resource["ciphertext"], validate=True),
                resource.get("associated_data", "").encode(),
            )
            payload = json.loads(plaintext)
        except (
            KeyError,
            TypeError,
            ValueError,
            InvalidTag,
            json.JSONDecodeError,
        ) as error:
            raise WeChatPayNotificationError("invalid WeChat Pay resource") from error
        if not isinstance(payload, dict):
            raise WeChatPayNotificationError("invalid WeChat Pay resource")
        return {"event_type": envelope.get("event_type"), "resource": payload}
