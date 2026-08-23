"""Operational settings, price versioning, user and funds reads.

Settings resolve database-first, environment-second. An absent key means "never
edited", so a freshly-migrated deployment behaves exactly as it did before this
table existed, and an operator can still bootstrap everything from ``.env``.

Only names in ``EDITABLE_SETTINGS`` are writable, each with its own bounds. That
allow-list is the reason this cannot become a way to write ``session_secret``
into the database, and the bounds are the same ones ``ServerSettings`` enforces —
a value that would be rejected at startup must be rejected here too.

Repricing never edits a quote. ``price_rules`` is versioned: the live rule is
retired and a new one inserted, so ``QuoteSession.quoted_amount_cents`` keeps the
amount the user actually agreed to.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from server.config import ServerSettings
from server.domain.service_tiers import (
    ANNOTATED_REVIEW,
    SUMMARY_REPORT,
    ServiceTier,
    require_service_tier,
)
from server.domain.refund_policy import (
    MAX_AUTOMATIC_AMOUNT_CENTS,
    MAX_AUTOMATIC_MONTHLY_COUNT,
)
from server.models import (
    AuditLog,
    OperationalSetting,
    Order,
    Payment,
    PriceRule,
    QuoteSession,
    Refund,
    User,
)
from server.services.orders import MINUTES_PER_PAGE
from server.services.refunds import RefundSource, RefundState


@dataclass(frozen=True)
class SettingSpec:
    """Bounds for one editable value, mirroring the ServerSettings field."""

    minimum: int
    maximum: int
    #: Reads the effective default from configuration when unset.
    default: str


#: The complete set of names an admin may write, with their bounds. Anything not
#: listed here is rejected, which is what stops a secret being smuggled in.
EDITABLE_SETTINGS: dict[str, SettingSpec] = {
    "max_pdf_pages": SettingSpec(minimum=1, maximum=200, default="max_pdf_pages"),
    "max_pdf_bytes": SettingSpec(
        minimum=1024, maximum=200 * 1024 * 1024, default="max_pdf_bytes"
    ),
    "quote_ttl_seconds": SettingSpec(
        minimum=60, maximum=30 * 86400, default="quote_ttl_seconds"
    ),
    "acceptance_ttl_seconds": SettingSpec(
        minimum=60, maximum=30 * 86400, default="acceptance_ttl_seconds"
    ),
    "minutes_per_page": SettingSpec(minimum=1, maximum=600, default=""),
    "automatic_refund_max_amount_cents": SettingSpec(
        minimum=1, maximum=100_000_000, default=""
    ),
    "automatic_refund_max_monthly_count": SettingSpec(
        minimum=1, maximum=1000, default=""
    ),
}

#: Defaults for values that have no ServerSettings field, so they come from the
#: constants the rest of the code already uses.
_CODE_DEFAULTS = {
    "minutes_per_page": MINUTES_PER_PAGE,
    "automatic_refund_max_amount_cents": MAX_AUTOMATIC_AMOUNT_CENTS,
    "automatic_refund_max_monthly_count": MAX_AUTOMATIC_MONTHLY_COUNT,
}


class UnknownSetting(KeyError):
    """The name is not on the editable allow-list."""


class SettingOutOfRange(ValueError):
    """The value is outside the bounds this setting accepts."""


def resolve_settings(
    session: Session,
    settings: ServerSettings,
) -> dict[str, int]:
    """Return every operational value in force, database first."""
    stored = {
        row.name: row.value
        for row in session.scalars(select(OperationalSetting)).all()
    }
    resolved: dict[str, int] = {}
    for name, spec in EDITABLE_SETTINGS.items():
        if name in stored:
            resolved[name] = int(stored[name])
            continue
        if spec.default:
            resolved[name] = int(getattr(settings, spec.default))
        else:
            resolved[name] = int(_CODE_DEFAULTS[name])
    return resolved


def resolve_setting(
    session: Session,
    settings: ServerSettings,
    name: str,
) -> int:
    return resolve_settings(session, settings)[name]


def update_settings(
    session: Session,
    settings: ServerSettings,
    *,
    changes: dict[str, int],
    admin_id: str,
) -> dict[str, int]:
    """Write operational values, validating each against its own bounds."""
    for name, value in changes.items():
        if name not in EDITABLE_SETTINGS:
            raise UnknownSetting(name)
        spec = EDITABLE_SETTINGS[name]
        if not isinstance(value, int) or isinstance(value, bool):
            raise SettingOutOfRange(name)
        if not spec.minimum <= value <= spec.maximum:
            raise SettingOutOfRange(name)

    for name, value in changes.items():
        row = session.scalar(
            select(OperationalSetting).where(OperationalSetting.name == name)
        )
        if row is None:
            session.add(OperationalSetting(name=name, value=str(value)))
        else:
            row.value = str(value)

    session.add(
        AuditLog(
            actor_type="admin",
            actor_id=admin_id,
            action="settings.update",
            target_type="settings",
            target_id="operational",
            # Only names and numeric values, all from the allow-list, so no
            # secret can reach an audit row through here.
            details={"changes": changes},
        )
    )
    session.commit()
    return resolve_settings(session, settings)


def reprice(
    session: Session,
    *,
    service_tier: str,
    cents_per_page: int,
    admin_id: str,
) -> PriceRule:
    """Retire the live price rule and insert a new version.

    Existing quotes reference their own ``price_rule_id`` and carry a snapshot in
    ``quoted_amount_cents``, so neither is touched: a user is charged what they
    were shown, whatever happens to the price afterwards.
    """
    resolved_tier = require_service_tier(service_tier)
    now = datetime.now(timezone.utc)
    live = session.scalars(
        select(PriceRule).where(
            PriceRule.service_tier == resolved_tier,
            PriceRule.retired_at.is_(None),
        )
    ).all()
    for rule in live:
        rule.retired_at = now

    created = PriceRule(
        service_tier=resolved_tier,
        cents_per_page=cents_per_page,
        effective_from=now,
    )
    session.add(created)
    session.flush()
    session.add(
        AuditLog(
            actor_type="admin",
            actor_id=admin_id,
            action="settings.price_rule",
            target_type="price_rule",
            target_id=created.id,
            details={
                "service_tier": resolved_tier,
                "cents_per_page": cents_per_page,
            },
        )
    )
    session.commit()
    return created


def active_cents_per_page(
    session: Session,
    settings: ServerSettings,
    service_tier: str = ANNOTATED_REVIEW,
) -> int:
    """The tier's live price, falling back to configuration before repricing."""
    resolved_tier: ServiceTier = require_service_tier(service_tier)
    rule = session.scalar(
        select(PriceRule)
        .where(
            PriceRule.service_tier == resolved_tier,
            PriceRule.retired_at.is_(None),
        )
        .order_by(PriceRule.effective_from.desc(), PriceRule.id)
        .limit(1)
    )
    if rule is None:
        if resolved_tier == SUMMARY_REPORT:
            return settings.summary_price_cents_per_page
        return settings.annotated_price_cents_per_page
    return rule.cents_per_page


@dataclass(frozen=True)
class UserDetail:
    public_id: str
    created_at: datetime
    order_count: int
    lifetime_paid_cents: int
    lifetime_user_refunded_cents: int
    technical_refunded_cents: int
    monthly_user_refund_count: int
    lifetime_refund_ratio: float


def load_user_detail(
    session: Session,
    public_id: str,
    *,
    now: datetime | None = None,
) -> UserDetail | None:
    """Summarise one user. ``openid`` is deliberately never returned."""
    user = session.scalar(select(User).where(User.public_id == public_id))
    if user is None:
        return None

    moment = now or datetime.now(timezone.utc)
    month_start = moment.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    paid = session.scalar(
        select(func.coalesce(func.sum(Payment.amount_cents), 0))
        .join(QuoteSession, QuoteSession.id == Payment.quote_session_id)
        .where(
            QuoteSession.owner_user_id == user.id,
            Payment.state == "succeeded",
        )
    )

    def _refunded(source: str) -> int:
        return int(
            session.scalar(
                select(func.coalesce(func.sum(Refund.amount_cents), 0))
                .select_from(Refund)
                .join(Payment, Payment.id == Refund.payment_id)
                .join(QuoteSession, QuoteSession.id == Payment.quote_session_id)
                .where(
                    QuoteSession.owner_user_id == user.id,
                    Refund.source == source,
                    Refund.state == RefundState.REFUNDED,
                )
            )
            or 0
        )

    user_refunded = _refunded(RefundSource.USER)
    technical_refunded = _refunded(RefundSource.ADMIN_TECHNICAL)

    monthly = session.scalar(
        select(func.count())
        .select_from(Refund)
        .join(Payment, Payment.id == Refund.payment_id)
        .join(QuoteSession, QuoteSession.id == Payment.quote_session_id)
        .where(
            QuoteSession.owner_user_id == user.id,
            Refund.source == RefundSource.USER,
            Refund.created_at >= month_start,
        )
    )

    order_count = session.scalar(
        select(func.count())
        .select_from(Order)
        .join(QuoteSession, QuoteSession.id == Order.quote_session_id)
        .where(QuoteSession.owner_user_id == user.id)
    )

    paid_cents = int(paid or 0)
    # Technical refunds are excluded from the ratio: our own failures must not
    # count against the user's standing.
    ratio = (
        float(Decimal(user_refunded) / Decimal(paid_cents)) if paid_cents > 0 else 0.0
    )

    return UserDetail(
        public_id=user.public_id,
        created_at=user.created_at,
        order_count=int(order_count or 0),
        lifetime_paid_cents=paid_cents,
        lifetime_user_refunded_cents=user_refunded,
        technical_refunded_cents=technical_refunded,
        monthly_user_refund_count=int(monthly or 0),
        lifetime_refund_ratio=round(ratio, 4),
    )


@dataclass(frozen=True)
class FundsSummary:
    payments: dict[str, int]
    refunds: dict[str, int]
    reconciliation: dict[str, object]


def load_funds(session: Session) -> FundsSummary:
    """Totals we can prove from our own ledger, and nothing beyond it."""
    succeeded = session.scalar(
        select(func.coalesce(func.sum(Payment.amount_cents), 0)).where(
            Payment.state == "succeeded"
        )
    )
    payment_count = session.scalar(
        select(func.count()).select_from(Payment).where(Payment.state == "succeeded")
    )
    refunded = session.scalar(
        select(func.coalesce(func.sum(Refund.amount_cents), 0)).where(
            Refund.state == RefundState.REFUNDED
        )
    )
    technical = session.scalar(
        select(func.coalesce(func.sum(Refund.amount_cents), 0)).where(
            Refund.state == RefundState.REFUNDED,
            Refund.source == RefundSource.ADMIN_TECHNICAL,
        )
    )
    failed = session.scalar(
        select(func.count())
        .select_from(Refund)
        .where(Refund.state == RefundState.REFUND_FAILED)
    )
    pending = session.scalar(
        select(func.count())
        .select_from(Refund)
        .where(Refund.state == RefundState.PENDING)
    )

    return FundsSummary(
        payments={
            "succeeded_cents": int(succeeded or 0),
            "succeeded_count": int(payment_count or 0),
        },
        refunds={
            "refunded_cents": int(refunded or 0),
            "technical_refunded_cents": int(technical or 0),
            "failed_count": int(failed or 0),
            "pending_count": int(pending or 0),
        },
        reconciliation={
            # We know what we asked the gateway to do. We do not know what the
            # bank settled, and claiming otherwise would misstate the accounts.
            # Phase 09 imports statements; until then this stays honest.
            "source": "none",
            "settled_to_bank_cents": None,
        },
    )


def list_audit(
    session: Session,
    *,
    actor_id: str | None = None,
    action: str | None = None,
    target_type: str | None = None,
    target_id: str | None = None,
    created_from: datetime | None = None,
    created_to: datetime | None = None,
    page_size: int = 50,
) -> tuple[AuditLog, ...]:
    """Read the append-only log, newest first.

    Read-only by construction: this module exposes no update or delete for
    ``AuditLog``, because the log is the evidence that an action happened.
    """
    statement = select(AuditLog).order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
    if actor_id:
        statement = statement.where(AuditLog.actor_id == actor_id)
    if action:
        statement = statement.where(AuditLog.action == action)
    if target_type:
        statement = statement.where(AuditLog.target_type == target_type)
    if target_id:
        statement = statement.where(AuditLog.target_id == target_id)
    if created_from is not None:
        statement = statement.where(AuditLog.created_at >= created_from)
    if created_to is not None:
        statement = statement.where(AuditLog.created_at <= created_to)
    return tuple(session.scalars(statement.limit(page_size)).all())


__all__ = [
    "EDITABLE_SETTINGS",
    "FundsSummary",
    "SettingOutOfRange",
    "UnknownSetting",
    "UserDetail",
    "active_cents_per_page",
    "list_audit",
    "load_funds",
    "load_user_detail",
    "reprice",
    "resolve_setting",
    "resolve_settings",
    "update_settings",
]
