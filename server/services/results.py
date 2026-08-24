from __future__ import annotations

import hashlib
import hmac
import json
import secrets
from base64 import urlsafe_b64decode, urlsafe_b64encode
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from typing import BinaryIO
from uuid import NAMESPACE_URL, uuid4, uuid5

from sqlalchemy import select, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from server.adapters.files import FileStorageError, LocalFileStore
from server.adapters.pdf import PdfValidationError, inspect_pdf
from server.db_locking import lock_row
from server.domain.states import (
    ORDER_TRANSITIONS,
    JobState,
    OrderState,
    require_job_transition,
    require_order_transition,
)
from server.models import (
    FileObject,
    GradingJob,
    GradingRound,
    Order,
    QuoteSession,
    Worker,
    WorkerEvent,
)
from server.services.files import FileState
from server.services.leases import LeaseConflict, STARTED_JOB_STATES
from server.services.grading_result_validation import (
    GradingResultInvalid,
    validate_staged_result,
)


RESULT_STAGING_DIRECTORY = "result-staging"
ORDERS_DIRECTORY = "orders"

MAX_RESULT_JSON_BYTES = 4 * 1024 * 1024
UPLOAD_TOKEN_TTL = timedelta(minutes=30)


class ResultKind(StrEnum):
    JSON = "result_json"
    PDF = "result_pdf"


_EXTENSIONS = {ResultKind.JSON: "json", ResultKind.PDF: "pdf"}


class UploadRejected(ValueError):
    """The uploaded bytes failed verification and must not be registered."""


class UploadNotAuthorized(PermissionError):
    """The upload token is forged, expired, or bound to different work."""


@dataclass(frozen=True)
class UploadGrant:
    kind: ResultKind
    upload_token: str
    max_bytes: int


@dataclass(frozen=True)
class StagedResult:
    file_id: str
    kind: ResultKind
    relative_path: str
    sha256: str
    size_bytes: int
    already_staged: bool = False


@dataclass(frozen=True)
class CommitOutcome:
    order_id: str
    round_number: int
    already_committed: bool


def _sign(payload: bytes, secret: str) -> str:
    return hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()


def issue_upload_token(
    *,
    job_id: str,
    worker_id: str,
    lease_version: int,
    kind: ResultKind,
    max_bytes: int,
    secret: str,
    now: datetime | None = None,
) -> str:
    """Mint a single-use token bound to the job, holder, fence, kind and size.

    Binding all five means a token cannot be replayed for another kind, reused
    by another Worker, or survive a lease being reclaimed. Single use is
    enforced separately by the consumed marker on the staged row.
    """
    issued_at = now or datetime.now(timezone.utc)
    claims = {
        "jti": uuid4().hex,
        "job_id": job_id,
        "worker_id": worker_id,
        "lease_version": lease_version,
        "kind": str(kind),
        "max_bytes": max_bytes,
        "expires_at": (issued_at + UPLOAD_TOKEN_TTL).isoformat(),
    }
    payload = urlsafe_b64encode(
        json.dumps(claims, separators=(",", ":"), sort_keys=True).encode("utf-8")
    ).rstrip(b"=")
    return f"{payload.decode('ascii')}.{_sign(payload, secret)}"


def _decode_upload_token(token: str, secret: str) -> dict:
    payload_text, separator, signature = token.rpartition(".")
    if not separator or not payload_text:
        raise UploadNotAuthorized("上传凭证无效。")
    # The payload must be canonical ASCII base64url. Silently dropping
    # undecodable characters would make the signature malleable: the same grant
    # would have unlimited valid spellings, defeating any future revocation or
    # audit that keys on the token string.
    try:
        payload = payload_text.encode("ascii")
    except UnicodeEncodeError as error:
        raise UploadNotAuthorized("上传凭证无效。") from error
    if not hmac.compare_digest(_sign(payload, secret), signature):
        raise UploadNotAuthorized("上传凭证无效。")
    padding = b"=" * (-len(payload) % 4)
    try:
        claims = json.loads(urlsafe_b64decode(payload + padding))
    except (ValueError, UnicodeDecodeError) as error:
        raise UploadNotAuthorized("上传凭证无效。") from error
    if datetime.fromisoformat(claims["expires_at"]) <= datetime.now(timezone.utc):
        raise UploadNotAuthorized("上传凭证已过期。")
    return claims


def _staged_relative_path(
    job_id: str,
    lease_version: int,
    kind: ResultKind,
    file_id: str,
) -> str:
    return (
        f"{RESULT_STAGING_DIRECTORY}/{job_id}/{lease_version}/"
        f"{file_id}.{_EXTENSIONS[kind]}"
    )


def _final_relative_path(
    order: Order,
    round_number: int,
    kind: ResultKind,
    file_id: str,
) -> str:
    created = order.created_at.astimezone(timezone.utc)
    return (
        f"{ORDERS_DIRECTORY}/{created:%Y}/{created:%m}/{order.id}/{round_number}/"
        f"{file_id}.{_EXTENSIONS[kind]}"
    )


class ResultService:
    """Stages Worker results and delivers them in one fenced transaction."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        store: LocalFileStore,
        *,
        secret: str,
        acceptance_ttl_seconds: int,
        max_pdf_bytes: int,
        max_pdf_pages: int,
    ) -> None:
        self._session_factory = session_factory
        self._store = store
        self._secret = secret
        self._acceptance_ttl_seconds = acceptance_ttl_seconds
        self._max_pdf_bytes = max_pdf_bytes
        self._max_pdf_pages = max_pdf_pages

    def _max_bytes(self, kind: ResultKind) -> int:
        return MAX_RESULT_JSON_BYTES if kind is ResultKind.JSON else self._max_pdf_bytes

    def max_bytes(self, kind: ResultKind) -> int:
        """Public receive limit used by the streaming HTTP adapter."""
        return self._max_bytes(kind)

    def begin_uploads(
        self,
        *,
        job_id: str,
        worker_id: str,
        lease_version: int,
    ) -> tuple[UploadGrant, ...]:
        """Move a running job to UPLOADING and issue its single-use tokens."""
        with self._session_factory() as session:
            job = lock_row(session, GradingJob, job_id)
            if (
                job is None
                or job.worker_id != worker_id
                or job.lease_version != lease_version
                or job.state not in {JobState.RUNNING, JobState.UPLOADING}
            ):
                raise LeaseConflict("批改任务不存在或租约已失效。")
            if job.state == JobState.RUNNING:
                require_job_transition(JobState(job.state), JobState.UPLOADING)
                job.state = JobState.UPLOADING
                session.add(job)
                session.commit()

        return tuple(
            UploadGrant(
                kind=kind,
                upload_token=issue_upload_token(
                    job_id=job_id,
                    worker_id=worker_id,
                    lease_version=lease_version,
                    kind=kind,
                    max_bytes=self._max_bytes(kind),
                    secret=self._secret,
                ),
                max_bytes=self._max_bytes(kind),
            )
            for kind in (ResultKind.JSON, ResultKind.PDF)
        )

    def stage_upload(
        self,
        *,
        job_id: str,
        worker_id: str,
        kind: ResultKind,
        token: str,
        declared_sha256: str,
        stream: BinaryIO,
    ) -> StagedResult:
        """Verify and stage one result artefact.

        Nothing is registered unless the bytes pass the size limit, the declared
        SHA-256 and (for PDFs) a real readability check.
        """
        claims = _decode_upload_token(token, self._secret)
        if (
            claims.get("job_id") != job_id
            or claims.get("worker_id") != worker_id
            or claims.get("kind") != str(kind)
        ):
            raise UploadNotAuthorized("上传凭证与该任务不匹配。")

        with self._session_factory() as session:
            job = lock_row(session, GradingJob, job_id)
            if (
                job is None
                or job.worker_id != worker_id
                or job.lease_version != claims.get("lease_version")
                or job.state != JobState.UPLOADING
            ):
                raise LeaseConflict("批改任务不存在或租约已失效。")

            # SQLite has no row-level FOR UPDATE.  A guarded no-op update opens
            # its write transaction before file I/O, while MySQL naturally
            # serialises on the already-locked job row.
            claimed = session.execute(
                update(GradingJob)
                .where(
                    GradingJob.id == job.id,
                    GradingJob.worker_id == worker_id,
                    GradingJob.lease_version == job.lease_version,
                    GradingJob.state == JobState.UPLOADING,
                )
                .values(state=JobState.UPLOADING)
            )
            if claimed.rowcount != 1:
                session.rollback()
                raise LeaseConflict("批改任务不存在或租约已失效。")

            existing = session.scalar(
                select(FileObject).where(
                    FileObject.kind == kind,
                    FileObject.relative_path.like(
                        f"{RESULT_STAGING_DIRECTORY}/{job_id}/{job.lease_version}/%"
                    ),
                )
            )
            if existing is not None:
                if existing.sha256 != declared_sha256.lower():
                    session.rollback()
                    raise LeaseConflict("该结果文件已以不同内容上传。")
                if not self._store.resolve(existing.relative_path).is_file():
                    session.delete(existing)
                    session.flush()
                else:
                    session.commit()
                    return StagedResult(
                        file_id=existing.id,
                        kind=kind,
                        relative_path=existing.relative_path,
                        sha256=existing.sha256,
                        size_bytes=existing.size_bytes,
                        already_staged=True,
                    )

            owner_user_id = self._owner_of(session, job)
            lease_version = job.lease_version
            file_id = str(
                uuid5(
                    NAMESPACE_URL,
                    f"grader-result:{job_id}:{lease_version}:{kind}",
                )
            )
            relative_path = _staged_relative_path(
                job_id, lease_version, kind, file_id
            )
            try:
                stored = self._store.put_at(
                    relative_path,
                    stream,
                    max_bytes=claims.get("max_bytes", self._max_bytes(kind)),
                )
            except FileStorageError as error:
                self._store.delete(relative_path)
                session.rollback()
                raise UploadRejected(str(error)) from None

            if stored.sha256 != declared_sha256.lower():
                self._store.delete(relative_path)
                session.rollback()
                raise UploadRejected("上传内容的 SHA-256 与声明不一致。")
            if kind is ResultKind.PDF:
                try:
                    inspect_pdf(
                        self._store.resolve(relative_path),
                        max_pages=self._max_pdf_pages + 1,
                    )
                except PdfValidationError as error:
                    self._store.delete(relative_path)
                    session.rollback()
                    raise UploadRejected(str(error)) from None

            record = FileObject(
                id=file_id,
                owner_user_id=owner_user_id,
                kind=kind,
                relative_path=relative_path,
                sha256=stored.sha256,
                size_bytes=stored.size_bytes,
                state=FileState.TEMPORARY,
                expires_at=datetime.now(timezone.utc) + UPLOAD_TOKEN_TTL,
            )
            session.add(record)
            try:
                session.commit()
            except SQLAlchemyError:
                session.rollback()
                self._store.delete(relative_path)
                raise
        return StagedResult(
            file_id=file_id,
            kind=kind,
            relative_path=relative_path,
            sha256=stored.sha256,
            size_bytes=stored.size_bytes,
        )

    @staticmethod
    def _owner_of(session: Session, job: GradingJob) -> str:
        order = session.get(Order, job.order_id)
        quote = session.get(QuoteSession, order.quote_session_id)
        return quote.owner_user_id

    def commit_result(
        self,
        *,
        job_id: str,
        worker_id: str,
        lease_version: int,
        result_json_file_id: str,
        result_pdf_file_id: str,
    ) -> CommitOutcome:
        """Deliver a verified result exactly once.

        Ordering is deliberate. Every validation and state change is flushed
        while the job row is locked, the FileObject rows are pointed at their
        final paths, then the bytes are *copied* to those paths, then the
        transaction commits, and only after a successful commit is the staged
        copy removed.

        Copy-commit-delete is chosen over move-commit because a move destroys
        the only copy before the outcome is known: a crash between the move and
        the commit would leave the row rolled back with its staged source gone,
        so the Worker's retry would have nothing to deliver and the job would sit
        in UPLOADING forever. With a copy, the worst case is an unreferenced file
        at the final path — harmless, collectable — while the staged bytes stay
        intact and the retry succeeds.
        """
        with self._session_factory() as session:
            job = lock_row(session, GradingJob, job_id)
            if job is None or job.worker_id != worker_id:
                raise LeaseConflict("批改任务不存在或租约已失效。")
            if job.lease_version != lease_version:
                raise LeaseConflict("批改任务不存在或租约已失效。")

            round_record = session.scalar(
                select(GradingRound).where(
                    GradingRound.order_id == job.order_id,
                    GradingRound.round_number == job.round_number,
                )
            )
            if (
                job.state == JobState.SUCCEEDED
                and round_record is not None
                and round_record.result_json_file_id == result_json_file_id
                and round_record.result_pdf_file_id == result_pdf_file_id
            ):
                return CommitOutcome(
                    order_id=job.order_id,
                    round_number=job.round_number,
                    already_committed=True,
                )
            if job.state != JobState.UPLOADING:
                raise LeaseConflict("批改任务不存在或租约已失效。")
            if round_record is None:
                raise LeaseConflict("批改轮次不存在。")

            order = lock_row(session, Order, job.order_id)
            delivered = (
                OrderState.V1_DELIVERED
                if job.round_number == 1
                else OrderState.V2_DELIVERED
            )
            if delivered not in ORDER_TRANSITIONS.get(
                OrderState(order.state), frozenset()
            ):
                # The order moved on while this Worker was uploading — almost
                # always a refund requested mid-grading, which Phase 05allows.
                # A late delivery must not resurrect it. Report the same
                # conflict the daemon already handles for a lost lease rather
                # than raising, which would 500 and strand the job in UPLOADING.
                raise LeaseConflict("订单状态已变更，结果不再可交付。")
            require_order_transition(OrderState(order.state), delivered)
            require_job_transition(JobState(job.state), JobState.SUCCEEDED)

            staged_prefix = (
                f"{RESULT_STAGING_DIRECTORY}/{job_id}/{job.lease_version}/"
            )
            copies: list[tuple[str, str]] = []
            staged_paths: dict[ResultKind, str] = {}
            for kind, file_id in (
                (ResultKind.JSON, result_json_file_id),
                (ResultKind.PDF, result_pdf_file_id),
            ):
                record = session.get(FileObject, file_id)
                if record is None or record.kind != kind:
                    raise LeaseConflict("结果文件缺失或不属于该任务。")
                # A file staged under a different fence belongs to a superseded
                # attempt and must never be delivered.
                if not record.relative_path.startswith(staged_prefix):
                    raise LeaseConflict("结果文件缺失或不属于该任务。")
                staged_paths[kind] = record.relative_path
                final_path = _final_relative_path(
                    order, job.round_number, kind, record.id
                )
                copies.append((record.relative_path, final_path))
                record.relative_path = final_path
                record.state = FileState.RETAINED
                session.add(record)

            quote = session.get(QuoteSession, order.quote_session_id)
            if quote is None:
                raise LeaseConflict("订单报价快照不存在。")
            try:
                validate_staged_result(
                    json_path=self._store.resolve(staged_paths[ResultKind.JSON]),
                    pdf_path=self._store.resolve(staged_paths[ResultKind.PDF]),
                    service_tier=round_record.service_tier,
                    grading_standard=round_record.grading_standard,
                    league_scope=round_record.league_scope,
                    source_page_count=quote.page_count,
                )
            except GradingResultInvalid as error:
                raise UploadRejected(str(error)) from None

            now = datetime.now(timezone.utc)
            round_record.result_json_file_id = result_json_file_id
            round_record.result_pdf_file_id = result_pdf_file_id
            round_record.delivered_at = now
            session.add(round_record)

            job.state = JobState.SUCCEEDED
            job.lease_expires_at = None
            job.ack_deadline = None
            session.add(job)

            order.state = delivered
            order.acceptance_deadline = now + timedelta(
                seconds=self._acceptance_ttl_seconds
            )
            session.add(order)

            worker = session.get(Worker, worker_id)
            if worker is not None and worker.current_job_id == job.id:
                worker.current_job_id = None
                session.add(worker)

            session.add(
                WorkerEvent(
                    worker_id=worker_id,
                    job_id=job.id,
                    event_type="result_committed",
                    details={
                        "lease_version": job.lease_version,
                        "round_number": job.round_number,
                    },
                )
            )

            # Surface any constraint violation before a single byte is written.
            session.flush()

            copied: list[str] = []
            try:
                for staged_path, final_path in copies:
                    self._store.copy(staged_path, final_path)
                    copied.append(final_path)
                session.commit()
            except (SQLAlchemyError, FileStorageError):
                session.rollback()
                # The staged originals are untouched, so removing the unreferenced
                # final copies restores the pre-commit state exactly.
                for final_path in copied:
                    self._store.delete(final_path)
                raise

            # Committed: the final paths are now authoritative, so the staged
            # copies are redundant. A failure here only leaves collectable
            # scratch data and must not fail an already-delivered order.
            for staged_path, _ in copies:
                try:
                    self._store.delete(staged_path)
                except FileStorageError:
                    pass
            return CommitOutcome(
                order_id=job.order_id,
                round_number=job.round_number,
                already_committed=False,
            )
