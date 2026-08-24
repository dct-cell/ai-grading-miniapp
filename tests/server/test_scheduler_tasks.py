"""Time-triggered lifecycle transitions owned by the scheduler.

Every task here must be idempotent: the loop runs every20 seconds, a deploy can
briefly overlap two processes, and an operator may run one by hand. Running a
task twice must not error, refund twice, or accept an order that a user just
refunded.

Time is injected rather than frozen globally, because the services already
accept ``now`` and SQL time functions do not respect freezegun.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from server.domain.states import JobState, OrderState
from server.models import (
    AdminSession,
    FileObject,
    GradingJob,
    MiniappSession,
    Order,
    QuoteSession,
    Refund,
    WorkerEvent,
)
from server.scheduler.tasks import SchedulerTasks, TaskReport
from server.services.files import FileState
from server.services.refunds import RefundState
from tests.server.conftest import (
    ADMIN_SHARED_KEY,
    authenticate,
    admin_login,
    create_admin,
    create_quote,
    deliver_v1_order,
    make_refund_request,
    pay_for_new_order,
    register_worker,
    worker_headers,
)


def count(session: Session, model) -> int:
    return session.scalar(select(func.count()).select_from(model))


@pytest.fixture
def tasks(client: TestClient) -> SchedulerTasks:
    from server.adapters.payments import FakePaymentGateway

    return SchedulerTasks(
        client.app.state.session_factory,
        settings=client.app.state.settings,
        gateway=FakePaymentGateway(),
    )


# --- auto acceptance ----------------------------------------------------------


def test_delivery_auto_accepts_after_the_acceptance_window(
    authenticated_client: TestClient,
    tasks: SchedulerTasks,
    session_factory: sessionmaker[Session],
) -> None:
    order_id = deliver_v1_order(authenticated_client)["order_id"]
    with session_factory() as session:
        deadline = session.get(Order, order_id).acceptance_deadline

    tasks.auto_accept_expired_orders(now=deadline + timedelta(seconds=1))

    with session_factory() as session:
        assert session.get(Order, order_id).state == OrderState.ACCEPTED


def test_auto_accept_leaves_orders_inside_the_window_alone(
    authenticated_client: TestClient,
    tasks: SchedulerTasks,
    session_factory: sessionmaker[Session],
) -> None:
    order_id = deliver_v1_order(authenticated_client)["order_id"]
    with session_factory() as session:
        deadline = session.get(Order, order_id).acceptance_deadline

    tasks.auto_accept_expired_orders(now=deadline - timedelta(seconds=1))

    with session_factory() as session:
        assert session.get(Order, order_id).state == OrderState.V1_DELIVERED


def test_auto_accept_never_overrides_a_pending_refund(
    authenticated_client: TestClient,
    tasks: SchedulerTasks,
    session_factory: sessionmaker[Session],
) -> None:
    """A user who asked for a refund must not be auto-accepted out of it.

    REFUND_PENDING can still reach ACCEPTED via an Admin rejection, so the task
    must select on the delivered states rather than on the deadline alone.
    """
    refund = make_refund_request(authenticated_client, pages=11)
    with session_factory() as session:
        order = session.get(Order, refund["order_id"])
        assert order.state == OrderState.REFUND_PENDING

    tasks.auto_accept_expired_orders(
        now=datetime.now(timezone.utc) + timedelta(days=30)
    )

    with session_factory() as session:
        order = session.get(Order, refund["order_id"])
        assert order.state == OrderState.REFUND_PENDING
        assert session.get(Refund, refund["refund_id"]).state == RefundState.PENDING


def test_auto_accept_loses_to_a_refund_committed_mid_sweep(
    authenticated_client: TestClient,
    tasks: SchedulerTasks,
    session_factory: sessionmaker[Session],
    monkeypatch,
) -> None:
    """A refund committed after the SELECT must not be overwritten.

    The in-loop state check reads the instance the sweep already loaded, so it
    cannot see a change committed by another connection. This drives a real
    refund into that window: only the compare-and-set predicate on the UPDATE
    can save the user's decision, which is the case that matters on MySQL.
    """
    from server.scheduler import tasks as tasks_module

    order_id = deliver_v1_order(authenticated_client, pages=11)["order_id"]
    with session_factory() as session:
        deadline = session.get(Order, order_id).acceptance_deadline

    interleaved: list[str] = []

    def refund_during_the_sweep(task_name: str) -> None:
        if task_name != "auto_accept_expired_orders" or interleaved:
            return
        interleaved.append(task_name)
        response = authenticated_client.post(
            f"/api/v1/orders/{order_id}/refund", json={"reason": "grading_disputed"}
        )
        assert response.status_code in {200, 202}, response.text

    monkeypatch.setattr(
        tasks_module, "_after_candidate_select", refund_during_the_sweep
    )
    report = tasks.auto_accept_expired_orders(now=deadline + timedelta(seconds=1))

    assert interleaved == ["auto_accept_expired_orders"], "no interleaving happened"
    assert report.affected == 0, "the sweep must not claim a row it lost"
    with session_factory() as session:
        order = session.get(Order, order_id)
        refund = session.scalar(select(Refund))
    assert order.state == OrderState.REFUND_PENDING
    assert refund.state == RefundState.PENDING


def test_auto_accept_ignores_orders_that_are_not_awaiting_acceptance(
    authenticated_client: TestClient,
    tasks: SchedulerTasks,
    session_factory: sessionmaker[Session],
) -> None:
    """The candidate query itself must filter on state, not just the deadline.

    A refund-pending order keeps the acceptance_deadline written at delivery, so
    selecting on the deadline alone would sweep up orders that have already left
    the acceptance states.
    """
    refund = make_refund_request(authenticated_client, pages=11)
    with session_factory() as session:
        order = session.get(Order, refund["order_id"])
        assert order.state == OrderState.REFUND_PENDING
        assert order.acceptance_deadline is not None, (
            "the deadline must still be set, or this test proves nothing"
        )
        deadline = order.acceptance_deadline

    selected = tasks.acceptance_candidates(now=deadline + timedelta(seconds=1))

    assert refund["order_id"] not in selected


def test_auto_accept_is_idempotent(
    authenticated_client: TestClient,
    tasks: SchedulerTasks,
    session_factory: sessionmaker[Session],
) -> None:
    order_id = deliver_v1_order(authenticated_client)["order_id"]
    with session_factory() as session:
        deadline = session.get(Order, order_id).acceptance_deadline
    moment = deadline + timedelta(seconds=1)

    first = tasks.auto_accept_expired_orders(now=moment)
    second = tasks.auto_accept_expired_orders(now=moment)

    assert first.affected == 1
    assert second.affected == 0, "an accepted order must not be processed again"
    with session_factory() as session:
        assert session.get(Order, order_id).state == OrderState.ACCEPTED


def test_auto_accept_processes_bounded_batches(
    authenticated_client: TestClient,
    tasks: SchedulerTasks,
    session_factory: sessionmaker[Session],
) -> None:
    """A backlog must not be loaded into memory all at once."""
    assert tasks.batch_size >= 1
    report = tasks.auto_accept_expired_orders(
        now=datetime.now(timezone.utc) + timedelta(days=30)
    )
    assert report.affected <= tasks.batch_size


# --- lease recycling ----------------------------------------------------------


def test_scheduler_requeues_a_never_acknowledged_lease(
    authenticated_client: TestClient,
    tasks: SchedulerTasks,
    session_factory: sessionmaker[Session],
) -> None:
    """A Worker that leased but never ACKed loses the job back to the queue."""
    pay_for_new_order(authenticated_client)
    worker_id = register_worker(
        authenticated_client, installation_id="install-sched-ack"
    )["worker_id"]
    leased = authenticated_client.post(
        "/worker/v1/jobs/lease",
        headers={**worker_headers(worker_id), "Prefer": "wait=0"},
    )
    assert leased.status_code == 200

    tasks.release_unacknowledged_leases(
        now=datetime.now(timezone.utc) + timedelta(minutes=5)
    )

    with session_factory() as session:
        job = session.get(GradingJob, leased.json()["job_id"])
    assert job.state == JobState.QUEUED
    assert job.worker_id is None


def test_scheduler_never_requeues_a_started_job(
    authenticated_client: TestClient,
    tasks: SchedulerTasks,
    session_factory: sessionmaker[Session],
) -> None:
    """A started job is failed, never re-run: duplicate grading costs money.

    This is the invariant Phase 03 fixed and Phase 05 must not undo by wiring
    the recyclers into a loop.
    """
    pay_for_new_order(authenticated_client)
    worker_id = register_worker(
        authenticated_client, installation_id="install-sched-running"
    )["worker_id"]
    leased = authenticated_client.post(
        "/worker/v1/jobs/lease",
        headers={**worker_headers(worker_id), "Prefer": "wait=0"},
    ).json()
    acked = authenticated_client.post(
        f"/worker/v1/jobs/{leased['job_id']}/ack",
        json={"lease_version": leased["lease_version"]},
        headers=worker_headers(worker_id),
    )
    assert acked.status_code == 200

    later = datetime.now(timezone.utc) + timedelta(hours=2)
    tasks.release_unacknowledged_leases(now=later)
    tasks.mark_expired_running_leases(now=later)

    with session_factory() as session:
        job = session.get(GradingJob, leased["job_id"])
    assert job.state == JobState.WORKER_EXCEPTION


def test_scheduler_cancels_a_legacy_job_whose_order_cannot_run(
    authenticated_client: TestClient,
    tasks: SchedulerTasks,
    session_factory: sessionmaker[Session],
) -> None:
    order_id = pay_for_new_order(authenticated_client)
    with session_factory() as session:
        order = session.get(Order, order_id)
        order.state = OrderState.REFUND_PENDING
        session.add(order)
        session.commit()

    report = tasks.reconcile_nonrunnable_jobs()

    assert report.affected == 1
    with session_factory() as session:
        job = session.scalar(
            select(GradingJob).where(GradingJob.order_id == order_id)
        )
    assert job.state == JobState.CANCELLED
    assert job.state != JobState.QUEUED


def test_lease_recycling_is_idempotent(
    authenticated_client: TestClient,
    tasks: SchedulerTasks,
    session_factory: sessionmaker[Session],
) -> None:
    pay_for_new_order(authenticated_client)
    worker_id = register_worker(
        authenticated_client, installation_id="install-sched-twice"
    )["worker_id"]
    authenticated_client.post(
        "/worker/v1/jobs/lease",
        headers={**worker_headers(worker_id), "Prefer": "wait=0"},
    )
    later = datetime.now(timezone.utc) + timedelta(minutes=5)

    first = tasks.release_unacknowledged_leases(now=later)
    second = tasks.release_unacknowledged_leases(now=later)

    assert first.affected == 1
    assert second.affected == 0


# --- file cleanup -------------------------------------------------------------


def test_unpaid_quote_files_are_deleted_after_the_quote_expires(
    authenticated_client: TestClient,
    tasks: SchedulerTasks,
    session_factory: sessionmaker[Session],
    settings,
) -> None:
    """Expired unpaid uploads must leave neither a row nor bytes on disk."""
    quote = create_quote(authenticated_client, pages=2)
    with session_factory() as session:
        record = session.get(QuoteSession, quote["id"])
        source = session.get(FileObject, record.source_file_id)
        relative_path = source.relative_path
        expires_at = record.expires_at
    on_disk = settings.data_dir / relative_path
    assert on_disk.exists()

    report = tasks.delete_expired_quotes(now=expires_at + timedelta(seconds=1))

    assert report.affected >= 1
    with session_factory() as session:
        source = session.get(FileObject, record.source_file_id)
    assert source.state == FileState.DELETED
    assert not on_disk.exists(), "the bytes must be removed, not just the row"


def test_paid_quote_files_are_never_deleted_by_the_quote_sweeper(
    authenticated_client: TestClient,
    tasks: SchedulerTasks,
    session_factory: sessionmaker[Session],
    settings,
) -> None:
    """A consumed quote belongs to a paid order; its PDFs must survive."""
    order_id = pay_for_new_order(authenticated_client)
    with session_factory() as session:
        order = session.get(Order, order_id)
        quote = session.get(QuoteSession, order.quote_session_id)
        source_id = quote.source_file_id
        relative_path = session.get(FileObject, source_id).relative_path

    tasks.delete_expired_quotes(now=datetime.now(timezone.utc) + timedelta(days=30))

    with session_factory() as session:
        source = session.get(FileObject, source_id)
    assert source.state == FileState.RETAINED
    assert (settings.data_dir / relative_path).exists()


def test_quote_cleanup_is_idempotent(
    authenticated_client: TestClient,
    tasks: SchedulerTasks,
    session_factory: sessionmaker[Session],
) -> None:
    quote = create_quote(authenticated_client, pages=2)
    with session_factory() as session:
        expires_at = session.get(QuoteSession, quote["id"]).expires_at
    moment = expires_at + timedelta(seconds=1)

    first = tasks.delete_expired_quotes(now=moment)
    second = tasks.delete_expired_quotes(now=moment)

    assert first.affected >= 1
    assert second.affected == 0, "already-deleted files must not be revisited"


def test_quote_cleanup_drains_past_a_finished_first_batch(
    authenticated_client: TestClient,
    client: TestClient,
    session_factory: sessionmaker[Session],
) -> None:
    from server.adapters.payments import FakePaymentGateway

    quotes = [create_quote(authenticated_client, pages=1) for _ in range(2)]
    moment = datetime.now(timezone.utc) + timedelta(days=2)
    with session_factory() as session:
        for quote in quotes:
            session.get(QuoteSession, quote["id"]).expires_at = moment - timedelta(days=1)
        session.commit()
    small = SchedulerTasks(
        session_factory,
        settings=client.app.state.settings,
        gateway=FakePaymentGateway(),
        batch_size=1,
    )

    first = small.delete_expired_quotes(now=moment)
    second = small.delete_expired_quotes(now=moment)
    third = small.delete_expired_quotes(now=moment)

    assert (first.affected, second.affected, third.affected) == (1, 1, 0)


def test_generic_temporary_cleanup_collects_orphan_result_staging(
    authenticated_client: TestClient,
    tasks: SchedulerTasks,
    session_factory: sessionmaker[Session],
) -> None:
    quote = create_quote(authenticated_client, pages=1)
    moment = datetime.now(timezone.utc) + timedelta(days=2)
    with session_factory() as session:
        quote_row = session.get(QuoteSession, quote["id"])
        file_id = quote_row.source_file_id
        session.get(FileObject, file_id).expires_at = moment - timedelta(seconds=1)
        session.commit()

    report = tasks.delete_expired_temporary_files(now=moment)

    assert report.affected == 1
    with session_factory() as session:
        assert session.get(FileObject, file_id).state == FileState.DELETED


def test_stale_part_cleanup_respects_age_and_suffix(
    tasks: SchedulerTasks,
    settings,
) -> None:
    import os

    staging = settings.data_dir / "staging"
    staging.mkdir(parents=True, exist_ok=True)
    old_part = staging / "old.part"
    fresh_part = staging / "fresh.part"
    unrelated = staging / "old.txt"
    for path in (old_part, fresh_part, unrelated):
        path.write_bytes(b"scratch")
    old_stamp = (datetime.now(timezone.utc) - timedelta(hours=3)).timestamp()
    os.utime(old_part, (old_stamp, old_stamp))
    os.utime(unrelated, (old_stamp, old_stamp))

    report = tasks.delete_stale_part_files()

    assert report.affected == 1
    assert not old_part.exists()
    assert fresh_part.exists()
    assert unrelated.exists()


def test_expired_sessions_and_old_worker_events_are_bounded_and_deleted(
    authenticated_client: TestClient,
    tasks: SchedulerTasks,
    session_factory: sessionmaker[Session],
) -> None:
    create_admin(session_factory)
    admin_login(authenticated_client)
    pay_for_new_order(authenticated_client)
    worker_id = register_worker(
        authenticated_client,
        installation_id="retention-worker",
    )["worker_id"]
    authenticated_client.post(
        "/worker/v1/jobs/lease",
        headers={**worker_headers(worker_id), "Prefer": "wait=0"},
    )
    moment = datetime.now(timezone.utc)
    old = moment - timedelta(days=120)
    with session_factory() as session:
        for record in session.scalars(select(MiniappSession)).all():
            record.expires_at = old
            session.add(record)
        for record in session.scalars(select(AdminSession)).all():
            record.expires_at = old
            session.add(record)
        for event in session.scalars(select(WorkerEvent)).all():
            event.created_at = old
            session.add(event)
        session.commit()

    report = tasks.delete_expired_sessions_and_events(now=moment)

    assert report.affected >= 3
    with session_factory() as session:
        assert count(session, MiniappSession) == 0
        assert count(session, AdminSession) == 0
        assert count(session, WorkerEvent) == 0


def test_order_files_are_deleted_after_their_retention_window(
    authenticated_client: TestClient,
    tasks: SchedulerTasks,
    session_factory: sessionmaker[Session],
    settings,
) -> None:
    """Delivered results are removed once the order is finished and expired."""
    order_id = deliver_v1_order(authenticated_client)["order_id"]
    accepted = authenticated_client.post(f"/api/v1/orders/{order_id}/accept")
    assert accepted.status_code == 200

    report = tasks.delete_expired_order_files(
        now=datetime.now(timezone.utc) + timedelta(days=365)
    )

    assert report.affected >= 1
    with session_factory() as session:
        states = {
            record.state
            for record in session.scalars(
                select(FileObject).where(FileObject.owner_user_id.is_not(None))
            ).all()
        }
    assert FileState.DELETED in states


def test_order_files_survive_while_the_order_is_still_live(
    authenticated_client: TestClient,
    tasks: SchedulerTasks,
    session_factory: sessionmaker[Session],
) -> None:
    """A delivered order inside its window keeps every artefact."""
    deliver_v1_order(authenticated_client)

    tasks.delete_expired_order_files(now=datetime.now(timezone.utc))

    with session_factory() as session:
        states = {
            record.state for record in session.scalars(select(FileObject)).all()
        }
    assert FileState.DELETED not in states


# --- refund reconciliation ----------------------------------------------------


def test_failed_refunds_are_retried(
    authenticated_client: TestClient,
    session_factory: sessionmaker[Session],
    client: TestClient,
) -> None:
    """A refund that failed at the gateway is picked up again, under its own id."""
    from server.adapters.payments import FakePaymentGateway
    from server.services.refunds import RefundService

    gateway = FakePaymentGateway()
    refund = make_refund_request(authenticated_client, pages=11)
    service = RefundService(session_factory, gateway)
    gateway.fail_once()
    service.execute(refund["refund_id"])
    with session_factory() as session:
        stored = session.get(Refund, refund["refund_id"])
        assert stored.state == RefundState.REFUND_FAILED
        external_id = stored.external_refund_id

    tasks = SchedulerTasks(
        session_factory,
        settings=client.app.state.settings,
        gateway=gateway,
    )
    report = tasks.retry_failed_refund_queries()

    assert report.affected == 1
    with session_factory() as session:
        assert session.get(Refund, refund["refund_id"]).state == (
            RefundState.REFUNDED
        )
    assert gateway.external_ids == [external_id, external_id]


def test_retrying_refunds_does_not_touch_settled_ones(
    authenticated_client: TestClient,
    tasks: SchedulerTasks,
    session_factory: sessionmaker[Session],
) -> None:
    """The sweeper must never re-send a refund that already succeeded."""
    make_refund_request(authenticated_client, pages=2)  # settles automatically

    report = tasks.retry_failed_refund_queries()

    assert report.affected == 0


# --- backup freshness ---------------------------------------------------------


def test_backup_freshness_reports_skipped_until_backups_exist(
    tasks: SchedulerTasks,
) -> None:
    """Encrypted COS backups arrive in Phase 09.

    The task is wired now so the loop's shape is final, but with nothing to
    check it must report``skipped`` rather than inventing a healthy result or
    breaking the run.
    """
    report = tasks.verify_backup_freshness()

    assert report.skipped is True
    assert report.affected == 0


def test_production_backup_marker_must_be_fresh(
    tmp_path,
    session_factory: sessionmaker[Session],
) -> None:
    import os

    from server.adapters.payments import FakePaymentGateway
    from server.config import Environment
    from tests.server.conftest import build_settings

    marker = tmp_path / "last-success"
    marker.touch()
    settings = build_settings(
        tmp_path,
        environment=Environment.PRODUCTION,
        database_url="mysql+pymysql://grader:pw@127.0.0.1/grader",
        backup_success_marker=marker,
    )
    production_tasks = SchedulerTasks(
        session_factory,
        settings=settings,
        gateway=FakePaymentGateway(),
    )
    now = datetime.now(timezone.utc)

    assert production_tasks.verify_backup_freshness(now=now).failed is False
    stale = (now - timedelta(hours=27)).timestamp()
    os.utime(marker, (stale, stale))
    report = production_tasks.verify_backup_freshness(now=now)

    assert report.failed is True
    assert report.error == "backup_stale"


# --- the run loop -------------------------------------------------------------


def test_run_due_executes_every_task_and_records_the_run(
    tasks: SchedulerTasks,
) -> None:
    reports = tasks.run_due()

    assert set(reports) == set(SchedulerTasks.TASK_NAMES)
    assert all(isinstance(report, TaskReport) for report in reports.values())
    for name in SchedulerTasks.TASK_NAMES:
        assert tasks.last_success_at(name) is not None


def test_run_due_is_safe_to_run_repeatedly(
    authenticated_client: TestClient,
    tasks: SchedulerTasks,
    session_factory: sessionmaker[Session],
) -> None:
    """The whole cycle is idempotent, not just the individual tasks."""
    deliver_v1_order(authenticated_client)
    before = _snapshot(session_factory)

    tasks.run_due()
    tasks.run_due()
    tasks.run_due()

    assert _snapshot(session_factory) == before


def test_one_failing_task_does_not_stop_the_others(
    tasks: SchedulerTasks,
    monkeypatch,
) -> None:
    """A crash in one sweeper must not silence the rest of the cycle."""

    def explode(**_kwargs) -> TaskReport:
        raise RuntimeError("boom")

    monkeypatch.setattr(tasks, "delete_expired_quotes", explode)

    reports = tasks.run_due()

    assert reports["delete_expired_quotes"].failed is True
    assert reports["auto_accept_expired_orders"].failed is False
    assert tasks.last_success_at("delete_expired_quotes") is None
    assert tasks.last_success_at("auto_accept_expired_orders") is not None


def _snapshot(session_factory: sessionmaker[Session]) -> dict[str, object]:
    with session_factory() as session:
        return {
            "orders": sorted(
                (order.id, order.state)
                for order in session.scalars(select(Order)).all()
            ),
            "jobs": sorted(
                (job.id, job.state)
                for job in session.scalars(select(GradingJob)).all()
            ),
            "files": sorted(
                (record.id, record.state)
                for record in session.scalars(select(FileObject)).all()
            ),
            "refunds": sorted(
                (refund.id, refund.state)
                for refund in session.scalars(select(Refund)).all()
            ),
        }
