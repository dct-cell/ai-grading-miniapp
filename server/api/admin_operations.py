"""Admin users, funds, operational settings and the audit view.

The settings responses list operational knobs only. There is deliberately no
field for ``session_secret``, either shared key, or the database URL: this API
must not become a way to read a running deployment's credentials.

The audit routes are read-only. ``AuditLog`` has no update or delete endpoint
anywhere in the application, because the log is the evidence that an action
happened.
"""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel, Field

from server.api.admin_dependencies import CurrentAdmin, Settings
from server.api.dependencies import DatabaseSession
from server.domain.service_tiers import ServiceTier
from server.services.admin_operations import (
    SettingOutOfRange,
    UnknownSetting,
    active_cents_per_page,
    list_audit,
    load_funds,
    load_user_detail,
    reprice,
    resolve_settings,
    update_settings,
)


router = APIRouter(prefix="/admin/api/v1", tags=["admin-operations"])

_USER_NOT_FOUND = HTTPException(
    status_code=status.HTTP_404_NOT_FOUND,
    detail="用户不存在。",
)


class SettingsView(BaseModel):
    """Operational values only. No secret has a field here."""

    summary_cents_per_page: int
    annotated_cents_per_page: int
    max_pdf_pages: int
    max_pdf_bytes: int
    quote_ttl_seconds: int
    acceptance_ttl_seconds: int
    minutes_per_page: int
    automatic_refund_max_amount_cents: int
    automatic_refund_max_monthly_count: int


class SettingsPatch(BaseModel):
    """Every field optional; unknown fields are refused outright.

    ``extra="forbid"`` is what turns a request naming ``session_secret`` into a
    422 rather than a silently ignored field.
    """

    model_config = {"extra": "forbid"}

    max_pdf_pages: int | None = Field(default=None, ge=1, le=200)
    max_pdf_bytes: int | None = Field(default=None, ge=1024, le=200 * 1024 * 1024)
    quote_ttl_seconds: int | None = Field(default=None, ge=60, le=30 * 86400)
    acceptance_ttl_seconds: int | None = Field(default=None, ge=60, le=30 * 86400)
    minutes_per_page: int | None = Field(default=None, ge=1, le=600)
    automatic_refund_max_amount_cents: int | None = Field(
        default=None, ge=1, le=100_000_000
    )
    automatic_refund_max_monthly_count: int | None = Field(
        default=None, ge=1, le=1000
    )


class PriceRuleBody(BaseModel):
    model_config = {"extra": "forbid"}

    service_tier: ServiceTier
    cents_per_page: int = Field(ge=1, le=1_000_000)


class PriceRuleView(BaseModel):
    id: str
    service_tier: ServiceTier
    cents_per_page: int
    effective_from: datetime


class UserDetailView(BaseModel):
    """Deliberately omits openid, the external WeChat identifier."""

    public_id: str
    created_at: datetime
    order_count: int
    lifetime_paid_cents: int
    lifetime_user_refunded_cents: int
    technical_refunded_cents: int
    monthly_user_refund_count: int
    lifetime_refund_ratio: float


class FundsView(BaseModel):
    payments: dict[str, int]
    refunds: dict[str, int]
    reconciliation: dict[str, object]


class AuditEntryView(BaseModel):
    id: str
    actor_type: str
    actor_id: str
    action: str
    target_type: str
    target_id: str
    details: dict[str, object]
    created_at: datetime


class AuditListView(BaseModel):
    items: list[AuditEntryView]


def _settings_view(session, settings) -> SettingsView:
    values = resolve_settings(session, settings)
    return SettingsView(
        summary_cents_per_page=active_cents_per_page(
            session, settings, "summary_report"
        ),
        annotated_cents_per_page=active_cents_per_page(
            session, settings, "annotated_review"
        ),
        **values,
    )


@router.get("/settings", response_model=SettingsView)
def read_settings(
    admin: CurrentAdmin,
    session: DatabaseSession,
    settings: Settings,
) -> SettingsView:
    del admin
    return _settings_view(session, settings)


@router.patch("/settings", response_model=SettingsView)
def patch_settings(
    payload: SettingsPatch,
    admin: CurrentAdmin,
    session: DatabaseSession,
    settings: Settings,
) -> SettingsView:
    changes = {
        name: value
        for name, value in payload.model_dump(exclude_unset=True).items()
        if value is not None
    }
    if not changes:
        return _settings_view(session, settings)
    try:
        update_settings(session, settings, changes=changes, admin_id=admin.id)
    except (UnknownSetting, SettingOutOfRange) as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"配置项无效：{error}",
        ) from None
    return _settings_view(session, settings)


@router.post(
    "/settings/price-rules",
    response_model=PriceRuleView,
    status_code=status.HTTP_201_CREATED,
)
def create_price_rule(
    payload: PriceRuleBody,
    admin: CurrentAdmin,
    session: DatabaseSession,
) -> PriceRuleView:
    """Publish a new price version. Existing quotes keep their snapshot."""
    rule = reprice(
        session,
        service_tier=payload.service_tier,
        cents_per_page=payload.cents_per_page,
        admin_id=admin.id,
    )
    return PriceRuleView(
        id=rule.id,
        service_tier=rule.service_tier,
        cents_per_page=rule.cents_per_page,
        effective_from=rule.effective_from,
    )


@router.get("/users/{public_id}", response_model=UserDetailView)
def read_user(
    public_id: str,
    admin: CurrentAdmin,
    session: DatabaseSession,
) -> UserDetailView:
    del admin
    detail = load_user_detail(session, public_id)
    if detail is None:
        raise _USER_NOT_FOUND
    return UserDetailView(
        public_id=detail.public_id,
        created_at=detail.created_at,
        order_count=detail.order_count,
        lifetime_paid_cents=detail.lifetime_paid_cents,
        lifetime_user_refunded_cents=detail.lifetime_user_refunded_cents,
        technical_refunded_cents=detail.technical_refunded_cents,
        monthly_user_refund_count=detail.monthly_user_refund_count,
        lifetime_refund_ratio=detail.lifetime_refund_ratio,
    )


@router.get("/funds", response_model=FundsView)
def read_funds(admin: CurrentAdmin, session: DatabaseSession) -> FundsView:
    del admin
    summary = load_funds(session)
    return FundsView(
        payments=summary.payments,
        refunds=summary.refunds,
        reconciliation=summary.reconciliation,
    )


@router.get("/audit", response_model=AuditListView)
def read_audit(
    admin: CurrentAdmin,
    session: DatabaseSession,
    actor_id: str | None = None,
    action: str | None = None,
    target_type: str | None = None,
    target_id: str | None = None,
    created_from: datetime | None = None,
    created_to: datetime | None = None,
    page_size: int = Query(default=50, ge=1, le=200),
) -> AuditListView:
    del admin
    entries = list_audit(
        session,
        actor_id=actor_id,
        action=action,
        target_type=target_type,
        target_id=target_id,
        created_from=created_from,
        created_to=created_to,
        page_size=page_size,
    )
    return AuditListView(
        items=[
            AuditEntryView(
                id=entry.id,
                actor_type=entry.actor_type,
                actor_id=entry.actor_id,
                action=entry.action,
                target_type=entry.target_type,
                target_id=entry.target_id,
                details=entry.details,
                created_at=entry.created_at,
            )
            for entry in entries
        ]
    )
