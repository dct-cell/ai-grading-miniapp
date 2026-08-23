"""User-facing result downloads.

Phase 06 delivers the product itself: the graded PDF and the result JSON the
mini-program reads the score summary from.

**Authorisation is re-checked on every request, deliberately.** An alternative
design mints a short-lived token and redeems it, but a token is a *cached*
authorisation decision, and `orders.downloads_revoked_at` is precisely a
decision that must not be cached: a refund that succeeds one second after a
token is minted has to stop the download immediately. Since the bytes are served
from local disk by this same application — never delegated to a CDN or
pre-signed object storage — there is nothing to delegate a token to, so the
session credential is checked directly instead.

`wx.downloadFile` sends request headers, so the mini-program authenticates this
endpoint exactly like any other API call.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from server.models import FileObject, GradingRound, Order, QuoteSession
from server.domain.service_tiers import SUMMARY_REPORT


class ResultArtefact(StrEnum):
    """The two artefacts a user may retrieve.

    Only these two. `output/internal/` working files (problem-analysis,
    marking-scheme, proof-map, verification, score-audit) are never exposed:
    they are the grader's scratch space, not a deliverable.
    """

    PDF = "result_pdf"
    JSON = "result_json"


CONTENT_TYPES = {
    ResultArtefact.PDF: "application/pdf",
    ResultArtefact.JSON: "application/json",
}


class ResultDownloadError(Exception):
    """Base class for download refusals."""


class ResultNotFound(ResultDownloadError):
    """No such order, round or artefact for this caller."""


class DownloadsRevoked(ResultDownloadError):
    """The order was refunded, so the paid-for result is no longer available."""


@dataclass(frozen=True)
class ResultDownload:
    relative_path: str
    size_bytes: int
    sha256: str
    content_type: str
    filename: str


def resolve_result_download(
    *,
    session: Session,
    owner_user_id: str,
    order_id: str,
    round_number: int,
    kind: ResultArtefact,
) -> ResultDownload:
    """Authorise one download and locate its file.

    Ownership is joined in SQL from the authenticated user, so a caller can
    never reach another user's order by guessing an id.
    """
    row = session.execute(
        select(Order, QuoteSession)
        .join(QuoteSession, QuoteSession.id == Order.quote_session_id)
        .where(Order.id == order_id, QuoteSession.owner_user_id == owner_user_id)
    ).one_or_none()
    if row is None:
        # Deliberately the same error a missing order raises: a distinct
        # "forbidden" would confirm that this order id belongs to someone.
        raise ResultNotFound("订单不存在。")
    order, _quote = row

    # Checked before the file is even located: a refunded order must not be
    # able to leak the existence or size of its result.
    if order.downloads_revoked_at is not None:
        raise DownloadsRevoked("该订单已退款，下载权限已被撤销。")

    round_record = session.scalar(
        select(GradingRound).where(
            GradingRound.order_id == order.id,
            GradingRound.round_number == round_number,
        )
    )
    if round_record is None or round_record.delivered_at is None:
        raise ResultNotFound("该批改轮次尚未交付。")

    file_id = (
        round_record.result_pdf_file_id
        if kind is ResultArtefact.PDF
        else round_record.result_json_file_id
    )
    if file_id is None:
        raise ResultNotFound("该批改结果文件不存在。")

    record = session.get(FileObject, file_id)
    if record is None:
        raise ResultNotFound("该批改结果文件不存在。")

    extension = "pdf" if kind is ResultArtefact.PDF else "json"
    standard = {
        "league_second_round": "联赛二试",
        "cmo": "CMO",
        "imo": "IMO",
    }.get(round_record.grading_standard, "数学竞赛")
    tier = "简明评分" if round_record.service_tier == SUMMARY_REPORT else "逐页精批"
    date = order.created_at.strftime("%Y%m%d")
    short_id = order.id.split("-")[0]
    return ResultDownload(
        relative_path=record.relative_path,
        size_bytes=record.size_bytes,
        sha256=record.sha256,
        content_type=CONTENT_TYPES[kind],
        # Built from server-side values only. A user-supplied filename could
        # smuggle CR/LF into the Content-Disposition header.
        filename=(
            f"数学竞赛题批改_{standard}_{date}_{short_id}_{tier}.{extension}"
            if kind is ResultArtefact.PDF
            else f"grading-{order.id}-round{round_number}.json"
        ),
    )


def open_result_stream(storage_root: Path, download: ResultDownload):
    """Open the resolved file, refusing anything outside the storage root."""
    root = storage_root.resolve()
    path = (root / download.relative_path).resolve()
    if path != root and root not in path.parents:
        raise ResultNotFound("该批改结果文件不存在。")
    if not path.is_file():
        raise ResultNotFound("该批改结果文件不存在。")
    return path.open("rb")
