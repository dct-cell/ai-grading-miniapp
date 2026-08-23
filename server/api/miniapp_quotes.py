from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, File, Form, HTTPException, UploadFile, status

from server.adapters.pdf import PdfValidationError
from server.adapters.files import FileStorageError, LocalFileStore
from server.api.dependencies import CurrentUser, DatabaseSession, Settings
from server.domain.service_tiers import (
    ANNOTATED_REVIEW,
    SERVICE_TIER_DEFINITIONS,
    SUMMARY_REPORT,
    ServiceTier,
    service_tier_label,
)
from server.models import QuoteSession
from server.schemas.quotes import (
    MAX_NOTE_CHARS,
    GradingStandard,
    QuoteView,
    ServiceTierListView,
    ServiceTierView,
)
from server.services.admin_operations import (
    active_cents_per_page,
    resolve_settings,
)
from server.services.quotes import (
    QuoteRejected,
    cents_per_page_of,
    create_quote,
    get_owned_quote,
)


router = APIRouter(prefix="/api/v1", tags=["miniapp-quotes"])

_QUOTE_NOT_FOUND = HTTPException(
    status_code=status.HTTP_404_NOT_FOUND,
    detail="报价不存在或已失效。",
)


def _view(
    quote: QuoteSession,
    cents_per_page: int,
) -> QuoteView:
    remaining = quote.expires_at - datetime.now(timezone.utc)
    return QuoteView(
        id=quote.id,
        page_count=quote.page_count,
        cents_per_page=cents_per_page,
        amount_cents=quote.quoted_amount_cents,
        expires_at=quote.expires_at,
        expires_in_seconds=max(0, round(remaining.total_seconds())),
        service_tier=quote.service_tier,
        service_tier_label=service_tier_label(quote.service_tier),
        grading_standard=quote.grading_standard,
        note=quote.note,
    )


@router.post(
    "/quotes",
    response_model=QuoteView,
    status_code=status.HTTP_201_CREATED,
)
def create_quote_session(
    user: CurrentUser,
    session: DatabaseSession,
    settings: Settings,
    service_tier: Annotated[ServiceTier, Form()],
    grading_standard: Annotated[GradingStandard, Form()],
    source_pdf: Annotated[UploadFile, File()],
    note: Annotated[str, Form(max_length=MAX_NOTE_CHARS)] = "",
    reference_pdf: Annotated[UploadFile | None, File()] = None,
) -> QuoteView:
    # Operational limits and the price are read per request, database first, so
    # an admin's change takes effect without a redeploy. Values already
    # snapshotted onto an existing quote are unaffected.
    operational = resolve_settings(session, settings)
    if service_tier == SUMMARY_REPORT and not settings.summary_report_enabled:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="简明评分尚未开放。",
        )
    store = LocalFileStore(settings.data_dir, max_bytes=operational["max_pdf_bytes"])
    try:
        result = create_quote(
            session=session,
            store=store,
            owner_user_id=user.id,
            service_tier=service_tier,
            grading_standard=grading_standard,
            note=note.strip(),
            source_stream=source_pdf.file,
            reference_stream=None if reference_pdf is None else reference_pdf.file,
            cents_per_page=active_cents_per_page(
                session,
                settings,
                service_tier,
            ),
            max_pages=operational["max_pdf_pages"],
            ttl_seconds=operational["quote_ttl_seconds"],
        )
    except (PdfValidationError, FileStorageError, QuoteRejected) as error:
        session.rollback()
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(error),
        ) from None

    return _view(result.quote, result.cents_per_page)


@router.get("/service-tiers", response_model=ServiceTierListView)
def read_service_tiers(
    session: DatabaseSession,
    settings: Settings,
) -> ServiceTierListView:
    items: list[ServiceTierView] = []
    for tier in (SUMMARY_REPORT, ANNOTATED_REVIEW):
        definition = SERVICE_TIER_DEFINITIONS[tier]
        items.append(
            ServiceTierView(
                id=tier,
                label=definition.label,
                description=definition.description,
                delivery_label=definition.delivery_label,
                cents_per_page=active_cents_per_page(session, settings, tier),
                enabled=(tier != SUMMARY_REPORT or settings.summary_report_enabled),
            )
        )
    return ServiceTierListView(items=items)


@router.get("/quotes/{quote_id}", response_model=QuoteView)
def read_quote_session(
    quote_id: str,
    user: CurrentUser,
    session: DatabaseSession,
) -> QuoteView:
    quote = get_owned_quote(session, user.id, quote_id)
    if quote is None:
        raise _QUOTE_NOT_FOUND
    return _view(quote, cents_per_page_of(session, quote))
