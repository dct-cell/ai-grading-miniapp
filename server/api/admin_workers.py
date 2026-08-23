"""Admin Worker control and aftersales queue endpoints.

The refund *decisions* live in ``admin_refunds.py`` and go through
``RefundService``. This module only reads the queue and changes Worker status, so
there is still exactly one code path that moves money.
"""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel

from server.api.admin_dependencies import CurrentAdmin
from server.api.dependencies import DatabaseSession
from server.services.admin_workers import (
    DISABLE,
    DRAIN,
    ENABLE,
    ControlNotApplicable,
    UnknownWorker,
    apply_worker_control,
    list_aftersales,
    list_workers,
)


router = APIRouter(prefix="/admin/api/v1", tags=["admin-workers"])

_WORKER_NOT_FOUND = HTTPException(
    status_code=status.HTTP_404_NOT_FOUND,
    detail="Worker 不存在。",
)
_NOT_APPLICABLE = HTTPException(
    status_code=status.HTTP_409_CONFLICT,
    detail="该 Worker 已停用，请先恢复再执行此操作。",
)


class WorkerRowView(BaseModel):
    """Deliberately omits installation_id, which is an enrolment credential."""

    worker_id: str
    device_name: str
    platform: str
    architecture: str
    worker_version: str
    codex_version: str | None
    tex_version: str | None
    status: str
    current_job_id: str | None
    last_heartbeat_at: datetime
    capabilities: dict[str, object]
    active_job_state: str | None
    lease_expires_at: datetime | None


class WorkerListView(BaseModel):
    items: list[WorkerRowView]


class AftersalesRowView(BaseModel):
    refund_id: str
    order_id: str
    owner_public_id: str
    state: str
    source: str
    amount_cents: int
    order_state: str
    created_at: datetime


class AftersalesListView(BaseModel):
    items: list[AftersalesRowView]


def _view(row) -> WorkerRowView:
    return WorkerRowView(
        worker_id=row.worker_id,
        device_name=row.device_name,
        platform=row.platform,
        architecture=row.architecture,
        worker_version=row.worker_version,
        codex_version=row.codex_version,
        tex_version=row.tex_version,
        status=row.status,
        current_job_id=row.current_job_id,
        last_heartbeat_at=row.last_heartbeat_at,
        capabilities=row.capabilities,
        active_job_state=row.active_job_state,
        lease_expires_at=row.lease_expires_at,
    )


@router.get("/workers", response_model=WorkerListView)
def read_workers(admin: CurrentAdmin, session: DatabaseSession) -> WorkerListView:
    del admin
    return WorkerListView(items=[_view(row) for row in list_workers(session)])


def _control(
    session: DatabaseSession,
    *,
    worker_id: str,
    action: str,
    admin_id: str,
) -> WorkerRowView:
    try:
        return _view(
            apply_worker_control(
                session,
                worker_id=worker_id,
                action=action,
                admin_id=admin_id,
            )
        )
    except UnknownWorker:
        raise _WORKER_NOT_FOUND from None
    except ControlNotApplicable:
        raise _NOT_APPLICABLE from None


@router.post("/workers/{worker_id}/drain", response_model=WorkerRowView)
def drain_worker(
    worker_id: str,
    admin: CurrentAdmin,
    session: DatabaseSession,
) -> WorkerRowView:
    """Stop handing this Worker new jobs; let it finish the one it has."""
    return _control(session, worker_id=worker_id, action=DRAIN, admin_id=admin.id)


@router.post("/workers/{worker_id}/disable", response_model=WorkerRowView)
def disable_worker(
    worker_id: str,
    admin: CurrentAdmin,
    session: DatabaseSession,
) -> WorkerRowView:
    """Hard stop for new work. Does not silently cancel a running job."""
    return _control(session, worker_id=worker_id, action=DISABLE, admin_id=admin.id)


@router.post("/workers/{worker_id}/enable", response_model=WorkerRowView)
def enable_worker(
    worker_id: str,
    admin: CurrentAdmin,
    session: DatabaseSession,
) -> WorkerRowView:
    return _control(session, worker_id=worker_id, action=ENABLE, admin_id=admin.id)


@router.get("/aftersales", response_model=AftersalesListView)
def read_aftersales(
    admin: CurrentAdmin,
    session: DatabaseSession,
    state: str | None = None,
) -> AftersalesListView:
    del admin
    return AftersalesListView(
        items=[
            AftersalesRowView(
                refund_id=row.refund_id,
                order_id=row.order_id,
                owner_public_id=row.owner_public_id,
                state=row.state,
                source=row.source,
                amount_cents=row.amount_cents,
                order_state=row.order_state,
                created_at=row.created_at,
            )
            for row in list_aftersales(session, state=state)
        ]
    )
