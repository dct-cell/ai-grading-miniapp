"""Admin Worker controls and the aftersales review queue.

The controls only ever change ``workers.status``. They never touch
``grading_jobs``: a Worker that is draining or disabled keeps the job it holds,
keeps its lease, and is allowed to finish and deliver. Cancelling in-flight work
would discard a grading run the user has already paid for, and the lease
recycler — not an admin action — is what decides when an abandoned job returns to
the queue.

Nothing here executes a refund. Approval routes through ``RefundService`` so that
one code path, with one ``external_refund_id`` per settled refund, remains the
only way money moves.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from server.models import AuditLog, GradingJob, Order, Payment, QuoteSession, Refund, User, Worker
from server.services.workers import WorkerStatus


#: Admin-driven status transitions and the audit action each one records.
DRAIN = "drain"
DISABLE = "disable"
ENABLE = "enable"

_TARGET_STATUS = {
    DRAIN: WorkerStatus.DRAINING,
    DISABLE: WorkerStatus.DISABLED,
    # Enabling returns a Worker to service. Its next heartbeat re-establishes
    # whether it is really reachable, so ONLINE is the right optimistic default
    # rather than SUSPECTED_OFFLINE.
    ENABLE: WorkerStatus.ONLINE,
}


class UnknownWorker(LookupError):
    """No such Worker row."""


class ControlNotApplicable(ValueError):
    """The Worker's current status does not allow this transition."""


@dataclass(frozen=True)
class WorkerView:
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


def list_workers(session: Session) -> tuple[WorkerView, ...]:
    """Report the operational facts an operator needs to triage a Worker.

    ``installation_id`` is deliberately omitted: it is half of the Worker's
    enrolment credential and appears only in the installer-written local config.
    """
    workers = session.scalars(select(Worker).order_by(Worker.created_at)).all()
    jobs = {
        job.id: job
        for job in session.scalars(
            select(GradingJob).where(
                GradingJob.id.in_(
                    [worker.current_job_id for worker in workers if worker.current_job_id]
                )
            )
        ).all()
    }
    return tuple(
        WorkerView(
            worker_id=worker.worker_id,
            device_name=worker.device_name,
            platform=worker.platform,
            architecture=worker.architecture,
            worker_version=worker.worker_version,
            codex_version=worker.codex_version,
            tex_version=worker.tex_version,
            status=worker.status,
            current_job_id=worker.current_job_id,
            last_heartbeat_at=worker.last_heartbeat_at,
            capabilities=worker.capabilities,
            active_job_state=(
                jobs[worker.current_job_id].state
                if worker.current_job_id in jobs
                else None
            ),
            lease_expires_at=(
                jobs[worker.current_job_id].lease_expires_at
                if worker.current_job_id in jobs
                else None
            ),
        )
        for worker in workers
    )


def apply_worker_control(
    session: Session,
    *,
    worker_id: str,
    action: str,
    admin_id: str,
) -> WorkerView:
    """Change a Worker's status and record who did it.

    Only ``status`` is written. ``current_job_id`` and the job row are left
    exactly as they are, which is what makes "drain does not cancel" true rather
    than merely intended.
    """
    worker = session.get(Worker, worker_id)
    if worker is None:
        raise UnknownWorker(worker_id)

    # Draining is a planned wind-down and disabling is a hard stop, so drain must
    # not quietly downgrade a deliberate disable. Both already withhold leases,
    # so this protects the operator's intent and the panel's honesty rather than
    # an authorisation boundary.
    if action == DRAIN and worker.status == WorkerStatus.DISABLED:
        raise ControlNotApplicable(worker_id)

    worker.status = _TARGET_STATUS[action]
    session.add(
        AuditLog(
            actor_type="admin",
            actor_id=admin_id,
            action=f"worker.{action}",
            target_type="worker",
            target_id=worker_id,
            details={"status": worker.status},
        )
    )
    session.commit()

    return next(
        view for view in list_workers(session) if view.worker_id == worker_id
    )


@dataclass(frozen=True)
class AftersalesRow:
    refund_id: str
    order_id: str
    owner_public_id: str
    state: str
    source: str
    amount_cents: int
    order_state: str
    created_at: datetime


def list_aftersales(
    session: Session,
    *,
    state: str | None = None,
) -> tuple[AftersalesRow, ...]:
    """The refund review queue, newest first.

    Joins through payment to quote to user so a row can name the affected user by
    public id. No file columns are selected, so no storage path can reach the
    response.
    """
    statement = (
        select(Refund, Order.id, Order.state, User.public_id)
        .join(Payment, Payment.id == Refund.payment_id)
        .join(QuoteSession, QuoteSession.id == Payment.quote_session_id)
        .join(User, User.id == QuoteSession.owner_user_id)
        .join(Order, Order.quote_session_id == QuoteSession.id)
        .order_by(Refund.created_at.desc())
    )
    if state:
        statement = statement.where(Refund.state == state)

    return tuple(
        AftersalesRow(
            refund_id=refund.id,
            order_id=order_id,
            owner_public_id=public_id,
            state=refund.state,
            source=refund.source,
            amount_cents=refund.amount_cents,
            order_state=order_state,
            created_at=refund.created_at,
        )
        for refund, order_id, order_state, public_id in session.execute(statement).all()
    )


__all__ = [
    "AftersalesRow",
    "ControlNotApplicable",
    "DISABLE",
    "DRAIN",
    "ENABLE",
    "UnknownWorker",
    "WorkerView",
    "apply_worker_control",
    "list_aftersales",
    "list_workers",
]
