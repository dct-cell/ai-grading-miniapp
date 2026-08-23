from __future__ import annotations

from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from server.api.dependencies import get_session, get_settings
from server.config import ServerSettings
from server.models import Worker
from server.services.workers import WorkerDisabled, authenticate_worker, verify_shared_key


_WORKER_UNAUTHORIZED = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="Worker 认证失败。",
    headers={"WWW-Authenticate": "Bearer"},
)
_WORKER_FORBIDDEN = HTTPException(
    status_code=status.HTTP_403_FORBIDDEN,
    detail="该 Worker 已被停用。",
)


def _shared_key(request: Request) -> str | None:
    """Read the Worker shared key from the Authorization header.

    This is a separate authentication domain from the mini-program: a session
    token presented here is only ever compared against the shared key, so it
    can never authenticate a Worker.
    """
    header = request.headers.get("Authorization")
    if header is None:
        return None
    scheme, _, value = header.partition(" ")
    if scheme.lower() != "bearer":
        return None
    return value.strip() or None


def require_shared_key(
    request: Request,
    settings: Annotated[ServerSettings, Depends(get_settings)],
) -> None:
    provided = _shared_key(request)
    if provided is None or not verify_shared_key(provided, settings.worker_shared_key):
        raise _WORKER_UNAUTHORIZED


def current_worker(
    request: Request,
    session: Annotated[Session, Depends(get_session)],
    settings: Annotated[ServerSettings, Depends(get_settings)],
) -> Worker:
    try:
        worker = authenticate_worker(
            session,
            provided_key=_shared_key(request),
            worker_id=request.headers.get("X-Worker-ID"),
            expected_key=settings.worker_shared_key,
        )
    except WorkerDisabled:
        raise _WORKER_FORBIDDEN from None
    if worker is None:
        raise _WORKER_UNAUTHORIZED
    return worker


SharedKeyGuard = Depends(require_shared_key)
CurrentWorker = Annotated[Worker, Depends(current_worker)]
