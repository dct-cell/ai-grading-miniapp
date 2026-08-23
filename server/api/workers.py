from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, status

from server.api.dependencies import DatabaseSession
from server.api.worker_dependencies import CurrentWorker, SharedKeyGuard
from server.schemas.heartbeats import HeartbeatRenewalRequest
from server.schemas.workers import (
    WorkerHeartbeatResponse,
    WorkerRegistrationRequest,
    WorkerRegistrationResponse,
)
from server.services.leases import LeaseConflict, LeaseService
from server.services.workers import (
    HEARTBEAT_INTERVAL_SECONDS,
    LEASE_SECONDS,
    LONG_POLL_SECONDS,
    MINIMUM_WORKER_VERSION,
    RegistrationRequest,
    record_heartbeat,
    register_worker,
)


router = APIRouter(prefix="/worker/v1", tags=["worker-control-plane"])


@router.post(
    "/register",
    response_model=WorkerRegistrationResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[SharedKeyGuard],
)
def register(
    payload: WorkerRegistrationRequest,
    session: DatabaseSession,
) -> WorkerRegistrationResponse:
    """Allocate or recover the server-side identity for an installation."""
    worker = register_worker(
        session,
        RegistrationRequest(
            installation_id=payload.installation_id,
            device_name=payload.device_name,
            platform=payload.platform,
            architecture=payload.architecture,
            worker_version=payload.worker_version,
            codex_version=payload.codex_version,
            tex_version=payload.tex_version,
            capabilities=payload.capabilities,
        ),
    )
    return WorkerRegistrationResponse(
        worker_id=worker.worker_id,
        heartbeat_interval_seconds=HEARTBEAT_INTERVAL_SECONDS,
        lease_seconds=LEASE_SECONDS,
        long_poll_seconds=LONG_POLL_SECONDS,
        minimum_worker_version=MINIMUM_WORKER_VERSION,
    )


@router.post("/heartbeat", response_model=WorkerHeartbeatResponse)
def heartbeat(
    payload: HeartbeatRenewalRequest,
    worker: CurrentWorker,
    session: DatabaseSession,
    request: Request,
) -> WorkerHeartbeatResponse:
    """Refresh liveness, optionally renewing the held lease in the same call."""
    lease_expires_at = None
    if payload.job_id is not None and payload.lease_version is not None:
        service = LeaseService(request.app.state.session_factory)
        try:
            job = service.renew(
                job_id=payload.job_id,
                worker_id=worker.worker_id,
                lease_version=payload.lease_version,
                phase=payload.phase,
            )
        except LeaseConflict:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="批改任务不存在或租约已失效。",
            ) from None
        lease_expires_at = job.lease_expires_at

    record_heartbeat(session, worker)
    return WorkerHeartbeatResponse(
        worker_id=worker.worker_id,
        status=worker.status,
        current_job_id=worker.current_job_id,
        lease_expires_at=lease_expires_at,
        heartbeat_interval_seconds=HEARTBEAT_INTERVAL_SECONDS,
        lease_seconds=LEASE_SECONDS,
        long_poll_seconds=LONG_POLL_SECONDS,
    )
