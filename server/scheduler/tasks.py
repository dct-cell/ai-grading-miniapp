"""Time-triggered lifecycle transitions.

One scheduler process owns every deadline in the system: acceptance windows,
lease recycling, file expiry and refund reconciliation. Nothing here is
triggered by a user request.

Two rules shape all of it:

**Idempotence.** The loop runs continuously, a deploy can briefly overlap two
processes, and an operator may run a task by hand. Every task therefore selects
only rows that still need work, guards each write on the state it observed, and
reports how many rows it actually changed.

**Bounded batches.** A backlog is drained a batch at a time rather than loaded
into memory, so a long outage cannot turn the first cycle after recovery into an
unbounded transaction.

Ordering within a cycle is deliberate: leases are recycled before acceptance is
swept, so a job that just failed is reflected before deadlines are judged.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable, Final

from sqlalchemy import delete, or_, select, union_all, update
from sqlalchemy.orm import Session, sessionmaker

from server.adapters.files import FileStorageError, LocalFileStore
from server.adapters.payments import PaymentGateway
from server.config import ServerSettings
from server.domain.states import OrderState, require_order_transition
from server.models import (
    AdminSession,
    FileObject,
    GradingRound,
    MiniappSession,
    Order,
    QuoteSession,
    Refund,
    WorkerEvent,
)
from server.services.files import FileState
from server.services.leases import LeaseService
from server.services.refunds import RefundService, RefundState


DEFAULT_BATCH_SIZE: Final[int] = 200

#: How long a finished order's artefacts are kept before collection.
ORDER_FILE_RETENTION = timedelta(days=30)
SESSION_ROW_RETENTION = timedelta(days=30)
WORKER_EVENT_RETENTION = timedelta(days=90)
PART_FILE_RETENTION = timedelta(hours=2)

#: Orders whose acceptance deadline the scheduler may act on. REFUND_PENDING is
#: deliberately absent: a refund decision is in flight and must not be
#: overridden by an automatic acceptance.
AUTO_ACCEPTABLE_STATES: Final[frozenset[str]] = frozenset(
    {OrderState.V1_DELIVERED, OrderState.V2_DELIVERED}
)

#: Orders that will never produce more work, so their files can be collected.
FINISHED_ORDER_STATES: Final[frozenset[str]] = frozenset(
    {OrderState.ACCEPTED, OrderState.REFUNDED}
)


@dataclass
class TaskReport:
    """What one task run actually did.

    ``affected`` counts rows changed, so a second run of an idempotent task
    reports zero — which is what the tests assert on.
    """

    name: str
    affected: int = 0
    skipped: bool = False
    failed: bool = False
    error: str | None = None


def _after_candidate_select(task_name: str) -> None:
    """Seam for interleaving a competing write in tests.

    Production behaviour is a no-op. Tests monkeypatch this to run a user
    request in the window between a task's SELECT and its UPDATE, proving the
    compare-and-set — not luck — is what stops the scheduler from clobbering a
    decision the user just made.
    """


class SchedulerTasks:
    """The full set of periodic maintenance tasks."""

    TASK_NAMES: Final[tuple[str, ...]] = (
        "release_unacknowledged_leases",
        "mark_expired_running_leases",
        "reconcile_nonrunnable_jobs",
        "mark_suspected_offline",
        "auto_accept_expired_orders",
        "delete_expired_quotes",
        "delete_expired_order_files",
        "delete_expired_temporary_files",
        "delete_stale_part_files",
        "delete_expired_sessions_and_events",
        "retry_failed_refund_queries",
        "verify_backup_freshness",
    )

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        settings: ServerSettings,
        gateway: PaymentGateway,
        batch_size: int = DEFAULT_BATCH_SIZE,
    ) -> None:
        self._session_factory = session_factory
        self._settings = settings
        self._store = LocalFileStore(settings.data_dir)
        self._leases = LeaseService(session_factory)
        self._refunds = RefundService(session_factory, gateway)
        self.batch_size = batch_size
        self._last_success: dict[str, datetime] = {}

    # -- bookkeeping ---------------------------------------------------------

    def last_success_at(self, name: str) -> datetime | None:
        return self._last_success.get(name)

    def run_due(self, *, now: datetime | None = None) -> dict[str, TaskReport]:
        """Run every task once, isolating failures.

        A task that raises must not prevent the others from running: a bug in
        file cleanup should never stop refunds from being reconciled.
        """
        moment = now or datetime.now(timezone.utc)
        reports: dict[str, TaskReport] = {}
        for name in self.TASK_NAMES:
            task: Callable[..., TaskReport] = getattr(self, name)
            try:
                report = task(now=moment)
            except Exception as error:  # noqa: BLE001 - isolation is the point
                reports[name] = TaskReport(
                    name=name, failed=True, error=type(error).__name__
                )
                continue
            reports[name] = report
            if not report.failed:
                self._last_success[name] = moment
        return reports

    # -- lease recycling -----------------------------------------------------

    def release_unacknowledged_leases(
        self, *, now: datetime | None = None
    ) -> TaskReport:
        """Return never-acknowledged leases to the queue.

        Delegates to LeaseService so the fencing rules and the locked re-read
        stay in one place; the scheduler must never write lease fields itself.
        """
        released = self._leases.release_unacknowledged(now=now)
        return TaskReport(
            name="release_unacknowledged_leases", affected=released
        )

    def mark_expired_running_leases(
        self, *, now: datetime | None = None
    ) -> TaskReport:
        """Fail started jobs whose lease expired.

        These are never requeued: the work already ran on a Worker, and running
        it again would duplicate a paid Codex session.
        """
        expired = self._leases.expire_started_leases(now=now)
        return TaskReport(name="mark_expired_running_leases", affected=expired)

    def reconcile_nonrunnable_jobs(
        self, *, now: datetime | None = None
    ) -> TaskReport:
        repaired = self._leases.reconcile_nonrunnable_jobs(
            now=now,
            limit=self.batch_size,
        )
        return TaskReport(name="reconcile_nonrunnable_jobs", affected=repaired)

    def mark_suspected_offline(
        self, *, now: datetime | None = None
    ) -> TaskReport:
        marked = self._leases.mark_suspected_offline(now=now)
        return TaskReport(name="mark_suspected_offline", affected=marked)

    # -- acceptance----------------------------------------------------------

    def acceptance_candidates(self, *, now: datetime | None = None) -> list[str]:
        """Ids the auto-accept sweep would consider.

        Exposed so a test can assert on the candidate query in isolation: a
        refund-pending order keeps the acceptance_deadline written at delivery,
        so filtering on the deadline alone would pick up orders that have
        already left the acceptance states.
        """
        moment = now or datetime.now(timezone.utc)
        with self._session_factory() as session:
            return list(
                session.scalars(
                    select(Order.id)
                    .where(
                        Order.state.in_(AUTO_ACCEPTABLE_STATES),
                        Order.acceptance_deadline.is_not(None),
                        Order.acceptance_deadline <= moment,
                    )
                    .order_by(Order.acceptance_deadline)
                    .limit(self.batch_size)
                ).all()
            )

    def auto_accept_expired_orders(
        self, *, now: datetime | None = None
    ) -> TaskReport:
        """Close delivered orders whose three-day window has passed."""
        moment = now or datetime.now(timezone.utc)
        affected = 0
        with self._session_factory() as session:
            candidates = session.scalars(
                select(Order)
                .where(
                    Order.state.in_(AUTO_ACCEPTABLE_STATES),
                    Order.acceptance_deadline.is_not(None),
                    Order.acceptance_deadline <= moment,
                )
                .order_by(Order.acceptance_deadline)
                .limit(self.batch_size)
            ).all()
            _after_candidate_select("auto_accept_expired_orders")
            for order in candidates:
                observed = OrderState(order.state)
                if observed not in AUTO_ACCEPTABLE_STATES:
                    continue
                require_order_transition(observed, OrderState.ACCEPTED)
                # Compare-and-set: a user accepting or refunding between the
                # select and this write must win, not be overwritten. The
                # in-loop check above cannot see a change committed by another
                # connection, so the predicate is what actually protects it.
                result = session.execute(
                    update(Order)
                    .where(Order.id == order.id, Order.state == observed)
                    .values(state=OrderState.ACCEPTED)
                )
                affected += result.rowcount
            session.commit()
        return TaskReport(name="auto_accept_expired_orders", affected=affected)

    # -- file cleanup --------------------------------------------------------

    def delete_expired_quotes(self, *, now: datetime | None = None) -> TaskReport:
        """Collect PDFs uploaded for quotes that were never paid for.

        A consumed quote belongs to a paid order, so its files are left alone
        even after the quote's own expiry.
        """
        moment = now or datetime.now(timezone.utc)
        with self._session_factory() as session:
            common = (
                QuoteSession.consumed_at.is_(None),
                QuoteSession.expires_at <= moment,
            )
            candidates_query = union_all(
                select(QuoteSession.source_file_id.label("file_id"))
                .join(FileObject, FileObject.id == QuoteSession.source_file_id)
                .where(*common, FileObject.state != FileState.DELETED),
                select(QuoteSession.reference_file_id.label("file_id"))
                .join(FileObject, FileObject.id == QuoteSession.reference_file_id)
                .where(*common, FileObject.state != FileState.DELETED),
            ).subquery()
            candidates = list(
                session.scalars(
                    select(candidates_query.c.file_id).limit(self.batch_size)
                ).all()
            )
            affected = self._collect_files(session, candidates, moment)
            session.commit()
        return TaskReport(name="delete_expired_quotes", affected=affected)

    def delete_expired_order_files(
        self, *, now: datetime | None = None
    ) -> TaskReport:
        """Collect artefacts of finished orders past their retention window."""
        moment = now or datetime.now(timezone.utc)
        cutoff = moment - ORDER_FILE_RETENTION
        with self._session_factory() as session:
            order_filter = (
                Order.state.in_(FINISHED_ORDER_STATES),
                Order.created_at <= cutoff,
            )
            candidates_query = union_all(
                select(QuoteSession.source_file_id.label("file_id"))
                .join(Order, Order.quote_session_id == QuoteSession.id)
                .join(FileObject, FileObject.id == QuoteSession.source_file_id)
                .where(*order_filter, FileObject.state != FileState.DELETED),
                select(QuoteSession.reference_file_id.label("file_id"))
                .join(Order, Order.quote_session_id == QuoteSession.id)
                .join(FileObject, FileObject.id == QuoteSession.reference_file_id)
                .where(*order_filter, FileObject.state != FileState.DELETED),
                select(GradingRound.result_json_file_id.label("file_id"))
                .join(Order, Order.id == GradingRound.order_id)
                .join(FileObject, FileObject.id == GradingRound.result_json_file_id)
                .where(*order_filter, FileObject.state != FileState.DELETED),
                select(GradingRound.result_pdf_file_id.label("file_id"))
                .join(Order, Order.id == GradingRound.order_id)
                .join(FileObject, FileObject.id == GradingRound.result_pdf_file_id)
                .where(*order_filter, FileObject.state != FileState.DELETED),
            ).subquery()
            candidates = list(
                session.scalars(
                    select(candidates_query.c.file_id).limit(self.batch_size)
                ).all()
            )

            affected = self._collect_files(session, candidates, moment)
            session.commit()
        return TaskReport(name="delete_expired_order_files", affected=affected)

    def delete_expired_temporary_files(
        self, *, now: datetime | None = None
    ) -> TaskReport:
        """Collect any expired temporary object, including result staging."""
        moment = now or datetime.now(timezone.utc)
        with self._session_factory() as session:
            candidates = list(
                session.scalars(
                    select(FileObject.id)
                    .where(
                        FileObject.state == FileState.TEMPORARY,
                        FileObject.expires_at <= moment,
                    )
                    .order_by(FileObject.expires_at, FileObject.id)
                    .limit(self.batch_size)
                ).all()
            )
            affected = self._collect_files(session, candidates, moment)
            session.commit()
        return TaskReport(name="delete_expired_temporary_files", affected=affected)

    def delete_stale_part_files(
        self, *, now: datetime | None = None
    ) -> TaskReport:
        """Remove abandoned atomic-write scratch files without following links."""
        moment = now or datetime.now(timezone.utc)
        cutoff = moment.timestamp() - PART_FILE_RETENTION.total_seconds()
        staging = self._settings.data_dir / "staging"
        affected = 0
        if staging.is_dir():
            for entry in sorted(staging.iterdir(), key=lambda path: path.name):
                if affected >= self.batch_size:
                    break
                try:
                    if (
                        entry.is_symlink()
                        or not entry.name.endswith(".part")
                        or not entry.is_file()
                        or entry.stat().st_mtime > cutoff
                    ):
                        continue
                    entry.unlink()
                    affected += 1
                except OSError:
                    continue
        return TaskReport(name="delete_stale_part_files", affected=affected)

    def delete_expired_sessions_and_events(
        self, *, now: datetime | None = None
    ) -> TaskReport:
        """Apply bounded retention while leaving immutable AuditLog untouched."""
        moment = now or datetime.now(timezone.utc)
        session_cutoff = moment - SESSION_ROW_RETENTION
        event_cutoff = moment - WORKER_EVENT_RETENTION
        affected = 0
        with self._session_factory() as session:
            for model in (MiniappSession, AdminSession):
                ids = list(
                    session.scalars(
                        select(model.id)
                        .where(
                            or_(
                                model.expires_at <= session_cutoff,
                                model.revoked_at <= session_cutoff,
                            )
                        )
                        .order_by(model.expires_at, model.id)
                        .limit(self.batch_size)
                    ).all()
                )
                if ids:
                    affected += session.execute(
                        delete(model).where(model.id.in_(ids))
                    ).rowcount
            event_ids = list(
                session.scalars(
                    select(WorkerEvent.id)
                    .where(WorkerEvent.created_at <= event_cutoff)
                    .order_by(WorkerEvent.created_at, WorkerEvent.id)
                    .limit(self.batch_size)
                ).all()
            )
            if event_ids:
                affected += session.execute(
                    delete(WorkerEvent).where(WorkerEvent.id.in_(event_ids))
                ).rowcount
            session.commit()
        return TaskReport(name="delete_expired_sessions_and_events", affected=affected)

    def _collect_files(
        self,
        session: Session,
        file_ids: list[str],
        moment: datetime,
    ) -> int:
        """Remove bytes, then tombstone the row.

        Deliberately the opposite order from the result-commit path: there the
        bytes are the only copy and must survive a rollback, whereas here the
        intent is destruction. Unlinking first means a crash leaves a row whose
        state is re-derived on the next run rather than a row claiming the file
        still exists. Deletion is best-effort: a storage error must not wedge
        the whole cycle.
        """
        affected = 0
        for file_id in dict.fromkeys(file_ids):
            record = session.get(FileObject, file_id)
            if record is None or record.state == FileState.DELETED:
                continue
            try:
                self._store.delete(record.relative_path)
            except FileStorageError:
                continue
            record.state = FileState.DELETED
            record.expires_at = moment
            session.add(record)
            affected += 1
        return affected

    # -- refunds -------------------------------------------------------------

    def retry_failed_refund_queries(
        self, *, now: datetime | None = None
    ) -> TaskReport:
        """Re-drive refunds that failed at the gateway.

        Each retry goes through RefundService.execute, which reuses the stored
        external_refund_id, so a retry can never become a second refund.
        """
        moment = now or datetime.now(timezone.utc)
        with self._session_factory() as session:
            refund_ids = list(
                session.scalars(
                    select(Refund.id)
                    .where(Refund.state == RefundState.REFUND_FAILED)
                    .order_by(Refund.updated_at, Refund.created_at, Refund.id)
                    .limit(self.batch_size)
                ).all()
            )

        affected = 0
        for refund_id in refund_ids:
            outcome = self._refunds.execute(refund_id, now=moment)
            if outcome.state is RefundState.REFUNDED:
                affected += 1
        return TaskReport(name="retry_failed_refund_queries", affected=affected)

    # -- backups -------------------------------------------------------------

    def verify_backup_freshness(self, *, now: datetime | None = None) -> TaskReport:
        """Require a successful off-host backup marker newer than 26 hours."""
        from server.config import Environment

        if self._settings.environment is not Environment.PRODUCTION:
            return TaskReport(name="verify_backup_freshness", skipped=True)
        moment = now or datetime.now(timezone.utc)
        marker = self._settings.backup_success_marker
        try:
            completed = datetime.fromtimestamp(marker.stat().st_mtime, timezone.utc)
        except OSError:
            return TaskReport(
                name="verify_backup_freshness",
                failed=True,
                error="backup_marker_missing",
            )
        if moment - completed > timedelta(hours=26):
            return TaskReport(
                name="verify_backup_freshness",
                failed=True,
                error="backup_stale",
            )
        return TaskReport(name="verify_backup_freshness", affected=0)
