from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import BinaryIO

from sqlalchemy import select
from sqlalchemy.orm import Session

from server.adapters.pdf import PdfValidationError
from server.adapters.files import FileStorageError, LocalFileStore
from server.domain.service_tiers import ServiceTier, require_service_tier
from server.models import FileObject, PriceRule, QuoteSession
from server.services.files import FILE_TTL, FileKind, store_temporary_pdf


QUOTE_TTL = FILE_TTL

SUPPORTED_GRADING_STANDARDS = frozenset({"league_second_round", "cmo", "imo"})


class QuoteRejected(ValueError):
    """The submitted intake data cannot produce a quote."""


@dataclass(frozen=True)
class QuoteResult:
    quote: QuoteSession
    cents_per_page: int


def active_price_rule(
    session: Session,
    service_tier: ServiceTier,
    cents_per_page: int,
) -> PriceRule:
    now = datetime.now(timezone.utc)
    rule = session.scalar(
        select(PriceRule)
        .where(
            PriceRule.service_tier == service_tier,
            PriceRule.cents_per_page == cents_per_page,
            PriceRule.retired_at.is_(None),
            PriceRule.effective_from <= now,
        )
        .order_by(PriceRule.effective_from.desc(), PriceRule.id)
        .limit(1)
    )
    if rule is None:
        rule = PriceRule(
            service_tier=service_tier,
            cents_per_page=cents_per_page,
            effective_from=now,
        )
        session.add(rule)
        session.flush()
    return rule


def create_quote(
    *,
    session: Session,
    store: LocalFileStore,
    owner_user_id: str,
    service_tier: str,
    grading_standard: str,
    note: str,
    source_stream: BinaryIO,
    reference_stream: BinaryIO | None,
    cents_per_page: int,
    max_pages: int,
    ttl_seconds: int,
) -> QuoteResult:
    try:
        resolved_service_tier = require_service_tier(service_tier)
    except ValueError as error:
        raise QuoteRejected(str(error)) from None
    if grading_standard not in SUPPORTED_GRADING_STANDARDS:
        raise QuoteRejected("请选择联赛二试、CMO 或 IMO 评分标准。")

    written: list[FileObject] = []
    ttl = timedelta(seconds=ttl_seconds)
    try:
        source = store_temporary_pdf(
            session=session,
            store=store,
            owner_user_id=owner_user_id,
            kind=FileKind.SOURCE,
            stream=source_stream,
            max_pages=max_pages,
            ttl=ttl,
        )
        written.append(source.file_object)

        reference_file_id: str | None = None
        if reference_stream is not None:
            reference = store_temporary_pdf(
                session=session,
                store=store,
                owner_user_id=owner_user_id,
                kind=FileKind.REFERENCE,
                stream=reference_stream,
                max_pages=max_pages,
                ttl=ttl,
            )
            written.append(reference.file_object)
            reference_file_id = reference.file_object.id
    except (PdfValidationError, FileStorageError):
        for record in written:
            store.delete(record.relative_path)
        raise

    rule = active_price_rule(session, resolved_service_tier, cents_per_page)
    quote = QuoteSession(
        owner_user_id=owner_user_id,
        source_file_id=source.file_object.id,
        reference_file_id=reference_file_id,
        price_rule_id=rule.id,
        service_tier=resolved_service_tier,
        grading_standard=grading_standard,
        league_scope=("auto" if grading_standard == "league_second_round" else None),
        note=note,
        page_count=source.page_count,
        quoted_amount_cents=source.page_count * rule.cents_per_page,
        expires_at=datetime.now(timezone.utc) + ttl,
    )
    session.add(quote)
    session.commit()
    return QuoteResult(quote=quote, cents_per_page=rule.cents_per_page)


def get_owned_quote(
    session: Session,
    owner_user_id: str,
    quote_id: str,
) -> QuoteSession | None:
    return session.scalar(
        select(QuoteSession).where(
            QuoteSession.id == quote_id,
            QuoteSession.owner_user_id == owner_user_id,
        )
    )


def cents_per_page_of(session: Session, quote: QuoteSession) -> int:
    rule = session.get(PriceRule, quote.price_rule_id)
    if rule is None:
        raise QuoteRejected("报价规则缺失。")
    if rule.service_tier != quote.service_tier:
        raise QuoteRejected("报价档位与价格规则不一致。")
    return rule.cents_per_page
