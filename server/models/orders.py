from datetime import datetime
from uuid import uuid4

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from server.models.base import Base, TimestampMixin, UTCDateTime


def _uuid_string() -> str:
    return str(uuid4())


class FileObject(TimestampMixin, Base):
    __tablename__ = "file_objects"
    __table_args__ = (
        # One row per stored object. The staged-upload check in the result
        # service is a check-then-insert that races on MySQL, so this constraint
        # is what actually makes an upload token single-use — the same way
        # orders.quote_session_id backs the payment callback's idempotency.
        UniqueConstraint(
            "relative_path",
            name="uq_file_objects_relative_path",
        ),
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=_uuid_string
    )
    owner_user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id"), nullable=False
    )
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    relative_path: Mapped[str] = mapped_column(String(512), nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)


class PriceRule(TimestampMixin, Base):
    __tablename__ = "price_rules"
    __table_args__ = (
        CheckConstraint(
            "cents_per_page > 0",
            name="ck_price_rules_cents_per_page_positive",
        ),
        Index(
            "ix_price_rules_tier_active",
            "service_tier",
            "retired_at",
            "effective_from",
        ),
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=_uuid_string
    )
    service_tier: Mapped[str] = mapped_column(String(32), nullable=False)
    cents_per_page: Mapped[int] = mapped_column(Integer, nullable=False)
    effective_from: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    retired_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)


class QuoteSession(TimestampMixin, Base):
    __tablename__ = "quote_sessions"
    __table_args__ = (
        CheckConstraint(
            "page_count > 0",
            name="ck_quote_sessions_page_count_positive",
        ),
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=_uuid_string
    )
    owner_user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id"), nullable=False
    )
    source_file_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("file_objects.id"), nullable=False
    )
    reference_file_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("file_objects.id"), nullable=True
    )
    price_rule_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("price_rules.id"), nullable=False
    )
    service_tier: Mapped[str] = mapped_column(String(32), nullable=False)
    grading_standard: Mapped[str] = mapped_column(Text, nullable=False)
    league_scope: Mapped[str | None] = mapped_column(String(32), nullable=True)
    note: Mapped[str] = mapped_column(Text, nullable=False)
    page_count: Mapped[int] = mapped_column(Integer, nullable=False)
    quoted_amount_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)


class Order(TimestampMixin, Base):
    __tablename__ = "orders"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=_uuid_string
    )
    quote_session_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("quote_sessions.id"), unique=True, nullable=False
    )
    state: Mapped[str] = mapped_column(String(32), nullable=False)
    paid_amount_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    current_round_number: Mapped[int] = mapped_column(Integer, nullable=False)
    acceptance_deadline: Mapped[datetime | None] = mapped_column(
        UTCDateTime(), nullable=True
    )
    downloads_revoked_at: Mapped[datetime | None] = mapped_column(
        UTCDateTime(), nullable=True
    )


class GradingRound(TimestampMixin, Base):
    __tablename__ = "grading_rounds"
    __table_args__ = (
        UniqueConstraint(
            "order_id",
            "round_number",
            name="uq_grading_rounds_order_round",
        ),
        CheckConstraint(
            "round_number IN (1, 2)",
            name="ck_grading_rounds_round_number",
        ),
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=_uuid_string
    )
    order_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("orders.id"), nullable=False
    )
    round_number: Mapped[int] = mapped_column(Integer, nullable=False)
    service_tier: Mapped[str] = mapped_column(String(32), nullable=False)
    grading_standard: Mapped[str] = mapped_column(Text, nullable=False)
    league_scope: Mapped[str | None] = mapped_column(String(32), nullable=True)
    note: Mapped[str] = mapped_column(Text, nullable=False)
    result_json_file_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("file_objects.id"), nullable=True
    )
    result_pdf_file_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("file_objects.id"), nullable=True
    )
    delivered_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)


class Appeal(TimestampMixin, Base):
    __tablename__ = "appeals"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=_uuid_string
    )
    order_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("orders.id"), unique=True, nullable=False
    )
    text: Mapped[str] = mapped_column(Text, nullable=False)
