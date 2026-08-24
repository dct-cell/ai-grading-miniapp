from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from secrets import token_hex

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from server.adapters.payments import PaymentGateway, PrepayRequest
from server.db_locking import lock_row
from server.domain.states import JobState, OrderState, require_order_transition
from server.models import (
    FileObject,
    GradingJob,
    GradingRound,
    Order,
    Payment,
    QuoteSession,
    User,
)
from server.services.files import promote_to_retained


class PaymentState:
    PENDING = "pending"
    SUCCEEDED = "succeeded"


class QuoteNotAvailable(LookupError):
    """The quote does not exist or is not owned by the caller."""


class QuoteNotPayable(ValueError):
    """The quote exists but can no longer be turned into an order."""


class PaymentNotFound(LookupError):
    """No payment intent matches the supplied identifier."""


class CallbackRejected(ValueError):
    """The callback failed verification and must not create an order."""


@dataclass(frozen=True)
class PrepayIntent:
    payment: Payment
    client_payload: dict[str, str]


def _require_payable(quote: QuoteSession) -> None:
    if quote.consumed_at is not None:
        raise QuoteNotPayable("该报价已经完成支付。")
    if quote.expires_at <= datetime.now(timezone.utc):
        raise QuoteNotPayable("报价已过期，请重新上传。")


def create_prepay(
    *,
    session: Session,
    gateway: PaymentGateway,
    owner_user_id: str,
    quote_id: str,
) -> PrepayIntent:
    quote = session.scalar(
        select(QuoteSession).where(
            QuoteSession.id == quote_id,
            QuoteSession.owner_user_id == owner_user_id,
        )
    )
    if quote is None:
        raise QuoteNotAvailable(quote_id)
    _require_payable(quote)
    payer_openid = session.scalar(
        select(User.openid).where(User.id == owner_user_id)
    )
    if not payer_openid:
        raise QuoteNotPayable("用户微信身份不完整。")

    merchant_order_id = f"{quote.id[:8]}-{token_hex(8)}"
    result = gateway.create_prepay(
        PrepayRequest(
            merchant_order_id=merchant_order_id,
            amount_cents=quote.quoted_amount_cents,
            description="数学竞赛答卷批改",
            payer_openid=payer_openid,
        )
    )
    payment = Payment(
        quote_session_id=quote.id,
        merchant_order_id=merchant_order_id,
        prepay_id=result.prepay_id,
        amount_cents=quote.quoted_amount_cents,
        state=PaymentState.PENDING,
    )
    session.add(payment)
    session.commit()
    return PrepayIntent(payment=payment, client_payload=dict(result.client_payload))


def _existing_order(session: Session, payment: Payment) -> Order | None:
    return session.scalar(
        select(Order).where(Order.quote_session_id == payment.quote_session_id)
    )


def confirm_payment(
    *,
    session: Session,
    payment_id: str,
    external_transaction_id: str,
    paid_amount_cents: int,
) -> Order:
    """Create the paid order exactly once for a verified payment notification.

    Raises CallbackRejected when verification fails; the caller must not
    create any order in that case.
    """
    payment = lock_row(session, Payment, payment_id)
    if payment is None:
        raise PaymentNotFound(payment_id)

    if payment.state == PaymentState.SUCCEEDED:
        order = _existing_order(session, payment)
        if (
            order is None
            or payment.external_transaction_id != external_transaction_id
            or payment.amount_cents != paid_amount_cents
        ):
            raise CallbackRejected("支付回调与已有支付记录不一致。")
        return order

    quote = lock_row(session, QuoteSession, payment.quote_session_id)
    if quote is None:
        raise CallbackRejected("报价不存在。")
    if quote.consumed_at is not None:
        raise CallbackRejected("该报价已由其他交易完成支付。")
    if quote.expires_at <= datetime.now(timezone.utc):
        raise CallbackRejected("报价已过期，请重新上传。")
    if paid_amount_cents != payment.amount_cents:
        raise CallbackRejected("支付金额与报价不一致。")
    if paid_amount_cents != quote.quoted_amount_cents:
        raise CallbackRejected("支付金额与报价不一致。")

    now = datetime.now(timezone.utc)
    payment.external_transaction_id = external_transaction_id
    payment.state = PaymentState.SUCCEEDED
    session.add(payment)

    file_records = []
    for file_id in (quote.source_file_id, quote.reference_file_id):
        if file_id is None:
            continue
        record = session.get(FileObject, file_id)
        if record is None:
            raise CallbackRejected("订单文件缺失。")
        file_records.append(record)

    require_order_transition(OrderState.AWAITING_PAYMENT, OrderState.V1_QUEUED)
    order = Order(
        quote_session_id=quote.id,
        state=OrderState.V1_QUEUED,
        paid_amount_cents=paid_amount_cents,
        current_round_number=1,
    )
    session.add(order)

    # Claim the quote first so that a concurrent callback loses on the unique
    # constraint before any further work happens.
    try:
        session.flush()
    except IntegrityError as error:
        session.rollback()
        raise CallbackRejected("支付回调重复或冲突。") from error

    session.add(
        GradingRound(
            order_id=order.id,
            round_number=1,
            service_tier=quote.service_tier,
            grading_standard=quote.grading_standard,
            league_scope=quote.league_scope,
            note=quote.note,
        )
    )
    session.add(
        GradingJob(
            order_id=order.id,
            round_number=1,
            state=JobState.QUEUED,
            queued_at=now,
            lease_version=0,
            attempt_count=0,
        )
    )
    quote.consumed_at = now
    session.add(quote)

    for record in file_records:
        promote_to_retained(record)
        session.add(record)

    try:
        session.commit()
    except SQLAlchemyError as error:
        session.rollback()
        raise CallbackRejected("支付回调重复或冲突。") from error
    return order


def confirm_by_transaction(
    *,
    session: Session,
    prepay_id: str,
    external_transaction_id: str,
) -> Order:
    payment_id = session.scalar(
        select(Payment.id).where(Payment.prepay_id == prepay_id)
    )
    if payment_id is None:
        raise PaymentNotFound(prepay_id)
    payment_amount = session.scalar(
        select(Payment.amount_cents).where(Payment.id == payment_id)
    )
    return confirm_payment(
        session=session,
        payment_id=payment_id,
        external_transaction_id=external_transaction_id,
        paid_amount_cents=payment_amount,
    )


def confirm_by_merchant_order(
    *,
    session: Session,
    merchant_order_id: str,
    external_transaction_id: str,
    paid_amount_cents: int,
) -> Order:
    payment_id = session.scalar(
        select(Payment.id).where(Payment.merchant_order_id == merchant_order_id)
    )
    if payment_id is None:
        raise PaymentNotFound(merchant_order_id)
    return confirm_payment(
        session=session,
        payment_id=payment_id,
        external_transaction_id=external_transaction_id,
        paid_amount_cents=paid_amount_cents,
    )


def get_owned_payment(
    session: Session,
    owner_user_id: str,
    payment_id: str,
) -> Payment | None:
    return session.scalar(
        select(Payment)
        .join(QuoteSession, QuoteSession.id == Payment.quote_session_id)
        .where(
            Payment.id == payment_id,
            QuoteSession.owner_user_id == owner_user_id,
        )
    )
