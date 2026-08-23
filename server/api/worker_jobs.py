from __future__ import annotations

import asyncio
import time

import anyio.to_thread
from fastapi import APIRouter, HTTPException, Header, Request, Response, status

from server.api.dependencies import Settings
from server.api.worker_dependencies import CurrentWorker
from server.schemas.heartbeats import AckRequest, LeaseStateView, RenewRequest
from server.schemas.worker_jobs import BundleFileView, TaskBundleView
from server.services.bundle_downloads import (
    BundleDownloadError,
    BundleDownloadService,
    BundleLeaseConflict,
    BundleNotFound,
    BundleTokenInvalid,
)
from server.services.leases import LeaseConflict, LeaseService, TaskBundle
from server.services.workers import ACK_SECONDS, LEASE_SECONDS


router = APIRouter(prefix="/worker/v1/jobs", tags=["worker-jobs"])

MAX_LONG_POLL_SECONDS = 25
_POLL_INTERVAL_SECONDS = 1.0

_LEASE_CONFLICT = HTTPException(
    status_code=status.HTTP_409_CONFLICT,
    detail="批改任务不存在或租约已失效。",
)


def parse_wait_seconds(prefer_header: str | None) -> int:
    """Read RFC 7240 `Prefer: wait=<seconds>`, capped at the protocol maximum.

    Anything unparseable falls back to the maximum so a malformed header cannot
    turn a long poll into a hot loop.
    """
    if prefer_header is None:
        return MAX_LONG_POLL_SECONDS
    for token in prefer_header.split(","):
        name, separator, value = token.strip().partition("=")
        if not separator or name.strip().lower() != "wait":
            continue
        try:
            requested = int(value.strip())
        except ValueError:
            return MAX_LONG_POLL_SECONDS
        return max(0, min(requested, MAX_LONG_POLL_SECONDS))
    return MAX_LONG_POLL_SECONDS


def _view(bundle: TaskBundle) -> TaskBundleView:
    def describe(file) -> BundleFileView | None:
        if file is None:
            return None
        return BundleFileView(
            file_id=file.file_id,
            kind=file.kind,
            sha256=file.sha256,
            size_bytes=file.size_bytes,
            download_token=file.download_token,
        )

    return TaskBundleView(
        job_id=bundle.job_id,
        order_id=bundle.order_id,
        round_number=bundle.round_number,
        lease_version=bundle.lease_version,
        service_tier=bundle.service_tier,
        grading_standard=bundle.grading_standard,
        league_scope=bundle.league_scope,
        note=bundle.note,
        page_count=bundle.page_count,
        source_file=describe(bundle.source_file),
        reference_file=describe(bundle.reference_file),
        ack_deadline=bundle.ack_deadline,
        lease_expires_at=bundle.lease_expires_at,
        lease_seconds=LEASE_SECONDS,
        ack_seconds=ACK_SECONDS,
    )


@router.post(
    "/lease",
    response_model=TaskBundleView,
    responses={status.HTTP_204_NO_CONTENT: {"description": "队列中没有可领取的任务。"}},
)
async def lease_job(request: Request, worker: CurrentWorker):
    """Claim at most one queued job, long-polling for up to 25 seconds."""
    service = LeaseService(request.app.state.session_factory)
    worker_id = worker.worker_id
    wait_seconds = parse_wait_seconds(request.headers.get("Prefer"))

    deadline = time.monotonic() + wait_seconds
    while True:
        bundle = await anyio.to_thread.run_sync(service.try_lease, worker_id)
        if bundle is not None:
            return _view(bundle)
        if time.monotonic() >= deadline:
            return Response(status_code=status.HTTP_204_NO_CONTENT)
        await asyncio.sleep(min(_POLL_INTERVAL_SECONDS, max(0.0, deadline - time.monotonic())))


def _lease_state(job) -> LeaseStateView:
    return LeaseStateView(
        job_id=job.id,
        state=job.state,
        lease_version=job.lease_version,
        lease_expires_at=job.lease_expires_at,
        lease_seconds=LEASE_SECONDS,
    )


@router.post("/{job_id}/ack", response_model=LeaseStateView)
def acknowledge_job(
    job_id: str,
    payload: AckRequest,
    worker: CurrentWorker,
    request: Request,
) -> LeaseStateView:
    """Confirm the Worker started the job it leased."""
    service = LeaseService(request.app.state.session_factory)
    try:
        job = service.acknowledge(
            job_id=job_id,
            worker_id=worker.worker_id,
            lease_version=payload.lease_version,
        )
    except LeaseConflict:
        raise _LEASE_CONFLICT from None
    return _lease_state(job)


@router.post("/{job_id}/renew", response_model=LeaseStateView)
def renew_job_lease(
    job_id: str,
    payload: RenewRequest,
    worker: CurrentWorker,
    request: Request,
) -> LeaseStateView:
    """Extend ownership of a started job using server time."""
    service = LeaseService(request.app.state.session_factory)
    try:
        job = service.renew(
            job_id=job_id,
            worker_id=worker.worker_id,
            lease_version=payload.lease_version,
            phase=payload.phase,
        )
    except LeaseConflict:
        raise _LEASE_CONFLICT from None
    return _lease_state(job)


_BUNDLE_KINDS = {"source", "reference"}


def _bundle_service(request: Request, settings: Settings) -> BundleDownloadService:
    return BundleDownloadService(
        request.app.state.session_factory,
        storage_root=settings.data_dir,
    )


def _download_exception(exc: BundleDownloadError) -> HTTPException:
    if isinstance(exc, BundleNotFound):
        return HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="请求的批改材料不存在。",
        )
    if isinstance(exc, BundleLeaseConflict):
        return HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="批改任务不存在或租约已失效。",
        )
    return HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="下载凭证无效。",
    )


@router.get(
    "/{job_id}/bundle/{kind}",
    responses={
        status.HTTP_200_OK: {"content": {"application/pdf": {}}},
        status.HTTP_403_FORBIDDEN: {"description": "Download token invalid."},
        status.HTTP_404_NOT_FOUND: {"description": "Bundle file not found."},
        status.HTTP_409_CONFLICT: {"description": "Lease no longer held."},
    },
)
async def download_bundle_file(
    job_id: str,
    kind: str,
    worker: CurrentWorker,
    request: Request,
    settings: Settings,
    x_download_token: str | None = Header(default=None, alias="X-Download-Token"),
) -> Response:
    """Stream one bundle PDF to a worker holding an active lease.

    Phase 04's one approved server-side exception: the worker downloads
    source and reference PDFs through this endpoint rather than via
    pre-signed URLs. The download token is bound to the lease version
    so a recycled lease invalidates older tokens immediately.
    """
    if kind not in _BUNDLE_KINDS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="未知的下载材料类型。",
        )
    if not x_download_token:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="下载凭证无效。",
        )

    service = _bundle_service(request, settings)
    try:
        download = await anyio.to_thread.run_sync(
            lambda: service.resolve(
                job_id=job_id,
                worker_id=worker.worker_id,
                kind=kind,
                download_token=x_download_token,
            )
        )
        handle = await anyio.to_thread.run_sync(lambda: service.open_stream(download))
    except BundleDownloadError as exc:
        raise _download_exception(exc) from None

    async def stream_chunks():
        try:
            while True:
                chunk = await anyio.to_thread.run_sync(handle.read, 1024 * 1024)
                if not chunk:
                    break
                yield chunk
        finally:
            await anyio.to_thread.run_sync(handle.close)

    return Response(
        content=await _consume(stream_chunks()),
        media_type="application/pdf",
        headers={
            "X-Content-SHA256": download.sha256,
            "X-Content-Length": str(download.size_bytes),
        },
    )


async def _consume(aiter):
    chunks: list[bytes] = []
    async for chunk in aiter:
        chunks.append(chunk)
    return b"".join(chunks)
