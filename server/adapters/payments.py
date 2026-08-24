from dataclasses import dataclass, field
from typing import Protocol


@dataclass(frozen=True)
class PrepayRequest:
    merchant_order_id: str
    amount_cents: int
    description: str
    payer_openid: str = ""


@dataclass(frozen=True)
class PrepayResult:
    prepay_id: str
    client_payload: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class RefundRequest:
    """One attempt at returning money for a specific payment.

    ``external_refund_id`` is ours to choose and is stable across retries: the
    provider deduplicates on it, so re-sending the same id must never move
    money twice. It is generated once when the Refund row is created and is
    never regenerated.
    """

    external_refund_id: str
    external_transaction_id: str
    amount_cents: int
    reason: str


@dataclass(frozen=True)
class RefundResult:
    external_refund_id: str
    succeeded: bool
    #: Provider-side detail for the audit trail. Never surfaced to the user.
    failure_code: str | None = None


class RefundFailed(Exception):
    """The gateway could not be reached or refused the request.

    Raised for transport-level problems, as distinct from a well-formed refusal
    reported through``RefundResult.succeeded``. Either way the caller must
    leave the refund retryable and must not mark the order refunded.
    """


class PaymentGatewayUnavailable(RuntimeError):
    """Prepay could not reach or authenticate with the provider."""


class PaymentGateway(Protocol):
    def create_prepay(self, request: PrepayRequest) -> PrepayResult: ...

    def refund(self, request: RefundRequest) -> RefundResult: ...


class FakePaymentGateway:
    """Deterministic gateway used until WeChat Pay credentials exist.

    Refunds succeed by default. Tests can queue deterministic failures with
    ``fail_once``/``fail_times`` to drive the retry path, and read``calls`` to
    prove a retry reuses one external_refund_id rather than minting a new one.
    """

    def __init__(self) -> None:
        self.calls: list[RefundRequest] = []
        self._pending_failures = 0
        self._raise_instead = False

    def create_prepay(self, request: PrepayRequest) -> PrepayResult:
        prepay_id = f"fake-{request.merchant_order_id}"
        return PrepayResult(prepay_id, {"fake_prepay_id": prepay_id})

    def fail_once(self, *, raising: bool = False) -> None:
        self.fail_times(1, raising=raising)

    def fail_times(self, count: int, *, raising: bool = False) -> None:
        self._pending_failures = count
        self._raise_instead = raising

    @property
    def external_ids(self) -> list[str]:
        return [call.external_refund_id for call in self.calls]

    def refund(self, request: RefundRequest) -> RefundResult:
        self.calls.append(request)
        if self._pending_failures > 0:
            self._pending_failures -= 1
            if self._raise_instead:
                raise RefundFailed("fake gateway is unreachable")
            return RefundResult(
                external_refund_id=request.external_refund_id,
                succeeded=False,
                failure_code="FAKE_DECLINED",
            )
        return RefundResult(
            external_refund_id=request.external_refund_id,
            succeeded=True,
        )
