"""Worker bundle download service.

Phase 04 adds the one approved server-side exception to the Phase 03
contract: a GET endpoint that streams source/reference PDFs to a worker
that holds an active lease. The download is authorised by the worker
credential plus a per-lease download token issued with the lease; the
token binds the download to one lease_version so a recycled lease
immediately invalidates older tokens.
"""
from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from server.models import FileObject, GradingJob, Order, QuoteSession
from server.services.leases import ACTIVE_JOB_STATES, LeaseConflict


@dataclass(frozen=True)
class BundleDownload:
    """A validated bundle file ready to stream to the worker."""

    file_id: str
    relative_path: str
    size_bytes: int
    sha256: str


class BundleDownloadError(Exception):
    """Base class for download authorisation failures."""


class BundleNotFound(BundleDownloadError):
    """The requested kind does not exist on this order."""


class BundleTokenInvalid(BundleDownloadError):
    """The download token does not match the current lease."""


class BundleLeaseConflict(BundleDownloadError):
    """The caller does not hold the lease it is trying to download against."""


class BundleDownloadService:
    """Authorise and locate bundle files for a leased job.

    The service owns three checks:
      1. The job exists and the caller holds its active lease.
      2. The download token matches the one stored on the job for the
         current lease_version.
      3. The requested kind exists on the order.

    The token comparison uses :func:`hmac.compare_digest` so a timing
    oracle cannot leak the stored token byte-by-byte.
    """

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        storage_root: Path,
    ) -> None:
        self._session_factory = session_factory
        self._storage_root = storage_root

    def resolve(
        self,
        *,
        job_id: str,
        worker_id: str,
        kind: str,
        download_token: str,
    ) -> BundleDownload:
        if kind not in {"source", "reference"}:
            raise BundleNotFound(f"unknown bundle kind: {kind}")

        with self._session_factory() as session:
            job = session.get(GradingJob, job_id)
            if job is None:
                raise BundleNotFound("job does not exist")
            if job.worker_id != worker_id:
                raise BundleLeaseConflict("caller does not hold the lease")
            if job.state not in ACTIVE_JOB_STATES:
                raise BundleLeaseConflict("lease is not active")
            if job.bundle_download_tokens is None:
                raise BundleTokenInvalid("no tokens issued for this lease")

            stored_token = job.bundle_download_tokens.get(kind)
            # Resolve the file first so a request for a kind that was never
            # part of the order (e.g. reference on a source-only order)
            # returns 404 NotFound rather than 403 TokenInvalid — the
            # caller legitimately does not know whether the file exists.
            order = session.get(Order, job.order_id)
            quote = session.get(QuoteSession, order.quote_session_id)
            file_id = (
                quote.source_file_id if kind == "source" else quote.reference_file_id
            )
            if file_id is None:
                raise BundleNotFound(f"no {kind} file on this order")

            if not stored_token or not hmac.compare_digest(
                str(stored_token), str(download_token)
            ):
                raise BundleTokenInvalid("download token rejected")

            record = session.get(FileObject, file_id)
            if record is None:
                raise BundleNotFound(f"{kind} file missing from storage")

            return BundleDownload(
                file_id=record.id,
                relative_path=record.relative_path,
                size_bytes=record.size_bytes,
                sha256=record.sha256,
            )

    def open_stream(self, download: BundleDownload):
        """Return a binary file handle for the resolved download.

        Callers must close the handle. The path is validated through
        :class:`LocalFileStore.resolve` at storage time, so we only open
        relative to the configured storage root here.
        """
        path = (self._storage_root / download.relative_path).resolve()
        root = self._storage_root.resolve()
        if path != root and root not in path.parents:
            raise BundleNotFound("file path escaped storage root")
        return path.open("rb")
