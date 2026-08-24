from __future__ import annotations

import hashlib
from uuid import uuid4

import anyio
from fastapi import APIRouter, HTTPException, Header, Request, Response, status
from sqlalchemy.exc import SQLAlchemyError

from server.adapters.files import FileStorageError, LocalFileStore
from server.api.dependencies import Settings
from server.api.worker_dependencies import CurrentWorker
from server.schemas.results import (
    BeginUploadsRequest,
    CommitResultRequest,
    CommitResultView,
    StagedResultView,
    UploadGrantView,
)
from server.services.leases import LeaseConflict
from server.services.results import (
    ResultKind,
    ResultService,
    UploadNotAuthorized,
    UploadRejected,
)


router = APIRouter(prefix="/worker/v1/jobs", tags=["worker-results"])

_LEASE_CONFLICT = HTTPException(
    status_code=status.HTTP_409_CONFLICT,
    detail="批改任务不存在或租约已失效。",
)


def _service(request: Request, settings) -> ResultService:
    return ResultService(
        request.app.state.session_factory,
        LocalFileStore(settings.data_dir),
        secret=settings.session_secret,
        acceptance_ttl_seconds=settings.acceptance_ttl_seconds,
        max_pdf_bytes=settings.max_pdf_bytes,
        max_pdf_pages=settings.max_pdf_pages,
    )


@router.post("/{job_id}/result/uploads", response_model=dict[str, UploadGrantView])
def begin_result_uploads(
    job_id: str,
    payload: BeginUploadsRequest,
    worker: CurrentWorker,
    request: Request,
    settings: Settings,
) -> dict[str, UploadGrantView]:
    """Issue single-use upload tokens and move the job to UPLOADING."""
    try:
        grants = _service(request, settings).begin_uploads(
            job_id=job_id,
            worker_id=worker.worker_id,
            lease_version=payload.lease_version,
        )
    except LeaseConflict:
        raise _LEASE_CONFLICT from None
    return {
        str(grant.kind): UploadGrantView(
            upload_token=grant.upload_token,
            max_bytes=grant.max_bytes,
        )
        for grant in grants
    }


@router.put(
    "/{job_id}/result/{kind}",
    response_model=StagedResultView,
    status_code=status.HTTP_201_CREATED,
)
async def stage_result_upload(
    job_id: str,
    kind: ResultKind,
    worker: CurrentWorker,
    request: Request,
    response: Response,
    settings: Settings,
    x_upload_token: str = Header(...),
    x_content_sha256: str = Header(...),
) -> StagedResultView:
    """Stream one verified result artefact into per-lease staging."""
    service = _service(request, settings)
    incoming = LocalFileStore(settings.data_dir).resolve(
        f"staging/worker-upload-{uuid4().hex}.part"
    )
    incoming.parent.mkdir(parents=True, exist_ok=True)
    try:
        digest = hashlib.sha256()
        received = 0
        limit = service.max_bytes(kind)
        async with await anyio.open_file(incoming, "wb") as output:
            async for chunk in request.stream():
                received += len(chunk)
                if received > limit:
                    raise UploadRejected(f"文件超过 {limit} 字节上限。")
                digest.update(chunk)
                await output.write(chunk)
        if digest.hexdigest() != x_content_sha256.lower():
            raise UploadRejected("上传内容的 SHA-256 与声明不一致。")
        with incoming.open("rb") as stream:
            staged = service.stage_upload(
                job_id=job_id,
                worker_id=worker.worker_id,
                kind=kind,
                token=x_upload_token,
                declared_sha256=x_content_sha256,
                stream=stream,
            )
    except UploadNotAuthorized as error:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=str(error),
        ) from None
    except UploadRejected as error:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(error),
        ) from None
    except LeaseConflict as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(error),
        ) from None
    finally:
        incoming.unlink(missing_ok=True)
    response.status_code = (
        status.HTTP_200_OK if staged.already_staged else status.HTTP_201_CREATED
    )
    return StagedResultView(
        file_id=staged.file_id,
        kind=str(staged.kind),
        sha256=staged.sha256,
        size_bytes=staged.size_bytes,
    )


@router.post("/{job_id}/result/commit", response_model=CommitResultView)
def commit_result(
    job_id: str,
    payload: CommitResultRequest,
    worker: CurrentWorker,
    request: Request,
    settings: Settings,
) -> CommitResultView:
    """Deliver the staged result exactly once, or report it already landed."""
    try:
        outcome = _service(request, settings).commit_result(
            job_id=job_id,
            worker_id=worker.worker_id,
            lease_version=payload.lease_version,
            result_json_file_id=payload.result_json_file_id,
            result_pdf_file_id=payload.result_pdf_file_id,
        )
    except LeaseConflict as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(error),
        ) from None
    except UploadRejected as error:
        # Contract mismatches are deterministic Worker output errors.  They
        # must be rejected explicitly rather than surfacing as an opaque 500,
        # and the order must remain undelivered.
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(error),
        ) from None
    except (SQLAlchemyError, FileStorageError):
        # The service already rolled the transaction back and returned the
        # staged bytes, so the Worker may safely retry the same commit.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="结果提交暂时失败，请稍后重试。",
        ) from None
    return CommitResultView(
        status="already_committed" if outcome.already_committed else "committed",
        order_id=outcome.order_id,
        round_number=outcome.round_number,
    )
