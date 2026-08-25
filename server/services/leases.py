from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import Select, and_, or_, select, update
from sqlalchemy.orm import Session, sessionmaker

from server.db_locking import lock_row
from server.domain.states import (
    JobState,
    OrderState,
    require_job_transition,
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
from server.services.workers import (
    ACK_SECONDS,
    LEASE_SECONDS,
    NON_LEASABLE_STATUSES,
    OFFLINE_AFTER_SECONDS,
    WorkerStatus,
)


ACTIVE_JOB_STATES = frozenset(
    {JobState.LEASED, JobState.RUNNING, JobState.UPLOADING}
)
STARTED_JOB_STATES = frozenset({JobState.RUNNING, JobState.UPLOADING})
CANCELLABLE_JOB_STATES = frozenset(
    {JobState.QUEUED, JobState.LEASED, JobState.RUNNING, JobState.UPLOADING}
)

_DOWNLOAD_TOKEN_BYTES = 32


class LeaseConflict(ValueError):
    """The caller does not hold the lease it is trying to act on."""


@dataclass(frozen=True)
class BundleFile:
    file_id: str
    kind: str
    sha256: str
    size_bytes: int
    download_token: str


@dataclass(frozen=True)
class TaskBundle:
    job_id: str
    order_id: str
    round_number: int
    lease_version: int
    service_tier: str
    grading_standard: str
    league_scope: str | None
    note: str
    page_count: int
    source_file: BundleFile
    reference_file: BundleFile | None
    ack_deadline: datetime
    lease_expires_at: datetime


def _issue_download_token() -> str:
    return secrets.token_urlsafe(_DOWNLOAD_TOKEN_BYTES)


def cancel_job(
    session: Session,
    job: GradingJob,
    *,
    event_type: str = "cancelled",
) -> bool:
    """Cancel one unfinished job and release every capability it granted.

    The caller owns the surrounding transaction and must have locked ``job``.
    Keeping this operation beside the lease writer prevents refund and repair
    paths from clearing only part of the fencing state.
    """
    if JobState(job.state) not in CANCELLABLE_JOB_STATES:
        return False
    previous_worker_id = job.worker_id
    require_job_transition(JobState(job.state), JobState.CANCELLED)
    job.state = JobState.CANCELLED
    job.ack_deadline = None
    job.lease_expires_at = None
    job.bundle_download_tokens = None
    session.add(job)
    if previous_worker_id is not None:
        session.execute(
            update(Worker)
            .where(
                Worker.worker_id == previous_worker_id,
                Worker.current_job_id == job.id,
            )
            .values(current_job_id=None)
        )
        session.add(
            WorkerEvent(
                worker_id=previous_worker_id,
                job_id=job.id,
                event_type=event_type,
                details={"lease_version": job.lease_version},
            )
        )
    return True


class LeaseService:
    """Owns every write to a grading job's lease fields.

    lease_version is a fencing token: every successful claim increases it, so a
    Worker holding an older value can never write to the job again.
    """

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    @staticmethod
    def _claim_statement() -> Select:
        return (
            select(GradingJob)
            .where(GradingJob.state == JobState.QUEUED)
            .order_by(GradingJob.queued_at, GradingJob.id)
            .with_for_update(skip_locked=True)
            .limit(1)
        )

    def _holds_active_job(self, session: Session, worker_id: str) -> bool:
        held = session.scalar(
            select(GradingJob.id).where(
                GradingJob.worker_id == worker_id,
                GradingJob.state.in_(ACTIVE_JOB_STATES),
            )
        )
        return held is not None

    def try_lease(self, worker_id: str) -> TaskBundle | None:
        """Claim at most one queued job for a Worker, or return None.

        The whole claim runs in one transaction: the active-job check, the row
        lock, the state transition and the lease_version bump commit together
        so two Workers can never hold the same job.
        """
        # Keep the indexed claim query exactly on (state, queued_at, id).
        # A bounded loop repairs legacy poisoned head rows without adding an
        # Order join that makes MySQL abandon the SKIP LOCKED claim index.
        for _ in range(16):
            with self._session_factory() as session:
                worker = lock_row(session, Worker, worker_id)
                if worker is None or worker.status in NON_LEASABLE_STATUSES:
                    return None
                if self._holds_active_job(session, worker_id):
                    return None

                job = session.scalars(self._claim_statement()).first()
                if job is None:
                    return None

                queued = (
                    OrderState.V1_QUEUED
                    if job.round_number == 1
                    else OrderState.V2_QUEUED
                )
                running = (
                    OrderState.V1_RUNNING
                    if job.round_number == 1
                    else OrderState.V2_RUNNING
                )
                order = lock_row(session, Order, job.order_id)
                if order is None or OrderState(order.state) is not queued:
                    cancel_job(
                        session,
                        job,
                        event_type="nonrunnable_job_skipped",
                    )
                    session.commit()
                    continue

                moved = session.execute(
                    update(Order)
                    .where(Order.id == order.id, Order.state == queued)
                    .values(state=running)
                )
                if moved.rowcount != 1:
                    session.rollback()
                    continue
                session.expire(order)

                now = datetime.now(timezone.utc)
                require_job_transition(JobState(job.state), JobState.LEASED)
                job.state = JobState.LEASED
                job.current_phase = None
                job.worker_id = worker_id
                job.lease_version = job.lease_version + 1
                job.ack_deadline = now + timedelta(seconds=ACK_SECONDS)
                job.lease_expires_at = now + timedelta(seconds=LEASE_SECONDS)
                job.attempt_count = job.attempt_count + 1
                session.add(job)

                worker.current_job_id = job.id
                worker.last_heartbeat_at = now
                session.add(worker)

                bundle = self._build_bundle(session, job)
                job.bundle_download_tokens = {
                    "source": bundle.source_file.download_token,
                    "reference": (
                        bundle.reference_file.download_token
                        if bundle.reference_file is not None
                        else None
                    ),
                }
                session.add(job)
                session.add(
                    WorkerEvent(
                        worker_id=worker_id,
                        job_id=job.id,
                        event_type="leased",
                        details={"lease_version": job.lease_version},
                    )
                )
                session.commit()
                return bundle
        return None

    def _build_bundle(self, session: Session, job: GradingJob) -> TaskBundle:
        order = session.get(Order, job.order_id)
        quote = session.get(QuoteSession, order.quote_session_id)
        round_record = session.scalar(
            select(GradingRound).where(
                GradingRound.order_id == job.order_id,
                GradingRound.round_number == job.round_number,
            )
        )

        def describe(file_id: str | None) -> BundleFile | None:
            if file_id is None:
                return None
            record = session.get(FileObject, file_id)
            if record is None:
                return None
            return BundleFile(
                file_id=record.id,
                kind=record.kind,
                sha256=record.sha256,
                size_bytes=record.size_bytes,
                download_token=_issue_download_token(),
            )

        return TaskBundle(
            job_id=job.id,
            order_id=job.order_id,
            round_number=job.round_number,
            lease_version=job.lease_version,
            service_tier=round_record.service_tier,
            grading_standard=round_record.grading_standard,
            league_scope=round_record.league_scope,
            note=round_record.note,
            page_count=quote.page_count,
            source_file=describe(quote.source_file_id),
            reference_file=describe(quote.reference_file_id),
            ack_deadline=job.ack_deadline,
            lease_expires_at=job.lease_expires_at,
        )

    def _fenced_job(
        self,
        session: Session,
        *,
        job_id: str,
        worker_id: str,
        lease_version: int,
        allowed_states: frozenset[str],
    ) -> GradingJob:
        """Load a job only when the caller still holds its lease.

        Every write path funnels through here so a wrong Worker or a stale
        fencing token can never mutate somebody else's job.
        """
        job = lock_row(session, GradingJob, job_id)
        if job is None:
            raise LeaseConflict("批改任务不存在或租约已失效。")
        if job.worker_id != worker_id:
            raise LeaseConflict("批改任务不存在或租约已失效。")
        if job.lease_version != lease_version:
            raise LeaseConflict("批改任务不存在或租约已失效。")
        if job.state not in allowed_states:
            raise LeaseConflict("批改任务不存在或租约已失效。")
        return job

    def acknowledge(
        self,
        *,
        job_id: str,
        worker_id: str,
        lease_version: int,
    ) -> GradingJob:
        """Confirm a Worker really started a leased job.

        Requires worker_id, lease_version, state and an unexpired ack_deadline
        to all match; anything else is a conflict.
        """
        with self._session_factory() as session:
            job = lock_row(session, GradingJob, job_id)
            if (
                job is None
                or job.worker_id != worker_id
                or job.lease_version != lease_version
            ):
                raise LeaseConflict("批改任务不存在或租约已失效。")
            # The Worker may have ACKed successfully while the HTTP response
            # was lost.  Returning the same fenced state makes that retry safe.
            if job.state == JobState.RUNNING:
                return job
            if job.state != JobState.LEASED:
                raise LeaseConflict("批改任务不存在或租约已失效。")
            now = datetime.now(timezone.utc)
            if job.ack_deadline is None or job.ack_deadline <= now:
                raise LeaseConflict("批改任务不存在或租约已失效。")

            require_job_transition(JobState(job.state), JobState.RUNNING)
            job.state = JobState.RUNNING
            job.ack_deadline = None
            job.lease_expires_at = now + timedelta(seconds=LEASE_SECONDS)
            session.add(job)
            session.add(
                WorkerEvent(
                    worker_id=worker_id,
                    job_id=job.id,
                    event_type="acked",
                    details={"lease_version": job.lease_version},
                )
            )
            session.commit()
            return job

    def renew(
        self,
        *,
        job_id: str,
        worker_id: str,
        lease_version: int,
        phase: str | None = None,
    ) -> GradingJob:
        """Extend a started lease using server time only.

        The deadline is always computed here; a client-supplied expiry is never
        trusted, and only RUNNING or UPLOADING may be renewed.
        """
        with self._session_factory() as session:
            job = self._fenced_job(
                session,
                job_id=job_id,
                worker_id=worker_id,
                lease_version=lease_version,
                allowed_states=STARTED_JOB_STATES,
            )
            now = datetime.now(timezone.utc)
            job.lease_expires_at = now + timedelta(seconds=LEASE_SECONDS)
            if phase is not None:
                job.current_phase = phase
            session.add(job)

            worker = session.get(Worker, worker_id)
            if worker is not None:
                worker.last_heartbeat_at = now
                worker.current_job_id = job.id
                if worker.status == WorkerStatus.SUSPECTED_OFFLINE:
                    worker.status = WorkerStatus.ONLINE
                session.add(worker)
            if phase is not None:
                session.add(
                    WorkerEvent(
                        worker_id=worker_id,
                        job_id=job.id,
                        event_type="renewed",
                        details={"lease_version": job.lease_version, "phase": phase},
                    )
                )
            session.commit()
            return job

    def fail_job(
        self,
        *,
        job_id: str,
        worker_id: str,
        lease_version: int,
        code: str,
        message: str,
    ) -> GradingJob:
        """Record a deterministic Worker failure exactly once.

        The order deliberately remains in its running state: the established
        product policy routes these cases to Admin technical refund rather than
        automatically re-running an expensive Codex session.
        """
        with self._session_factory() as session:
            job = lock_row(session, GradingJob, job_id)
            if (
                job is None
                or job.worker_id != worker_id
                or job.lease_version != lease_version
            ):
                raise LeaseConflict("批改任务不存在或租约已失效。")
            if job.state == JobState.WORKER_EXCEPTION:
                return job
            if job.state not in STARTED_JOB_STATES:
                raise LeaseConflict("批改任务不存在或租约已失效。")

            require_job_transition(
                JobState(job.state),
                JobState.WORKER_EXCEPTION,
            )
            job.state = JobState.WORKER_EXCEPTION
            job.ack_deadline = None
            job.lease_expires_at = None
            job.bundle_download_tokens = None
            session.add(job)
            self._clear_worker_job(session, worker_id, job.id)
            session.add(
                WorkerEvent(
                    worker_id=worker_id,
                    job_id=job.id,
                    event_type="job_failed",
                    details={
                        "lease_version": job.lease_version,
                        "code": code,
                        "message": message,
                    },
                )
            )
            session.commit()
            return job

    def _locked_candidates(self, session: Session, statement: Select):
        """Re-read candidate jobs under a row lock where the backend supports it.

        The recyclers decide from a snapshot and then write. Without a lock a
        job can ACK or commit in between, and the blind write would clobber a
        legitimate transition: a started job pushed back to QUEUED (duplicate
        execution) or a delivered job marked WORKER_EXCEPTION.
        """
        if session.get_bind().dialect.name != "sqlite":
            statement = statement.with_for_update()
        return session.scalars(statement).all()

    def release_unacknowledged(self, *, now: datetime | None = None) -> int:
        """Return only never-acknowledged leases to the queue.

        A job that reached RUNNING or UPLOADING has started real work on a
        Worker. Requeueing it would duplicate execution, so it is left to
        expire_started_leases instead.
        """
        moment = now or datetime.now(timezone.utc)
        released = 0
        with self._session_factory() as session:
            stale = self._locked_candidates(
                session,
                select(GradingJob).where(
                    GradingJob.state == JobState.LEASED,
                    GradingJob.ack_deadline.is_not(None),
                    GradingJob.ack_deadline <= moment,
                ),
            )
            for job in stale:
                # Re-check under the lock: the snapshot may be stale.
                if job.state != JobState.LEASED:
                    continue
                if job.ack_deadline is None or job.ack_deadline > moment:
                    continue
                previous_worker_id = job.worker_id
                require_job_transition(JobState(job.state), JobState.QUEUED)
                job.state = JobState.QUEUED
                job.worker_id = None
                job.ack_deadline = None
                job.lease_expires_at = None
                job.queued_at = moment
                session.add(job)
                self._clear_worker_job(session, previous_worker_id, job.id)
                self._revert_order_to_queued(session, job)
                if previous_worker_id is not None:
                    session.add(
                        WorkerEvent(
                            worker_id=previous_worker_id,
                            job_id=job.id,
                            event_type="ack_timeout_requeued",
                            details={"lease_version": job.lease_version},
                        )
                    )
                released += 1
            session.commit()
        return released

    def expire_started_leases(self, *, now: datetime | None = None) -> int:
        """Fail started jobs whose lease expired; never requeue them."""
        moment = now or datetime.now(timezone.utc)
        expired = 0
        with self._session_factory() as session:
            stale = self._locked_candidates(
                session,
                select(GradingJob).where(
                    GradingJob.state.in_(STARTED_JOB_STATES),
                    GradingJob.lease_expires_at.is_not(None),
                    GradingJob.lease_expires_at <= moment,
                ),
            )
            for job in stale:
                # Re-check under the lock so a job that just succeeded is not
                # overwritten with WORKER_EXCEPTION after delivery.
                if job.state not in STARTED_JOB_STATES:
                    continue
                if job.lease_expires_at is None or job.lease_expires_at > moment:
                    continue
                previous_worker_id = job.worker_id
                require_job_transition(JobState(job.state), JobState.WORKER_EXCEPTION)
                job.state = JobState.WORKER_EXCEPTION
                session.add(job)
                self._clear_worker_job(session, previous_worker_id, job.id)
                if previous_worker_id is not None:
                    session.add(
                        WorkerEvent(
                            worker_id=previous_worker_id,
                            job_id=job.id,
                            event_type="lease_expired",
                            details={"lease_version": job.lease_version},
                        )
                    )
                expired += 1
            session.commit()
        return expired

    def mark_suspected_offline(self, *, now: datetime | None = None) -> int:
        """Flag Workers that stopped heartbeating, without touching their jobs."""
        moment = now or datetime.now(timezone.utc)
        threshold = moment - timedelta(seconds=OFFLINE_AFTER_SECONDS)
        marked = 0
        with self._session_factory() as session:
            candidates = session.scalars(
                select(Worker).where(
                    Worker.status == WorkerStatus.ONLINE,
                    Worker.last_heartbeat_at <= threshold,
                )
            ).all()
            for worker in candidates:
                worker.status = WorkerStatus.SUSPECTED_OFFLINE
                session.add(worker)
                marked += 1
            session.commit()
        return marked

    def reconcile_nonrunnable_jobs(
        self,
        *,
        now: datetime | None = None,
        limit: int = 200,
    ) -> int:
        """Cancel legacy jobs whose order can no longer run that round.

        New refunds cancel their job transactionally.  This bounded repair is
        deliberately retained for rows written by older releases or an
        interrupted historical transaction, so one poisoned row can never sit
        at the head of the queue forever.
        """
        del now  # Kept for the Scheduler task signature.
        repaired = 0
        with self._session_factory() as session:
            statement = (
                select(GradingJob)
                .join(Order, Order.id == GradingJob.order_id)
                .where(
                    GradingJob.state.in_(CANCELLABLE_JOB_STATES),
                    ~or_(
                        and_(
                            GradingJob.round_number == 1,
                            GradingJob.state == JobState.QUEUED,
                            Order.state == OrderState.V1_QUEUED,
                        ),
                        and_(
                            GradingJob.round_number == 2,
                            GradingJob.state == JobState.QUEUED,
                            Order.state == OrderState.V2_QUEUED,
                        ),
                        and_(
                            GradingJob.round_number == 1,
                            GradingJob.state.in_(ACTIVE_JOB_STATES),
                            Order.state == OrderState.V1_RUNNING,
                        ),
                        and_(
                            GradingJob.round_number == 2,
                            GradingJob.state.in_(ACTIVE_JOB_STATES),
                            Order.state == OrderState.V2_RUNNING,
                        ),
                    ),
                )
                .order_by(GradingJob.queued_at, GradingJob.id)
                .limit(max(1, limit))
            )
            candidate_ids = list(
                session.scalars(statement.with_only_columns(GradingJob.id)).all()
            )
            for job_id in candidate_ids:
                job = lock_row(session, GradingJob, job_id)
                if job is None or JobState(job.state) not in CANCELLABLE_JOB_STATES:
                    continue
                order = lock_row(session, Order, job.order_id)
                if order is not None and self._job_matches_order(job, order):
                    continue
                if cancel_job(session, job, event_type="order_state_reconciled"):
                    repaired += 1
            session.commit()
        return repaired

    @staticmethod
    def _job_matches_order(job: GradingJob, order: Order) -> bool:
        expected = (
            OrderState.V1_QUEUED
            if job.round_number == 1 and job.state == JobState.QUEUED
            else OrderState.V2_QUEUED
            if job.round_number == 2 and job.state == JobState.QUEUED
            else OrderState.V1_RUNNING
            if job.round_number == 1 and job.state in ACTIVE_JOB_STATES
            else OrderState.V2_RUNNING
            if job.round_number == 2 and job.state in ACTIVE_JOB_STATES
            else None
        )
        return expected is not None and OrderState(order.state) is expected

    @staticmethod
    def _clear_worker_job(
        session: Session,
        worker_id: str | None,
        job_id: str,
    ) -> None:
        """Release a Worker's slot only when it still points at this job."""
        if worker_id is None:
            return
        worker = session.get(Worker, worker_id)
        if worker is not None and worker.current_job_id == job_id:
            worker.current_job_id = None
            session.add(worker)

    @staticmethod
    def _revert_order_to_queued(session: Session, job: GradingJob) -> None:
        queued = (
            OrderState.V1_QUEUED if job.round_number == 1 else OrderState.V2_QUEUED
        )
        running = (
            OrderState.V1_RUNNING if job.round_number == 1 else OrderState.V2_RUNNING
        )
        # A refund may win while the ACK deadline is expiring.  Rewind only the
        # exact running state created by this lease; never overwrite a newer
        # refund/terminal decision from another transaction.
        session.execute(
            update(Order)
            .where(Order.id == job.order_id, Order.state == running)
            .values(state=queued)
        )

    def get(self, job_id: str) -> GradingJob | None:
        with self._session_factory() as session:
            return session.get(GradingJob, job_id)
