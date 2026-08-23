from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from server.models import Worker


HEARTBEAT_INTERVAL_SECONDS = 20
LEASE_SECONDS = 120
LONG_POLL_SECONDS = 25
ACK_SECONDS = 30
OFFLINE_AFTER_SECONDS = 60
MINIMUM_WORKER_VERSION = "3.0.0"


class WorkerStatus:
    ONLINE = "online"
    SUSPECTED_OFFLINE = "suspected_offline"
    #: An operator asked this Worker to stop taking new work. It keeps the job it
    #: already holds and may finish and deliver it; only the *next* lease is
    #: withheld. Killing a running grading run would throw away money and time
    #: the user has already spent, so draining never cancels work in flight.
    DRAINING = "draining"
    DISABLED = "disabled"


#: Every status a Worker row may hold. Anything that reports Workers by status
#: should enumerate this rather than listing names by hand, so a newly added
#: status cannot make those Workers silently disappear from a report.
ALL_WORKER_STATUSES: tuple[str, ...] = (
    WorkerStatus.ONLINE,
    WorkerStatus.SUSPECTED_OFFLINE,
    WorkerStatus.DRAINING,
    WorkerStatus.DISABLED,
)

#: Statuses that must not receive a new lease. DRAINING and DISABLED differ in
#: intent — one is a planned wind-down, the other a hard stop — but both have to
#: stop the queue handing over work, so the claim path tests membership here
#: rather than comparing against DISABLED alone.
NON_LEASABLE_STATUSES = frozenset({WorkerStatus.DRAINING, WorkerStatus.DISABLED})


class WorkerDisabled(PermissionError):
    """The Worker exists but an operator switched it off."""


@dataclass(frozen=True)
class RegistrationRequest:
    installation_id: str
    device_name: str
    platform: str
    architecture: str
    worker_version: str
    codex_version: str | None = None
    tex_version: str | None = None
    capabilities: dict[str, object] | None = None


def verify_shared_key(provided: str, expected: str) -> bool:
    """Compare the Worker shared key without leaking its length or content.

    Hashing first gives both operands a fixed width, so compare_digest cannot
    reveal the shared key through a length-dependent or early-exit timing
    difference the way `provided == expected` would.
    """
    provided_hash = hashlib.sha256(provided.encode("utf-8")).digest()
    expected_hash = hashlib.sha256(expected.encode("utf-8")).digest()
    return hmac.compare_digest(provided_hash, expected_hash)


def _apply_reported_environment(
    worker: Worker,
    request: RegistrationRequest,
    now: datetime,
) -> None:
    worker.device_name = request.device_name
    worker.platform = request.platform
    worker.architecture = request.architecture
    worker.worker_version = request.worker_version
    worker.codex_version = request.codex_version
    worker.tex_version = request.tex_version
    worker.capabilities = dict(request.capabilities or {})
    worker.last_heartbeat_at = now


def register_worker(session: Session, request: RegistrationRequest) -> Worker:
    """Return the stable Worker identity for an installation.

    The worker_id is always allocated by the server. Re-registration refreshes
    the reported runtime facts but never re-enables a disabled Worker; only an
    operator may do that.
    """
    now = datetime.now(timezone.utc)
    existing = session.scalar(
        select(Worker).where(Worker.installation_id == request.installation_id)
    )
    if existing is not None:
        _apply_reported_environment(existing, request, now)
        session.add(existing)
        session.commit()
        return existing

    savepoint = session.begin_nested()
    try:
        worker = Worker(
            installation_id=request.installation_id,
            device_name=request.device_name,
            platform=request.platform,
            architecture=request.architecture,
            worker_version=request.worker_version,
            codex_version=request.codex_version,
            tex_version=request.tex_version,
            capabilities=dict(request.capabilities or {}),
            status=WorkerStatus.ONLINE,
            last_heartbeat_at=now,
        )
        session.add(worker)
        savepoint.commit()
        session.commit()
        return worker
    except IntegrityError:
        # A concurrent first registration may have won the installation_id
        # unique constraint; adopt its worker_id instead of surfacing a 500.
        savepoint.rollback()
        winner = session.scalar(
            select(Worker).where(Worker.installation_id == request.installation_id)
        )
        if winner is None:
            # The violation was something else entirely, so there is no winner
            # to adopt. Re-raise the real error rather than a bare `raise`,
            # which outside the except block would become a RuntimeError.
            raise

    _apply_reported_environment(winner, request, now)
    session.add(winner)
    session.commit()
    return winner


def authenticate_worker(
    session: Session,
    *,
    provided_key: str | None,
    worker_id: str | None,
    expected_key: str,
) -> Worker | None:
    """Resolve the Worker for a shared key plus worker ID, or None.

    Returning None means "unauthenticated"; a disabled Worker authenticates
    successfully and raises WorkerDisabled so the caller can answer 403.
    """
    if not provided_key or not worker_id:
        return None
    if not verify_shared_key(provided_key, expected_key):
        return None
    worker = session.get(Worker, worker_id)
    if worker is None:
        return None
    if worker.status == WorkerStatus.DISABLED:
        raise WorkerDisabled(worker_id)
    return worker


def record_heartbeat(session: Session, worker: Worker) -> Worker:
    """Refresh liveness from server time and clear a suspected outage."""
    worker.last_heartbeat_at = datetime.now(timezone.utc)
    if worker.status == WorkerStatus.SUSPECTED_OFFLINE:
        worker.status = WorkerStatus.ONLINE
    session.add(worker)
    session.commit()
    return worker
