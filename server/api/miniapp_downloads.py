"""Result download route.

Registered in every environment, like the Worker control plane and Admin
refunds: delivering a paid-for result is a real feature, not a fake adapter, so
it is deliberately outside FAKE_ADAPTER_ENVIRONMENTS.
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from urllib.parse import quote

import anyio
from fastapi import APIRouter, HTTPException, Request, Response, status
from fastapi.responses import StreamingResponse

from server.api.dependencies import CurrentUser, DatabaseSession, Settings
from server.services.result_downloads import (
    DownloadsRevoked,
    ResultArtefact,
    ResultDownloadError,
    ResultNotFound,
    open_result_stream,
    resolve_result_download,
)


router = APIRouter(prefix="/api/v1/orders", tags=["miniapp-downloads"])

CHUNK_BYTES = 512 * 1024


@router.get(
    "/{order_id}/rounds/{round_number}/result/{kind}",
    responses={
        status.HTTP_200_OK: {
            "content": {"application/pdf": {}, "application/json": {}}
        },
        status.HTTP_404_NOT_FOUND: {"description": "No such order, round or artefact."},
        status.HTTP_410_GONE: {"description": "Downloads revoked by a refund."},
    },
)
async def download_result(
    order_id: str,
    round_number: int,
    kind: str,
    user: CurrentUser,
    session: DatabaseSession,
    settings: Settings,
) -> Response:
    """Stream one delivered artefact to the order's owner.

    Authorisation happens on this request rather than through a token, so a
    refund revokes access immediately instead of when a token would expire.
    """
    try:
        artefact = ResultArtefact(kind)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="该批改结果文件不存在。",
        ) from None

    try:
        download = resolve_result_download(
            session=session,
            owner_user_id=user.id,
            order_id=order_id,
            round_number=round_number,
            kind=artefact,
        )
        handle = open_result_stream(settings.data_dir, download)
    except DownloadsRevoked as error:
        # 410 Gone: the resource existed and is permanently unavailable to this
        # caller. The mini-program refreshes the order and shows this message.
        raise HTTPException(
            status_code=status.HTTP_410_GONE, detail=str(error)
        ) from None
    except ResultNotFound as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(error)
        ) from None
    except ResultDownloadError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="该批改结果文件不存在。",
        ) from None

    async def stream_chunks() -> AsyncIterator[bytes]:
        try:
            while True:
                chunk = await anyio.to_thread.run_sync(handle.read, CHUNK_BYTES)
                if not chunk:
                    break
                yield chunk
        finally:
            handle.close()

    return StreamingResponse(
        stream_chunks(),
        media_type=download.content_type,
        headers={
            "Content-Length": str(download.size_bytes),
            # The filename is server-generated, so it cannot inject headers.
            "Content-Disposition": (
                'attachment; filename="grading-report.pdf"; '
                f"filename*=UTF-8''{quote(download.filename)}"
            ),
            "X-Content-SHA256": download.sha256,
            # A revoked download must never be served from a cache.
            "Cache-Control": "no-store",
        },
    )
