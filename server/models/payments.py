from uuid import uuid4

from sqlalchemy import CheckConstraint, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from server.models.base import Base, TimestampMixin


def _uuid_string() -> str:
    return str(uuid4())


class Payment(TimestampMixin, Base):
    __tablename__ = "payments"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=_uuid_string
    )
    quote_session_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("quote_sessions.id"), nullable=False
    )
    merchant_order_id: Mapped[str] = mapped_column(
        String(64), unique=True, nullable=False
    )
    prepay_id: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    external_transaction_id: Mapped[str | None] = mapped_column(
        String(64), unique=True, nullable=True
    )
    amount_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False)


class Refund(TimestampMixin, Base):
    __tablename__ = "refunds"
    __table_args__ = (
        CheckConstraint(
            "source IN ('user', 'admin_technical')",
            name="ck_refunds_source",
        ),
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=_uuid_string
    )
    payment_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("payments.id"), nullable=False
    )
    external_refund_id: Mapped[str] = mapped_column(
        String(64), unique=True, nullable=False
    )
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False)
    amount_cents: Mapped[int] = mapped_column(Integer, nullable=False)
